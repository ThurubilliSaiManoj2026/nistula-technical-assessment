# Nistula Guest Message Webhook
### AI-Powered Guest Communication Pipeline — Technical Assessment Submission

**Candidate:** Thurubilli Sai Manoj  
**Role Applied:** Role C — AI and Integration  
**Assessment Window:** 48 hours  
**Submission Date:** May 2026

---

## What This System Does

Nistula receives guest enquiries and messages across seven channels — WhatsApp, Booking.com, Airbnb, Expedia, MakeMyTrip, Agoda, and Instagram. Today each channel is handled separately, responses are delayed, and guest intelligence is lost between conversations.

This system is the core of the solution to that problem. It is a production-grade backend pipeline that:

- Receives an inbound guest message from any supported channel via a single webhook endpoint
- Normalises it into a unified schema regardless of source channel
- Classifies the query into one of six types using a weighted keyword scoring engine
- Computes a four-factor confidence score that determines how safe it is to auto-send the AI reply
- Drafts a contextually intelligent, channel-aware reply using the Claude API with a query-type-specific prompt strategy
- Returns the drafted reply with a confidence score and a recommended action: `auto_send`, `agent_review`, or `escalate`

---

## Architecture Overview

```
Inbound Message (any channel)
         │
         ▼
┌──────────────────────┐
│  POST /webhook/msg   │  FastAPI validates payload via Pydantic
└─────────┬────────────┘
          │
          ▼
┌──────────────────────┐
│    classifier.py     │  Weighted keyword scoring → query_type
└─────────┬────────────┘         + classification_confidence
          │
          ▼
┌──────────────────────┐
│ property_context.py  │  Fetches Villa B1 briefing by property_id
└─────────┬────────────┘
          │
          ▼
┌──────────────────────┐
│    confidence.py     │  Four-factor score → action decision
└─────────┬────────────┘
          │
          ▼
┌──────────────────────┐
│  claude_client.py    │  Query-type prompt → Claude API → drafted reply
└─────────┬────────────┘
          │
          ▼
      WebhookResponse
  { message_id, query_type,
    drafted_reply, confidence_score, action }
```

---

## Project Structure

```
nistula-technical-assessment/
├── README.md                    ← You are here
├── .env.example                 ← Environment variable template
├── requirements.txt             ← Python dependencies
├── schema.sql                   ← Part 2: PostgreSQL schema
├── thinking.md                  ← Part 3: Written answers
├── conftest.py                  ← Pytest path configuration
└── src/
    ├── main.py                  ← FastAPI app + /webhook/message endpoint
    ├── models.py                ← Pydantic request/response schemas
    ├── classifier.py            ← Query type classification engine
    ├── property_context.py      ← Mock property data + context formatter
    ├── confidence.py            ← Confidence scoring engine
    ├── claude_client.py         ← Claude API integration layer
    └── tests/
        └── test_webhook.py      ← 7 integration tests (all passing)
```

---

## Setup Instructions

### Prerequisites

- Python 3.11 or higher
- A valid Anthropic API key

### Step 1 — Clone the repository

```bash
git clone https://github.com/ThurubilliSaiManoj2026/nistula-technical-assessment.git
cd nistula-technical-assessment
```

### Step 2 — Create and activate a virtual environment

```bash
# Create the environment
python -m venv venv

# Activate on Windows (PowerShell)
venv\Scripts\activate

# Activate on macOS / Linux
source venv/bin/activate
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Configure environment variables

```bash
# Copy the example file
cp .env.example .env
```

Open `.env` and add your Anthropic API key:

```
ANTHROPIC_API_KEY=your_api_key_here
```

### Step 5 — Run the server

```bash
uvicorn src.main:app --reload
```

The server starts at `http://localhost:8000`. Visit `http://localhost:8000/docs` for the auto-generated interactive Swagger UI where every endpoint is fully documented and testable.

### Step 6 — Run the tests

```bash
pytest src/tests/test_webhook.py -v
```

All 7 tests pass end-to-end, including a full complaint escalation test that verifies the hard override rule — complaints always escalate regardless of confidence score.

