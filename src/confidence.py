# src/confidence.py
# ─────────────────────────────────────────────────────────────────────────────
# Confidence scoring engine for the Nistula guest messaging pipeline.
#
# SCORING MODEL — Weighted composite of four independent signals:
#
#   Factor                   Weight    What it measures
#   ─────────────────────── ──────    ──────────────────────────────────────
#   Classification confidence  0.40   How cleanly message maps to one type
#   Sentiment score            0.30   Absence of negative / angry language
#   Completeness score         0.20   Whether property context covers the query
#   Complexity score           0.10   Penalty for multi-question messages
#
#   final_score = (F1 × 0.40) + (F2 × 0.30) + (F3 × 0.20) + (F4 × 0.10)
#
# ACTION THRESHOLDS (as specified in the assessment brief):
#   final_score >= 0.85               → auto_send
#   0.60 <= final_score < 0.85        → agent_review
#   final_score < 0.60                → escalate
#   query_type == "complaint" (always) → escalate  [hard override]
#
# WHY THESE WEIGHTS?
# Classification confidence gets the highest weight because it directly
# measures how well-understood the message is. Sentiment gets 0.30 because
# a negative guest is the highest business risk in hospitality — a bad
# auto-response to an angry guest can cost a review. Completeness and
# complexity are secondary but real contributors.
# ─────────────────────────────────────────────────────────────────────────────

import re
from dataclasses import dataclass

from src.models import QueryType, ActionType
from src.classifier import ClassificationResult


# ── SCORING RESULT ────────────────────────────────────────────────────────────
# Carries the final score, chosen action, and a full breakdown of all four
# factor scores — essential for debugging and for the README explanation.

@dataclass
class ConfidenceResult:
    final_score: float                        # 0.0–1.0 composite confidence
    action: ActionType                        # auto_send / agent_review / escalate
    factor_scores: dict[str, float]           # breakdown of all four factors
    reasoning: str                            # human-readable explanation


# ── NEGATIVE SENTIMENT SIGNALS ────────────────────────────────────────────────
# Phrases that signal guest frustration or anger. We scan for these
# independently of the query classifier because a message like
# "Is the villa available? Also the AC is broken and I want a refund"
# could partially score as availability but still contains a complaint signal.
# Negative sentiment here directly reduces confidence regardless of query type.

NEGATIVE_SENTIMENT_PHRASES: list[tuple[str, float]] = [
    # (phrase, penalty_weight) — higher weight = stronger negative signal
    ("unacceptable",            1.0),
    ("refund",                  1.0),
    ("money back",              1.0),
    ("not happy",               0.9),
    ("very disappointed",       1.0),
    ("extremely disappointed",  1.0),
    ("disgusting",              1.0),
    ("appalling",               1.0),
    ("terrible",                0.8),
    ("horrible",                0.8),
    ("awful",                   0.8),
    ("worst",                   0.8),
    ("this is ridiculous",      1.0),
    ("not working",             0.6),
    ("broken",                  0.6),
    ("dirty",                   0.7),
    ("unhappy",                 0.7),
    ("disappointed",            0.7),
    ("complaint",               0.8),
    ("disgusted",               0.9),
    ("furious",                 1.0),
    ("angry",                   0.9),
    ("poor service",            0.9),
    ("bad experience",          0.9),
    ("not satisfied",           0.7),
    ("issue",                   0.3),
    ("problem",                 0.3),
]

# Maximum possible raw penalty score (sum of all weights).
# Used to normalise the sentiment score to [0.0, 1.0].
MAX_SENTIMENT_PENALTY = sum(w for _, w in NEGATIVE_SENTIMENT_PHRASES)


# ── FACTOR 2: SENTIMENT SCORE ─────────────────────────────────────────────────

def _compute_sentiment_score(message: str) -> float:
    """
    Returns a sentiment score between 0.0 and 1.0.
    1.0 = completely positive / neutral message (no penalty applied).
    0.0 = extremely negative / angry message (maximum penalty applied).

    A message with no negative signals scores 1.0 (full confidence contribution).
    Every negative phrase detected reduces the score proportionally to its weight.
    """

    normalized = re.sub(r'\s+', ' ', message.lower().strip())
    total_penalty = 0.0

    for phrase, weight in NEGATIVE_SENTIMENT_PHRASES:
        if len(phrase.split()) > 1:
            if phrase in normalized:
                total_penalty += weight
        else:
            pattern = r'\b' + re.escape(phrase) + r'\b'
            if re.search(pattern, normalized):
                total_penalty += weight

    # Normalise: cap at MAX_SENTIMENT_PENALTY to keep in [0.0, 1.0] range.
    normalised_penalty = min(total_penalty / MAX_SENTIMENT_PENALTY, 1.0)

    # Invert: high penalty → low sentiment score.
    return round(1.0 - normalised_penalty, 4)


# ── FACTOR 3: COMPLETENESS SCORE ─────────────────────────────────────────────

