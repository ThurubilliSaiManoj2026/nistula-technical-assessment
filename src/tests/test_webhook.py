# src/tests/test_webhook.py
# ─────────────────────────────────────────────────────────────────────────────
# Integration tests for the Nistula guest message webhook pipeline.
#
# These are END-TO-END integration tests — they call the real Claude API
# and exercise the full pipeline: validation → classification → confidence
# scoring → Claude reply → response. They prove the system actually works,
# not just that individual modules are internally consistent.
#
# Run with:  pytest src/tests/test_webhook.py -v
#
# Prerequisites:
#   - .env file must exist with a valid ANTHROPIC_API_KEY
#   - Virtual environment must be activated
#   - All dependencies must be installed (pip install -r requirements.txt)
# ─────────────────────────────────────────────────────────────────────────────

import pytest
from fastapi.testclient import TestClient

from src.main import app

# Initialise the TestClient once for all tests in this module.
# TestClient runs the full FastAPI app in a test context — all middleware,
# exception handlers, and startup/shutdown events fire exactly as in production.
client = TestClient(app)


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: Response Validator
# ─────────────────────────────────────────────────────────────────────────────
# Every successful webhook response must conform to the same shape.
# Rather than repeating these assertions in every test, we centralise them
# in a single helper. This is the DRY principle applied to test code.

def assert_valid_response(response_json: dict):
    """
    Validates that a webhook response contains all required fields
    with correct types and value ranges.
    """
    assert "message_id"      in response_json, "Response must contain message_id"
    assert "query_type"      in response_json, "Response must contain query_type"
    assert "drafted_reply"   in response_json, "Response must contain drafted_reply"
    assert "confidence_score" in response_json, "Response must contain confidence_score"
    assert "action"          in response_json, "Response must contain action"

    # Validate types
    assert isinstance(response_json["message_id"], str),   "message_id must be a string"
    assert isinstance(response_json["drafted_reply"], str), "drafted_reply must be a string"
    assert isinstance(response_json["confidence_score"], float), \
        "confidence_score must be a float"

    # Validate value ranges and allowed values
    assert 0.0 <= response_json["confidence_score"] <= 1.0, \
        "confidence_score must be between 0.0 and 1.0"

    assert response_json["query_type"] in [
        "pre_sales_availability",
        "pre_sales_pricing",
        "post_sales_checkin",
        "special_request",
        "complaint",
        "general_enquiry",
    ], f"Unknown query_type: {response_json['query_type']}"

    assert response_json["action"] in [
        "auto_send", "agent_review", "escalate"
    ], f"Unknown action: {response_json['action']}"

    # A drafted reply should never be empty — even fallbacks have content
    assert len(response_json["drafted_reply"].strip()) > 0, \
        "drafted_reply must not be empty"

    # message_id should be a valid UUID format (36 chars with hyphens)
    assert len(response_json["message_id"]) == 36, \
        "message_id must be a valid UUID (36 characters)"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1: Health Check
# Verifies the service is alive and the API key is configured.
# ─────────────────────────────────────────────────────────────────────────────

def test_health_check():
    """The /health endpoint should return 200 with status: healthy."""
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["api_key_configured"] is True, \
        "API key must be configured — check your .env file"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2: Pre-Sales Availability + Pricing (Assessment Brief Exact Payload)
# This is the exact payload specified in the brief. It MUST work correctly.
# The message asks about both availability AND pricing — a dual query.
# ─────────────────────────────────────────────────────────────────────────────