---

## API Reference

### `POST /webhook/message`

Receives an inbound guest message and returns an AI-drafted reply with confidence scoring.

**Request Body:**

```json
{
  "source": "whatsapp",
  "guest_name": "Rahul Sharma",
  "message": "Is the villa available from April 20 to 24? What is the rate for 2 adults?",
  "timestamp": "2026-05-05T10:30:00Z",
  "booking_ref": "NIS-2024-0891",
  "property_id": "villa-b1"
}
```

Supported `source` values: `whatsapp`, `booking_com`, `airbnb`, `instagram`, `direct`.
The `booking_ref` field is optional — pre-booking enquiries will not have one.

**Response:**

```json
{
  "message_id": "a3f1c2d4-7b8e-4f2a-9c1d-3e5f6a7b8c9d",
  "query_type": "pre_sales_availability",
  "drafted_reply": "Hi Rahul! Great news — Villa B1 is available from April 20 to 24...",
  "confidence_score": 0.9180,
  "action": "auto_send"
}
```

**Action Thresholds:**

| Score Range         | Action         | Meaning                                        |
|---------------------|----------------|------------------------------------------------|
| ≥ 0.85              | `auto_send`    | Send immediately — no human review needed      |
| 0.60 – 0.84         | `agent_review` | Human reviews and approves before sending      |
| < 0.60              | `escalate`     | Urgent human intervention required             |
| `complaint` (always)| `escalate`     | Hard override — complaints always escalate     |

**Validation:** Sending an unsupported `source` value (e.g. `telegram`) returns `422 Unprocessable Entity` — Pydantic rejects invalid input at the API boundary before any business logic runs.

---

### `GET /health`

Returns service status and confirms the API key is configured.

```json
{
  "status": "healthy",
  "timestamp": "2026-05-12T08:30:00Z",
  "api_key_configured": true,
  "version": "1.0.0"
}
```

### `GET /docs`

Auto-generated interactive Swagger UI. Every endpoint, request schema, and response schema is documented and testable directly from the browser.

---

## Confidence Scoring Logic

This is the most important design decision in the system. The confidence score is a number between `0.0` and `1.0` that answers one specific question: **how safe is it to auto-send this AI-drafted reply without a human reviewing it?**

It is computed as a weighted sum of four independent factors, each measuring a different dimension of risk.

---

### Factor 1 — Classification Confidence (Weight: 40%)

This value comes directly from the query classifier. After scoring the guest message against all six query type keyword tables, classification confidence is computed as:

```
classification_confidence = winning_category_score / total_score_across_all_categories
```

When one category clearly dominates — for example, "Is the villa available?" scoring 92% in `pre_sales_availability` — confidence is high (0.92). When a message splits its score across multiple categories — for example, a message asking about availability while also expressing frustration — confidence is lower (0.50), signalling ambiguity.

This factor carries the highest weight (40%) because it directly measures how well the system understood the guest's intent. A misclassified message produces an irrelevant reply no matter how well Claude drafts it.

---

### Factor 2 — Sentiment Score (Weight: 30%)

This factor independently scans the message for negative emotional signals — frustration, anger, disappointment — using a weighted keyword list that is separate from the query classifier. A completely neutral or positive message scores `1.0`. Each negative phrase reduces the score proportionally to its severity weight.

Examples of phrase weights: `"refund"` → 1.0, `"unacceptable"` → 1.0, `"disappointed"` → 0.7, `"issue"` → 0.3. The raw penalty total is normalised against the maximum possible penalty to keep the final score in the 0–1 range, then inverted (high penalty → low score).

Sentiment carries 30% weight because an upset guest is the highest business risk in hospitality. A confidently wrong auto-response to a distressed guest can permanently damage a relationship and a review score. When emotional signals are present, a human should always see the reply first.

---

### Factor 3 — Completeness Score (Weight: 20%)

This factor measures whether the property data store contained sufficient context to answer the specific query type. A pricing question for a known property with full rate data scores `1.0`. A question about an unknown `property_id` scores `0.2` — Claude would be guessing without grounded facts.

For each query type, a set of required context fields is checked against the property briefing:

