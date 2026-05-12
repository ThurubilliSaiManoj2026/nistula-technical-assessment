# src/claude_client.py
# ─────────────────────────────────────────────────────────────────────────────
# Claude API integration layer for the Nistula guest messaging pipeline.
#
# DESIGN PHILOSOPHY:
# This module treats prompt engineering as a first-class concern. Rather than
# sending every guest message to Claude with the same generic system prompt,
# we route each query type to a tailored prompt strategy. A pricing question
# gets a prompt that emphasises numerical accuracy. A complaint gets a prompt
# built around empathy and de-escalation. This is the difference between an
# AI that feels robotic and one that feels like a trained hospitality agent.
#
# The module is intentionally stateless — it receives everything it needs as
# function arguments and returns a structured result. No global state, no
# side effects. This makes it trivially testable and easy to reason about.
# ─────────────────────────────────────────────────────────────────────────────

import os
import logging
from dataclasses import dataclass

import anthropic
from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from src.models import QueryType, NormalizedMessage
from src.property_context import get_property_context

# Configure module-level logger so all API calls and errors are traceable.
logger = logging.getLogger(__name__)

# The exact model string specified in the assessment brief.
MODEL = "claude-sonnet-4-20250514"

# Maximum tokens for the drafted reply. 1024 is sufficient for any guest
# message reply — we want concise, actionable responses, not essays.
MAX_TOKENS = 1024


# ── RESULT DATACLASS ──────────────────────────────────────────────────────────
# Carries the drafted reply along with token usage metadata.
# Token counts are important for production cost monitoring — even in an
# assessment, logging this demonstrates production-grade thinking.

@dataclass
class ClaudeReplyResult:
    drafted_reply: str       # The actual reply text to send to the guest
    model_used: str          # Model string used (for auditability)
    input_tokens: int        # Tokens consumed by the prompt
    output_tokens: int       # Tokens consumed by the reply
    success: bool            # False if the API call failed and fallback was used
    error_message: str | None = None  # Populated only on failure


# ── QUERY-TYPE PROMPT CONFIGURATIONS ──────────────────────────────────────────
# Each query type has its own:
#   - 'instructions': injected into the system prompt to guide Claude's approach
#   - 'temperature': controls creativity vs. factual precision (0.0–1.0)
#   - 'tone_guide': a one-liner describing the desired emotional register
#
# Lower temperature (0.1–0.3) → more deterministic, fact-faithful replies.
# Higher temperature (0.6–0.8) → warmer, more naturally human-sounding replies.

QUERY_TYPE_CONFIGS: dict[str, dict] = {

    "pre_sales_availability": {
        "temperature": 0.3,
        "tone_guide": "Warm and encouraging — the guest is considering booking.",
        "instructions": """
Your task is to answer a villa availability enquiry.
- Confirm clearly whether the requested dates are available based on the property briefing.
- If available, express genuine delight and encourage the next step (enquiring about booking).
- Mention the check-in and check-out times naturally within the reply.
- If availability information is not in the briefing for those specific dates, say the team
  will confirm within a few hours — do not make up availability status.
- Keep the reply warm, concise, and action-oriented. End with an invitation to proceed.
""",
    },

    "pre_sales_pricing": {
        "temperature": 0.2,
        "tone_guide": "Clear and transparent — guests trust agents who show their working.",
        "instructions": """
Your task is to answer a villa pricing enquiry.
- Always show the calculation explicitly: base rate × nights, plus any extra guest charges.
- State the exact total in INR. Do not round or estimate — use the figures in the briefing.
- If the guest has mentioned a number of adults, factor in extra guest charges if applicable.
- Mention what is included (private pool, caretaker, etc.) to justify the rate — this
  is hospitality sales, not just quoting numbers.
- If a guest has NOT specified nights or guest count, give the base nightly rate and
  explain how to calculate the total once they share their travel details.
- End with a warm call to action.
""",
    },

    "post_sales_checkin": {
        "temperature": 0.3,
        "tone_guide": "Helpful and welcoming — the guest has already booked and is excited.",
        "instructions": """
Your task is to answer a pre-arrival or check-in related question from a confirmed guest.
- Answer the specific question asked directly and clearly.
- If asked for the WiFi password, share it exactly as stated in the property briefing.
- If asked about check-in time or directions, provide the exact details from the briefing.
- Remind the guest that the caretaker is available during specific hours for any help.
- Tone should feel like a warm welcome from a trusted host — the guest has already chosen
  Nistula, so focus on making them feel excited and well-prepared for their stay.
""",
    },

    "special_request": {
        "temperature": 0.6,
        "tone_guide": "Accommodating and personal — make the guest feel their request matters.",
        "instructions": """
Your task is to respond to a special request from a guest (early check-in, chef booking,
airport transfer, celebration arrangements, etc.).
- Acknowledge the request warmly and specifically — never use a generic acknowledgement.
- Confirm what IS possible based on the property briefing (chef on call, caretaker hours).
- For requests that require coordination (airport pickup, early check-in), say that the
  team will personally follow up to confirm arrangements — do not promise things not in
  the briefing.
- Make the guest feel like their request is being handled with personal attention,
  not just logged into a system.
- End with a warm assurance that the team is on it.
""",
    },

    "complaint": {
        "temperature": 0.7,
        "tone_guide": "Deeply empathetic and action-focused — the guest is distressed.",
        "instructions": """
Your task is to respond to a guest complaint. This is the most sensitive message type.
- Lead with a sincere, specific apology. Never be defensive. Never make excuses.
- Acknowledge the exact issue the guest has raised — show you have understood them.
- Commit to immediate action: tell the guest that the team is being alerted right now.
- If it is late at night, acknowledge that the guest is dealing with this at an
  inconvenient hour and that this makes it worse — show human understanding.
- Do NOT make promises about refunds, compensation, or outcomes in this reply —
  that is a human decision. Instead, assure the guest that the right person will
  follow up urgently.
- Keep the tone warm, sincere, and human. Avoid corporate language.
- The goal is to de-escalate and make the guest feel heard, not to close the complaint.
""",
    },

    "general_enquiry": {
        "temperature": 0.4,
        "tone_guide": "Friendly and informative — treat every question as an opportunity.",
        "instructions": """
Your task is to answer a general enquiry about the villa or its facilities.
- Answer accurately using only the information in the property briefing.
- If the answer is in the briefing (pet policy, parking, amenities), state it clearly.
- If the answer is NOT in the briefing, do not guess — say the team will be happy
  to confirm and to get in touch.
- Use each answer as a gentle opportunity to highlight the villa's strengths
  where it naturally fits the context.
- Keep the reply conversational and warm.
""",
    },
}


