# src/main.py
# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application — Nistula Guest Message Webhook Handler.
#
# This is the entry point of the entire system. It wires together:
#   classifier      → determines query type from raw message text
#   property_context → fetches villa facts for the Claude prompt
#   confidence      → scores how safe it is to auto-send the reply
#   claude_client   → drafts the actual guest reply via Claude API
#
# Endpoint:  POST /webhook/message
# Health:    GET  /health
# Docs:      GET  /docs  (FastAPI auto-generated Swagger UI)
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.models import InboundMessage, NormalizedMessage, WebhookResponse
from src.classifier import classify_message
from src.property_context import get_property_context
from src.confidence import compute_confidence
from src.claude_client import get_claude_reply

# ── ENVIRONMENT & LOGGING SETUP ───────────────────────────────────────────────
# Load .env before anything else so ANTHROPIC_API_KEY is available to all
# modules that import at startup. This must be the first executable statement.

load_dotenv()

# Configure structured logging so every request and error is traceable.
# In production this would ship to a log aggregator (CloudWatch, Datadog, etc.)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── FASTAPI APPLICATION ───────────────────────────────────────────────────────

app = FastAPI(
    title="Nistula Guest Message Webhook",
    description=(
        "AI-powered guest messaging pipeline for Nistula luxury villas. "
        "Receives inbound messages from any channel, normalises them into a "
        "unified schema, classifies the query type, drafts a reply via Claude, "
        "and returns a confidence-scored response with a recommended action."
    ),
    version="1.0.0",
)

# CORS middleware — allows the React agent dashboard (Role B) to call this
# API from a different origin in development and production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Restrict to specific frontend origin in production
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── GLOBAL EXCEPTION HANDLER ──────────────────────────────────────────────────
# Catches any unhandled exception that escapes the endpoint handler and returns
# a clean JSON error instead of exposing a raw Python traceback to the caller.

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred. The team has been notified.",
        },
    )


# ── HEALTH CHECK ENDPOINT ─────────────────────────────────────────────────────
# A lightweight endpoint for load balancers, uptime monitors, and deployment
# checks. Returns 200 if the service is running, with the current server time.

@app.get("/health", tags=["System"])
async def health_check():
    """Confirms the service is alive and the API key is configured."""
    api_key_configured = bool(os.getenv("ANTHROPIC_API_KEY"))
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api_key_configured": api_key_configured,
        "version": "1.0.0",
    }


# ── ROOT ENDPOINT ─────────────────────────────────────────────────────────────

@app.get("/", tags=["System"])
async def root():
    return {
        "service": "Nistula Guest Message Webhook",
        "docs": "/docs",
        "health": "/health",
    }


# ── CORE WEBHOOK ENDPOINT ─────────────────────────────────────────────────────

@app.post(
    "/webhook/message",
    response_model=WebhookResponse,
    tags=["Messaging"],
    summary="Process an inbound guest message",
    description=(
        "Receives a raw guest message from any supported channel, normalises it "
        "into a unified schema, classifies the query type, drafts an AI reply "
        "via Claude, and returns a confidence-scored response with a recommended "
        "action (auto_send / agent_review / escalate)."
    ),
)
async def handle_webhook_message(payload: InboundMessage):
    """
    Main webhook endpoint — the heart of the Nistula messaging pipeline.

    Processing pipeline (in order):
      1. Validate inbound payload        (Pydantic, automatic)
      2. Classify query type             (classifier.py)
      3. Fetch property context          (property_context.py)
      4. Build normalised message        (models.py)
      5. Compute confidence score        (confidence.py)
      6. Draft reply via Claude API      (claude_client.py)
      7. Return WebhookResponse          (models.py)
    """

    logger.info(
        f"Inbound message | source={payload.source} | "
        f"guest={payload.guest_name} | property={payload.property_id}"
    )

    # ── STEP 1: Classify the message ──────────────────────────────────────────
    # Run the keyword-scoring classifier on the raw message text.
    # This gives us the query_type and classification_confidence before we
    # build the NormalizedMessage, because query_type is part of that schema.

    classification = classify_message(payload.message)

    logger.info(
        f"Classification | query_type={classification.query_type} | "
        f"confidence={classification.classification_confidence:.3f} | "
        f"scores={classification.scores}"
    )

    # ── STEP 2: Fetch property context ────────────────────────────────────────
    # Attempt to retrieve the property briefing for the given property_id.
    # property_found is used as a signal in the confidence scorer.

    property_context = get_property_context(payload.property_id)
    property_found = property_context is not None

    if not property_found:
        logger.warning(f"Unknown property_id: '{payload.property_id}'")

    # ── STEP 3: Build the NormalizedMessage ───────────────────────────────────
    # This is the unified schema that all downstream modules work with.
    # The message_id UUID is auto-generated by Pydantic's default_factory.

    normalized_message = NormalizedMessage(
        source=payload.source,
        guest_name=payload.guest_name,
        message_text=payload.message,
        timestamp=payload.timestamp,
        booking_ref=payload.booking_ref,
        property_id=payload.property_id,
        query_type=classification.query_type,
    )

    logger.info(f"Normalized message created | message_id={normalized_message.message_id}")

    # ── STEP 4: Compute confidence score ─────────────────────────────────────
    # Combines classification confidence, sentiment, completeness, and
    # message complexity into a single 0.0–1.0 confidence score.
    # Also determines the action: auto_send / agent_review / escalate.

    confidence_result = compute_confidence(
        message=payload.message,
        classification_result=classification,
        property_found=property_found,
        property_context=property_context,
    )

    logger.info(
        f"Confidence result | score={confidence_result.final_score:.4f} | "
        f"action={confidence_result.action} | reasoning={confidence_result.reasoning}"
    )

    # ── STEP 5: Draft reply via Claude API ────────────────────────────────────
    # We always call Claude regardless of the action type. Even escalated
    # messages need a drafted reply — the human agent reviewing the message
    # uses it as a starting point, editing rather than writing from scratch.
    # This is how real agent-assist systems work in production.

    claude_result = get_claude_reply(normalized_message)

    if not claude_result.success:
        logger.error(
            f"Claude API call failed | error={claude_result.error_message} | "
            f"message_id={normalized_message.message_id}"
        )
        # We do NOT raise an HTTPException here. The fallback reply from
        # get_claude_reply() is still a valid, usable response. The action
        # is forced to agent_review so a human sees it — the agent will
        # notice the generic reply and handle it manually.
        action = "agent_review"
    else:
        action = confidence_result.action

    logger.info(
        f"Reply drafted | message_id={normalized_message.message_id} | "
        f"tokens_used={claude_result.input_tokens + claude_result.output_tokens}"
    )

    # ── STEP 6: Build and return the response ─────────────────────────────────
    # Assemble the final WebhookResponse from all pipeline outputs.
    # Pydantic validates this on the way out — if any field is wrong type,
    # it raises before the response is sent, making bugs immediately visible.

    response = WebhookResponse(
        message_id=normalized_message.message_id,
        query_type=normalized_message.query_type,
        drafted_reply=claude_result.drafted_reply,
        confidence_score=confidence_result.final_score,
        action=action,                          # type: ignore[arg-type]
    )

    logger.info(
        f"Response ready | message_id={response.message_id} | "
        f"action={response.action} | confidence={response.confidence_score}"
    )

    return response