- `pre_sales_availability` requires availability data
- `pre_sales_pricing` requires rate data in INR
- `post_sales_checkin` requires check-in time and WiFi details
- `complaint` requires no specific data (complaints are handled empathetically regardless)

The score is the ratio of present fields to required fields, with a minimum floor of `0.5` for known properties — because even partial context is genuinely useful to Claude.

---

### Factor 4 — Complexity Score (Weight: 10%)

Multi-question messages are harder for any AI to answer completely and coherently. A single clear question scores `1.0`. Each additional distinct question — detected by counting question marks and implicit question-opening phrases — reduces the score by `0.15`, with a floor of `0.4`.

This carries the lowest weight (10%) because Claude handles multi-part questions reasonably well. It is a mild caution signal, not a strong risk indicator.

---

### The Hard Override Rule

**Any message classified as `complaint` always receives `action: escalate` and `confidence_score: 0.0`, regardless of the computed score.** This override fires before any factor computation — there is no code path through which a complaint can receive `auto_send` or `agent_review`.

This is the most important safety rule in the system. The business risk of a single poorly-handled complaint auto-response — especially at 3am when a guest is distressed — far outweighs any efficiency gain from automating it. Complaints always go to a human. Always.

---

### Worked Example

For the assessment brief payload — WhatsApp, Rahul Sharma, availability and pricing for April 20–24, Villa B1:

| Factor                       | Raw Score | Weight | Contribution |
|------------------------------|-----------|--------|--------------|
| Classification confidence    | 0.87      | 0.40   | 0.3480       |
| Sentiment score              | 1.00      | 0.30   | 0.3000       |
| Completeness score           | 1.00      | 0.20   | 0.2000       |
| Complexity score             | 0.85      | 0.10   | 0.0850       |
| **Final confidence score**   |           |        | **0.9330**   |
| **Action**                   |           |        | **`auto_send`** |

The complexity score is 0.85 rather than 1.0 because the message contains two distinct questions (availability and pricing) — a mild penalty is applied. Despite this, the final score of 0.9330 comfortably clears the 0.85 auto-send threshold.

---

## Query Classification

Six query types are supported. Classification uses weighted keyword scoring — fully local, no external API call, no LLM inference. This keeps classification fast (sub-millisecond), deterministic, and completely auditable.

| Query Type                | Typical Example                                            |
|---------------------------|------------------------------------------------------------|
| `pre_sales_availability`  | "Is the villa free from April 20 to 24?"                   |
| `pre_sales_pricing`       | "What is the rate for 2 adults for 3 nights?"              |
| `post_sales_checkin`      | "What time is check-in? What is the WiFi password?"        |
| `special_request`         | "Can you arrange an early check-in and airport transfer?"  |
| `complaint`               | "The AC is broken. This is unacceptable. I want a refund." |
| `general_enquiry`         | "Do you allow pets? Is there parking at the villa?"        |

**Complaint override:** If the complaint category scores even 60% of the winning category's score, the message is forced to `complaint` classification. This prevents a message like "Is the villa available? Also the AC is broken and I want a refund" from being classified as `pre_sales_availability` and receiving a cheerful booking reply while ignoring the refund demand.

---

## Prompt Engineering Strategy

Claude does not receive a single generic system prompt for all message types. Each query type routes to a tailored prompt configuration with three distinct components:

**Query-specific instructions** guide Claude's approach for each situation. A pricing prompt instructs Claude to show the calculation explicitly and state the exact INR total. A complaint prompt instructs Claude to lead with sincere empathy, acknowledge the specific issue by name, commit to immediate action, and explicitly avoid making refund promises — which are human decisions.

**Per-type temperature** controls the balance between factual precision and natural warmth. Factual queries (`pre_sales_pricing`, `pre_sales_availability`, `post_sales_checkin`) use temperature 0.2–0.3 so Claude stays tightly grounded in the property briefing. Empathetic responses (`complaint`, `special_request`) use temperature 0.6–0.7 so the reply feels warm and human rather than robotically precise.