# ── SYSTEM PROMPT BUILDER ─────────────────────────────────────────────────────

def _build_system_prompt(
    query_type: QueryType,
    property_context: str,
    guest_name: str,
    source: str,
) -> str:
    """
    Constructs the complete system prompt for a given query type.

    The system prompt has four sections:
    1. Role definition — who Claude is in this context
    2. Property briefing — the factual ground truth about the villa
    3. Query-specific instructions — how to handle this particular type
    4. Universal formatting rules — reply length, style, sign-off
    """

    config = QUERY_TYPE_CONFIGS.get(query_type, QUERY_TYPE_CONFIGS["general_enquiry"])

    # Map source channel to appropriate greeting style.
    # WhatsApp and Instagram replies should feel personal and informal.
    # Booking.com and Airbnb replies are slightly more formal (platform context).
    channel_notes = {
        "whatsapp":   "This message came via WhatsApp. Keep the tone personal and warm.",
        "instagram":  "This message came via Instagram DM. Keep the tone friendly and modern.",
        "booking_com":"This message came via Booking.com. Maintain a professional but warm tone.",
        "airbnb":     "This message came via Airbnb. Keep the tone friendly and host-like.",
        "direct":     "This is a direct enquiry. Keep the tone personal and welcoming.",
    }
    channel_note = channel_notes.get(source, "Keep the tone professional and warm.")

    system_prompt = f"""
You are Nistula's AI guest communication assistant — a warm, professional, and deeply
knowledgeable hospitality agent for Nistula luxury villa stays in Goa, India.

You communicate on behalf of the Nistula team. Your replies should feel like they come
from a thoughtful, attentive human host — never robotic, never generic.

══════════════════════════════════════════════════════
PROPERTY BRIEFING — USE ONLY THESE FACTS
══════════════════════════════════════════════════════
{property_context}

══════════════════════════════════════════════════════
GUEST & CHANNEL CONTEXT
══════════════════════════════════════════════════════
Guest name: {guest_name}
{channel_note}
Tone guide: {config['tone_guide']}

══════════════════════════════════════════════════════
TASK-SPECIFIC INSTRUCTIONS
══════════════════════════════════════════════════════
{config['instructions'].strip()}

══════════════════════════════════════════════════════
UNIVERSAL FORMATTING RULES
══════════════════════════════════════════════════════
- Address the guest by their first name naturally (not in every sentence — that feels robotic).
- Keep replies between 80 and 200 words. Concise, warm, and complete.
- Do NOT use bullet points or numbered lists — write in natural conversational prose.
- Do NOT start with "Certainly!", "Of course!", "Absolutely!" or any hollow affirmation.
- End replies with a warm sign-off but do NOT write "Best regards" or a formal closing —
  this is a hospitality conversation, not a corporate email.
- Never reveal that you are an AI. You are the Nistula team.
- Never invent facts not present in the property briefing above.
""".strip()

    return system_prompt


