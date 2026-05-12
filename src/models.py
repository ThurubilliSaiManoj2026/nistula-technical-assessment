# src/models.py
# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models defining the data contracts for the entire webhook pipeline.
# Every other module imports from here. Pydantic automatically validates types,
# raises clear errors on bad input, and serialises cleanly to/from JSON.
# ─────────────────────────────────────────────────────────────────────────────

from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime
import uuid


# ── 1. INBOUND WEBHOOK PAYLOAD ───────────────────────────────────────────────
# This is the raw payload Nistula receives from any channel (WhatsApp,
# Booking.com, Airbnb, Instagram, Direct). The 'source' field is a strict
# Literal so FastAPI rejects any unknown channel at validation time —
# no silent failures.

class InboundMessage(BaseModel):
    source: Literal["whatsapp", "booking_com", "airbnb", "instagram", "direct"]
    guest_name: str
    message: str
    timestamp: datetime
    booking_ref: Optional[str] = None   # Optional: not all enquiries have a booking yet
    property_id: str


# ── 2. QUERY TYPE ENUM ───────────────────────────────────────────────────────
# Six distinct query types as specified in the assessment brief.
# Using Literal keeps this strict and self-documenting.

QueryType = Literal[
    "pre_sales_availability",
    "pre_sales_pricing",
    "post_sales_checkin",
    "special_request",
    "complaint",
    "general_enquiry",
]


# ── 3. NORMALISED UNIFIED SCHEMA ─────────────────────────────────────────────
# Every inbound message — regardless of source channel — is transformed into
# this single unified shape before being passed to Claude. This is the core
# of the "unified inbox" concept: one schema to rule all channels.

class NormalizedMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str
    guest_name: str
    message_text: str
    timestamp: datetime
    booking_ref: Optional[str] = None
    property_id: str
    query_type: QueryType


# ── 4. ACTION ENUM ───────────────────────────────────────────────────────────
# Determines what happens after the AI drafts a reply, based on confidence.
# auto_send  : score ≥ 0.85  → send immediately, no human needed
# agent_review: 0.60 ≤ score < 0.85 → human reviews before sending
# escalate   : score < 0.60 OR complaint → urgent human intervention

ActionType = Literal["auto_send", "agent_review", "escalate"]


# ── 5. WEBHOOK RESPONSE ───────────────────────────────────────────────────────
# This is what the /webhook/message endpoint returns to the caller.
# message_id ties back to the NormalizedMessage so the full record is traceable.

class WebhookResponse(BaseModel):
    message_id: str
    query_type: QueryType
    drafted_reply: str
    confidence_score: float = Field(ge=0.0, le=1.0)  # enforces 0–1 range strictly
    action: ActionType