**Channel-aware tone guidance** adjusts the register of the reply to match the platform context. WhatsApp and Instagram replies feel personal and conversational. Booking.com and Airbnb replies are slightly more formal to match user expectations on those platforms.

**Universal anti-patterns** are explicitly prohibited in every system prompt regardless of query type: hollow affirmations ("Certainly!", "Of course!", "Absolutely!"), bullet-point formatting in replies, corporate sign-offs, and any mention that the responder is an AI. These are common LLM defaults that feel wrong in a hospitality context and would immediately reveal the system as automated.

---

## Database Schema (Part 2 Summary)

The full PostgreSQL schema is in `schema.sql`. Six tables cover the complete data model:

- `properties` — villa inventory, kept in sync with the PMS
- `guests` — one record per real guest across all channels
- `channel_identities` — maps platform-specific IDs (WhatsApp number, Airbnb user ID) to a single guest record, enabling cross-channel identity resolution
- `reservations` — confirmed bookings linking guests to properties with full financial detail
- `conversations` — message threads per channel session, linked to guests and optionally to reservations
- `messages` — every inbound and outbound message in one table, with AI classification metadata, confidence scores, draft source tracking, and the original AI draft preserved even after agent edits

**Hardest design decision:** The `channel_identities` table. A guest contacting via WhatsApp, Airbnb, and Instagram is one person but has three different platform identifiers. Storing channel identities in a dedicated table (rather than adding `airbnb_id`, `whatsapp_phone` columns to `guests`) means adding a new channel in future requires zero schema changes — one new enum value and new rows in an existing table. The naive approach would require `ALTER TABLE` for every new channel onboarded.

---

## Technology Stack

| Component      | Technology                                        |
|----------------|---------------------------------------------------|
| Runtime        | Python 3.11                                       |
| Framework      | FastAPI (async-native, Pydantic v2 integrated)    |
| AI Model       | Anthropic Claude (`claude-sonnet-4-20250514`)     |
| Validation     | Pydantic v2 (request + response schemas)          |
| Testing        | Pytest + FastAPI TestClient (httpx)               |
| Environment    | python-dotenv                                     |
| Database       | PostgreSQL (schema designed, not wired for demo)  |

**Why FastAPI over Django:** This is a pure API service — no templating, no ORM, no admin panel. FastAPI is async-native (non-blocking Claude API calls), Pydantic-native (zero-boilerplate validation), and produces auto-generated OpenAPI docs with no additional configuration. Django's value lies in its full-stack batteries-included ecosystem, none of which is relevant here.

---

## Test Results

```
7 passed in 40.57s

test_health_check                         PASSED
test_availability_and_pricing_whatsapp    PASSED  ← Exact assessment brief payload
test_post_sales_checkin_booking_com       PASSED
test_complaint_always_escalates           PASSED  ← Hard override verified
test_special_request_airbnb              PASSED
test_general_enquiry_instagram            PASSED
test_invalid_source_channel_returns_422   PASSED  ← Pydantic validation verified
```

Tests are end-to-end integration tests — they call the real Claude API and exercise the complete pipeline from HTTP request through classification, confidence scoring, and AI reply generation to validated HTTP response. All seven pass consistently.

---

## Environment Variables

| Variable            | Required | Description                              |
|---------------------|----------|------------------------------------------|
| `ANTHROPIC_API_KEY` | Yes      | Your Anthropic API key for Claude access |

See `.env.example` for the template. Never commit your actual `.env` file — it is listed in `.gitignore`.

---

## Note on Model Deprecation

The assessment brief specified `claude-sonnet-4-20250514`. During testing, the Anthropic SDK raised a deprecation warning indicating this model reaches end-of-life on June 15, 2026. All 7 tests pass and the full system is functional with this model. In a production deployment, the `MODEL` constant in `src/claude_client.py` would be updated to the latest stable Claude Sonnet model string before the deprecation date. The model string is isolated to a single constant, making this a one-line change.

---

## Repository

**GitHub:** [https://github.com/ThurubilliSaiManoj2026/nistula-technical-assessment](https://github.com/ThurubilliSaiManoj2026/nistula-technical-assessment)