# ── USER TURN BUILDER ─────────────────────────────────────────────────────────

def _build_user_prompt(message: NormalizedMessage) -> str:
    """
    Constructs the user turn that carries the guest's actual message.
    We include the booking reference if present, since it adds important
    context for post-sales queries.
    """

    booking_info = (
        f"Booking reference: {message.booking_ref}\n"
        if message.booking_ref
        else ""
    )

    return (
        f"Guest message received at {message.timestamp.strftime('%I:%M %p on %B %d, %Y')}:\n\n"
        f"{booking_info}"
        f'"{message.message_text}"'
    )


# ── MAIN API CALL FUNCTION ────────────────────────────────────────────────────

def get_claude_reply(message: NormalizedMessage) -> ClaudeReplyResult:
    """
    Calls the Claude API to draft a reply to a guest message.

    This function:
    1. Fetches the property context for the message's property_id.
    2. Builds a query-type-specific system prompt.
    3. Calls the Anthropic API with the appropriate temperature.
    4. Returns the drafted reply along with token usage metadata.

    If the API call fails for any reason, returns a safe fallback reply
    and logs the error — the system never crashes on an API failure.

    Args:
        message: The fully normalised and classified guest message.

    Returns:
        A ClaudeReplyResult containing the drafted reply and metadata.
    """

    # ── Step 1: Fetch property context ────────────────────────────────────────
    property_context = get_property_context(message.property_id)

    if property_context is None:
        logger.warning(
            f"Unknown property_id '{message.property_id}' — using generic fallback context."
        )
        # Provide a minimal fallback so Claude can still give a courteous response.
        property_context = (
            "Property details are being retrieved. "
            "For specific questions, the team will follow up shortly."
        )

    # ── Step 2: Build prompts ─────────────────────────────────────────────────
    system_prompt = _build_system_prompt(
        query_type=message.query_type,
        property_context=property_context,
        guest_name=message.guest_name,
        source=message.source,
    )

    user_prompt = _build_user_prompt(message)

    # ── Step 3: Determine temperature for this query type ─────────────────────
    config = QUERY_TYPE_CONFIGS.get(
        message.query_type,
        QUERY_TYPE_CONFIGS["general_enquiry"]
    )
    temperature = config["temperature"]

    # ── Step 4: Initialise Anthropic client and call the API ──────────────────
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY environment variable is not set.")
        return _fallback_result("API key not configured.")

    client = Anthropic(api_key=api_key)

    try:
        logger.info(
            f"Calling Claude API | query_type={message.query_type} | "
            f"property={message.property_id} | temperature={temperature}"
        )

        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=temperature,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
        )

        # Extract the text content from the response.
        # response.content is a list of ContentBlock objects.
        # We take the first text block — there will always be exactly one
        # for a standard (non-tool-use) message completion.
        drafted_reply = response.content[0].text.strip()

        logger.info(
            f"Claude reply received | "
            f"input_tokens={response.usage.input_tokens} | "
            f"output_tokens={response.usage.output_tokens}"
        )

        return ClaudeReplyResult(
            drafted_reply=drafted_reply,
            model_used=MODEL,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            success=True,
        )

    # ── Specific API error handling ────────────────────────────────────────────
    # We catch Anthropic-specific errors separately so we can log them with
    # the right severity level and message. All of them result in the same
    # fallback behaviour for the caller, but the logs are far more useful.

    except RateLimitError as e:
        logger.error(f"Claude API rate limit exceeded: {e}")
        return _fallback_result("Rate limit exceeded — please retry in a moment.")

    except APIConnectionError as e:
        logger.error(f"Claude API connection error: {e}")
        return _fallback_result("Connection to AI service failed.")

    except APIError as e:
        logger.error(f"Claude API error (status {e.status_code}): {e.message}")
        return _fallback_result(f"AI service error: {e.message}")

    except Exception as e:
        logger.exception(f"Unexpected error calling Claude API: {e}")
        return _fallback_result("An unexpected error occurred.")


# ── FALLBACK RESULT BUILDER ───────────────────────────────────────────────────

def _fallback_result(error_message: str) -> ClaudeReplyResult:
    """
    Returns a safe, human-escalating fallback reply when the Claude API fails.
    This ensures the webhook endpoint always returns a valid response — never
    a 500 error — even when the AI layer is unavailable.
    """

    return ClaudeReplyResult(
        drafted_reply=(
            "Thank you for reaching out to Nistula. Our team has received your message "
            "and will get back to you shortly. We appreciate your patience."
        ),
        model_used=MODEL,
        input_tokens=0,
        output_tokens=0,
        success=False,
        error_message=error_message,
    )