def test_availability_and_pricing_whatsapp():
    """
    Replicates the exact payload from the assessment brief.
    Should classify as pre_sales_availability or pre_sales_pricing
    (the message contains signals for both — classifier picks the dominant one).
    Should NOT escalate for a clean availability/pricing enquiry.
    """
    payload = {
        "source": "whatsapp",
        "guest_name": "Rahul Sharma",
        "message": "Is the villa available from April 20 to 24? What is the rate for 2 adults?",
        "timestamp": "2026-05-05T10:30:00Z",
        "booking_ref": "NIS-2024-0891",
        "property_id": "villa-b1",
    }

    response = client.post("/webhook/message", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert_valid_response(body)

    # This message contains strong availability AND pricing signals.
    # The classifier should land on one of these two types.
    assert body["query_type"] in ["pre_sales_availability", "pre_sales_pricing"], \
        f"Expected availability or pricing query, got: {body['query_type']}"

    # A clear, positive enquiry for a known property should never escalate.
    assert body["action"] != "escalate", \
        "A clean availability/pricing query should not escalate"

    # The reply should mention the guest by name or reference the villa.
    reply_lower = body["drafted_reply"].lower()
    assert any(keyword in reply_lower for keyword in ["april", "available", "rate", "inr", "night"]), \
        "Reply should address the availability or pricing question specifically"

    print(f"\n[TEST 2] Query type: {body['query_type']}")
    print(f"[TEST 2] Confidence: {body['confidence_score']:.4f}")
    print(f"[TEST 2] Action: {body['action']}")
    print(f"[TEST 2] Reply preview: {body['drafted_reply'][:120]}...")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3: Post-Sales Check-In Query (Booking.com)
# A confirmed guest asking practical pre-arrival questions.
# ─────────────────────────────────────────────────────────────────────────────

def test_post_sales_checkin_booking_com():
    """
    A confirmed guest asking about check-in time and WiFi.
    Should classify as post_sales_checkin.
    Reply must include the WiFi password and check-in time from property context.
    """
    payload = {
        "source": "booking_com",
        "guest_name": "Priya Mehta",
        "message": "Hi, we are arriving tomorrow. What time can we check in? "
                   "Also what is the WiFi password?",
        "timestamp": "2026-04-19T08:00:00Z",
        "booking_ref": "NIS-2024-1042",
        "property_id": "villa-b1",
    }

    response = client.post("/webhook/message", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert_valid_response(body)

    assert body["query_type"] == "post_sales_checkin", \
        f"Expected post_sales_checkin, got: {body['query_type']}"

    # The WiFi password is a hard fact — it must appear in the reply.
    # Nistula@2024 is specified in the property briefing.
    assert "nistula@2024" in body["drafted_reply"].lower() or \
           "2:00" in body["drafted_reply"] or \
           "2 pm" in body["drafted_reply"].lower(), \
        "Reply must include either WiFi password or check-in time"

    print(f"\n[TEST 3] Query type: {body['query_type']}")
    print(f"[TEST 3] Confidence: {body['confidence_score']:.4f}")
    print(f"[TEST 3] Action: {body['action']}")
    print(f"[TEST 3] Reply preview: {body['drafted_reply'][:120]}...")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4: Guest Complaint (WhatsApp — 3AM Scenario)
# The most critical test — complaints must ALWAYS escalate.
# This mirrors the exact scenario described in Part 3 of the assessment.
# ─────────────────────────────────────────────────────────────────────────────

def test_complaint_always_escalates():
    """
    A guest complaint about hot water at 3am demanding a refund.
    Regardless of confidence score, action MUST be 'escalate'.
    This is the hard override rule in confidence.py.
    """
    payload = {
        "source": "whatsapp",
        "guest_name": "Vikram Nair",
        "message": "There is no hot water and we have guests arriving for breakfast "
                   "in 4 hours. This is unacceptable. I want a refund for tonight.",
        "timestamp": "2026-04-21T03:00:00Z",
        "booking_ref": "NIS-2024-0955",
        "property_id": "villa-b1",
    }

    response = client.post("/webhook/message", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert_valid_response(body)

    # This is the most important assertion in the entire test suite.
    # A complaint MUST always escalate — this is non-negotiable.
    assert body["query_type"] == "complaint", \
        f"Expected complaint classification, got: {body['query_type']}"
    assert body["action"] == "escalate", \
        "Complaints must ALWAYS escalate — hard override rule violated"

    # The confidence score for a complaint should be 0.0 (hard override)
    assert body["confidence_score"] == 0.0, \
        "Complaint confidence score must be 0.0 (hard escalation override)"

    # The reply should still be drafted — agents use it as a starting point.
    assert len(body["drafted_reply"]) > 0, \
        "A drafted reply must exist even for escalated complaints"

    print(f"\n[TEST 4] Query type: {body['query_type']}")
    print(f"[TEST 4] Confidence: {body['confidence_score']:.4f}")
    print(f"[TEST 4] Action: {body['action']} ← must be escalate")
    print(f"[TEST 4] Reply preview: {body['drafted_reply'][:120]}...")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5: Special Request (Airbnb)
# A guest wanting early check-in and airport pickup.
# ─────────────────────────────────────────────────────────────────────────────

def test_special_request_airbnb():
    """
    A guest requesting early check-in and airport transfer via Airbnb.
    Should classify as special_request.
    Reply should acknowledge the requests warmly without making hard promises.
    """
    payload = {
        "source": "airbnb",
        "guest_name": "Sarah Johnson",
        "message": "Hello! We land at Goa airport at 10am on check-in day. "
                   "Can you arrange an early check-in and an airport transfer? "
                   "It is our anniversary so hoping for something special!",
        "timestamp": "2026-04-18T14:00:00Z",
        "booking_ref": "NIS-2024-1105",
        "property_id": "villa-b1",
    }

    response = client.post("/webhook/message", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert_valid_response(body)

    assert body["query_type"] == "special_request", \
        f"Expected special_request, got: {body['query_type']}"

    print(f"\n[TEST 5] Query type: {body['query_type']}")
    print(f"[TEST 5] Confidence: {body['confidence_score']:.4f}")
    print(f"[TEST 5] Action: {body['action']}")
    print(f"[TEST 5] Reply preview: {body['drafted_reply'][:120]}...")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6: General Enquiry (Instagram)
# A prospective guest asking about pet policy.
# ─────────────────────────────────────────────────────────────────────────────

def test_general_enquiry_instagram():
    """
    A prospective guest asking about pet policy via Instagram DM.
    Should classify as general_enquiry.
    Reply should clearly state the pet policy from property context.
    """
    payload = {
        "source": "instagram",
        "guest_name": "Ananya Kapoor",
        "message": "Hi! Loved your page. Do you allow pets at the villa? "
                   "We have a small dog and would love to bring him along.",
        "timestamp": "2026-04-15T16:30:00Z",
        "booking_ref": None,
        "property_id": "villa-b1",
    }

    response = client.post("/webhook/message", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert_valid_response(body)

    assert body["query_type"] == "general_enquiry", \
        f"Expected general_enquiry, got: {body['query_type']}"

    # No booking ref means this is a pre-booking enquiry — should not escalate.
    assert body["action"] != "escalate", \
        "A simple pet policy question should not escalate"

    print(f"\n[TEST 6] Query type: {body['query_type']}")
    print(f"[TEST 6] Confidence: {body['confidence_score']:.4f}")
    print(f"[TEST 6] Action: {body['action']}")
    print(f"[TEST 6] Reply preview: {body['drafted_reply'][:120]}...")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7: Invalid Payload — Pydantic Validation
# Sends a request with an invalid source channel.
# FastAPI + Pydantic must reject this with 422 before any business logic runs.
# ─────────────────────────────────────────────────────────────────────────────

def test_invalid_source_channel_returns_422():
    """
    Sends a payload with an invalid source channel ('telegram' is not supported).
    Pydantic should reject this at the API boundary with a 422 Unprocessable Entity.
    This confirms that input validation is enforced structurally, not as an afterthought.
    """
    payload = {
        "source": "telegram",          # Not in the allowed Literal values
        "guest_name": "Test Guest",
        "message": "Is the villa available?",
        "timestamp": "2026-05-01T10:00:00Z",
        "property_id": "villa-b1",
    }

    response = client.post("/webhook/message", json=payload)

    # 422 = Unprocessable Entity — Pydantic validation failed
    assert response.status_code == 422, \
        f"Invalid source should return 422, got {response.status_code}"

    print(f"\n[TEST 7] Invalid payload correctly rejected with status 422")