def _compute_completeness_score(
    query_type: QueryType,
    property_found: bool,
    property_context: str | None,
) -> float:
    """
    Returns a completeness score between 0.0 and 1.0.
    Measures whether our property data store had enough context to answer
    the specific query type. A pricing question for an unknown property
    gets a very low completeness score.

    Args:
        query_type:       The classified query type.
        property_found:   Whether the property_id exists in our data store.
        property_context: The formatted context string (None if not found).
    """

    # If we don't even know the property, we cannot answer anything reliably.
    if not property_found or property_context is None:
        return 0.2   # Very low — we're essentially guessing

    # For each query type, check whether the context contains the key
    # information needed to answer that type of question.
    context_lower = property_context.lower()

    completeness_checks: dict[str, list[str]] = {
        "pre_sales_availability": ["availability", "available"],
        "pre_sales_pricing":      ["base rate", "inr", "per night"],
        "post_sales_checkin":     ["check-in", "wifi", "caretaker"],
        "special_request":        ["chef", "caretaker"],
        "complaint":              [],       # Complaints don't need specific data
        "general_enquiry":        ["amenities", "pool", "parking"],
    }

    required_keywords = completeness_checks.get(query_type, [])

    # If no specific keywords are required (e.g. complaints), full completeness.
    if not required_keywords:
        return 1.0

    # Count how many required keywords are present in the context.
    matched = sum(1 for kw in required_keywords if kw in context_lower)
    completeness = matched / len(required_keywords)

    # Even with partial data, a known property gets a minimum floor of 0.5.
    return round(max(0.5, completeness), 4)


# ── FACTOR 4: COMPLEXITY SCORE ────────────────────────────────────────────────

def _compute_complexity_score(message: str) -> float:
    """
    Returns a complexity score between 0.0 and 1.0.
    Penalises messages that contain multiple distinct questions, since
    multi-question messages are harder for Claude to answer completely.

    Detection strategy: count question marks and question-starting phrases.
    Each additional question beyond the first reduces the score by 0.15.
    """

    normalized = message.strip()

    # Count explicit question marks as a proxy for distinct questions.
    question_mark_count = normalized.count("?")

    # Also detect implicit questions (sentences starting with question words
    # but missing a question mark — common in WhatsApp messages).
    implicit_question_patterns = [
        r'\bwhat\b', r'\bwhen\b', r'\bhow\b', r'\bis there\b',
        r'\bare there\b', r'\bdo you\b', r'\bcan you\b', r'\bwill\b',
    ]
    implicit_count = sum(
        1 for pattern in implicit_question_patterns
        if re.search(pattern, normalized.lower())
    )

    # Use the larger of the two counts as the question estimate.
    estimated_questions = max(question_mark_count, implicit_count // 2)

    # Score: 1.0 for a single question, decreasing by 0.15 per extra question.
    # Floor at 0.4 so even very complex messages still contribute something.
    score = max(0.4, 1.0 - (max(0, estimated_questions - 1) * 0.15))
    return round(score, 4)


# ── MAIN CONFIDENCE FUNCTION ──────────────────────────────────────────────────

def compute_confidence(
    message: str,
    classification_result: ClassificationResult,
    property_found: bool,
    property_context: str | None,
) -> ConfidenceResult:
    """
    Computes the final confidence score and determines the appropriate action.

    Args:
        message:               The raw guest message text.
        classification_result: Output from classifier.classify_message().
        property_found:        Whether the property_id exists in our data store.
        property_context:      Formatted property context string (or None).

    Returns:
        A ConfidenceResult with the final score, action, and full breakdown.
    """

    query_type = classification_result.query_type

    # ── HARD OVERRIDE: Complaints always escalate ─────────────────────────────
    # We check this before computing any scores because there is no scenario
    # where a complaint should be auto-sent. Speed of computation is irrelevant
    # compared to the business risk of auto-responding to an angry guest.
    if query_type == "complaint":
        return ConfidenceResult(
            final_score=0.0,
            action="escalate",
            factor_scores={
                "classification_confidence": classification_result.classification_confidence,
                "sentiment_score": 0.0,
                "completeness_score": 0.0,
                "complexity_score": 0.0,
            },
            reasoning=(
                "Complaint detected — hard escalation override applied. "
                "No AI auto-response for complaint messages regardless of confidence."
            ),
        )

    # ── COMPUTE ALL FOUR FACTORS ──────────────────────────────────────────────
    f1_classification = classification_result.classification_confidence
    f2_sentiment      = _compute_sentiment_score(message)
    f3_completeness   = _compute_completeness_score(
                            query_type, property_found, property_context
                        )
    f4_complexity     = _compute_complexity_score(message)

    # ── WEIGHTED SUM ──────────────────────────────────────────────────────────
    final_score = (
        (f1_classification * 0.40) +
        (f2_sentiment      * 0.30) +
        (f3_completeness   * 0.20) +
        (f4_complexity     * 0.10)
    )

    # Clamp to [0.0, 1.0] as a safety guarantee.
    final_score = round(max(0.0, min(1.0, final_score)), 4)

    # ── DETERMINE ACTION ──────────────────────────────────────────────────────
    if final_score >= 0.85:
        action = "auto_send"
    elif final_score >= 0.60:
        action = "agent_review"
    else:
        action = "escalate"

    # ── BUILD HUMAN-READABLE REASONING STRING ─────────────────────────────────
    # This gets logged and is invaluable for debugging and for the README.
    reasoning = (
        f"Classification confidence: {f1_classification:.2f} (×0.40) | "
        f"Sentiment score: {f2_sentiment:.2f} (×0.30) | "
        f"Completeness score: {f3_completeness:.2f} (×0.20) | "
        f"Complexity score: {f4_complexity:.2f} (×0.10) | "
        f"Final: {final_score:.4f} → action: {action}"
    )

    return ConfidenceResult(
        final_score=final_score,
        action=action,                        # type: ignore[arg-type]
        factor_scores={
            "classification_confidence": round(f1_classification, 4),
            "sentiment_score":           round(f2_sentiment, 4),
            "completeness_score":        round(f3_completeness, 4),
            "complexity_score":          round(f4_complexity, 4),
        },
        reasoning=reasoning,
    )