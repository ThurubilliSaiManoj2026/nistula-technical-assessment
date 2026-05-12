# src/classifier.py
# ─────────────────────────────────────────────────────────────────────────────
# Query classification engine for the Nistula guest messaging pipeline.
#
# DESIGN: Weighted keyword scoring across all six query types.
# Each category has a set of (keyword/phrase, weight) pairs. We scan the
# guest message for every keyword, accumulate scores per category, then
# pick the highest scorer as the predicted query_type.
#
# The classification_confidence (0.0–1.0) is computed as:
#
#   confidence = winning_score / total_score_across_all_categories
#
# This naturally produces high confidence when one category dominates and
# low confidence when multiple categories score similarly (ambiguous message).
# This value is then used as one of four inputs to the final confidence score.
#
# WHY NOT USE AN LLM FOR CLASSIFICATION?
# We deliberately keep classification local and deterministic. Using Claude
# to classify would add latency, cost, and non-determinism to every request.
# Keyword scoring is fast, explainable, and auditable — exactly what you want
# for a component that runs on every single inbound message.
# ─────────────────────────────────────────────────────────────────────────────

import re
from dataclasses import dataclass
from typing import Optional

from src.models import QueryType


# ── CLASSIFICATION RESULT ─────────────────────────────────────────────────────
# A small dataclass that carries both the predicted query type and the
# confidence in that prediction. Keeping these together makes it easy
# for downstream code (confidence.py, claude_client.py) to use both values.

@dataclass
class ClassificationResult:
    query_type: QueryType
    classification_confidence: float   # 0.0 (totally ambiguous) to 1.0 (certain)
    scores: dict[str, float]           # Full score breakdown — useful for debugging


# ── KEYWORD TABLES ────────────────────────────────────────────────────────────
# Each entry is a (phrase, weight) tuple. Phrases are matched case-insensitively.
# Multi-word phrases are matched before single words to avoid partial matches.
#
# Weight guide:
#   3.0 → near-certain signal for this category (e.g. "refund" → complaint)
#   2.0 → strong signal (e.g. "available" → availability)
#   1.5 → moderate signal
#   1.0 → weak but supporting signal
#
# Ordering within each list: longer phrases first, so "check in time" is
# matched before "check in" — prevents double-counting sub-phrases.

KEYWORD_TABLES: dict[str, list[tuple[str, float]]] = {

    "pre_sales_availability": [
        ("is the villa available",      3.0),
        ("are you available",           3.0),
        ("check availability",          2.5),
        ("any availability",            2.5),
        ("available from",              2.5),
        ("available on",                2.5),
        ("available between",           2.5),
        ("do you have availability",    2.5),
        ("dates available",             2.0),
        ("is it free",                  2.0),
        ("vacant",                      2.0),
        ("available",                   1.5),
        ("dates",                       1.0),
        ("from april",                  1.0),
        ("from may",                    1.0),
        ("from june",                   1.0),
        ("nights",                      0.5),
    ],

    "pre_sales_pricing": [
        ("what is the rate",            3.0),
        ("what are the rates",          3.0),
        ("how much does it cost",       3.0),
        ("how much per night",          3.0),
        ("what is the price",           3.0),
        ("pricing",                     2.5),
        ("per night",                   2.5),
        ("cost for",                    2.5),
        ("charge for",                  2.5),
        ("total cost",                  2.5),
        ("rate for",                    2.0),
        ("price for",                   2.0),
        ("how much",                    2.0),
        ("inr",                         1.5),
        ("rupees",                      1.5),
        ("rate",                        1.5),
        ("price",                       1.5),
        ("cost",                        1.0),
        ("budget",                      1.0),
        ("adults",                      0.8),   # "rate for 2 adults" pattern
    ],

    "post_sales_checkin": [
        ("what time can we check in",   3.0),
        ("check in time",               3.0),
        ("check-in time",               3.0),
        ("what time is check out",      3.0),
        ("check out time",              3.0),
        ("check-out time",              3.0),
        ("wifi password",               3.0),
        ("wi-fi password",              3.0),
        ("internet password",           3.0),
        ("directions to",               2.5),
        ("how do we get",               2.5),
        ("how to get there",            2.5),
        ("where is the key",            2.5),
        ("how do we check in",          2.5),
        ("self check in",               2.5),
        ("arriving at",                 2.0),
        ("arrival time",                2.0),
        ("check in",                    2.0),
        ("check-in",                    2.0),
        ("check out",                   2.0),
        ("check-out",                   2.0),
        ("wifi",                        2.0),
        ("wi-fi",                       2.0),
        ("password",                    1.5),
        ("caretaker",                   1.5),
        ("directions",                  1.5),
        ("how to reach",                1.5),
        ("key",                         1.0),
    ],

    "special_request": [
        ("early check in",              3.0),
        ("early check-in",              3.0),
        ("late check out",              3.0),
        ("late check-out",              3.0),
        ("airport transfer",            3.0),
        ("airport pickup",              3.0),
        ("airport drop",                3.0),
        ("cab from airport",            3.0),
        ("book a chef",                 3.0),
        ("arrange a chef",              3.0),
        ("can you arrange",             2.5),
        ("can you organise",            2.5),
        ("special occasion",            2.5),
        ("birthday",                    2.5),
        ("anniversary",                 2.5),
        ("flower decoration",           2.5),
        ("cake",                        2.0),
        ("extra bed",                   2.0),
        ("baby cot",                    2.0),
        ("high chair",                  2.0),
        ("special request",             2.0),
        ("need help with",              1.5),
        ("could you",                   1.0),
        ("can you",                     0.8),
        ("transfer",                    1.0),
    ],

    "complaint": [
        ("this is unacceptable",        3.0),
        ("i want a refund",             3.0),
        ("i am not happy",              3.0),
        ("i'm not happy",               3.0),
        ("very disappointed",           3.0),
        ("extremely disappointed",      3.0),
        ("not working",                 3.0),
        ("isn't working",               3.0),
        ("doesn't work",                3.0),
        ("broken",                      3.0),
        ("refund",                      3.0),
        ("full refund",                 3.0),
        ("money back",                  3.0),
        ("no hot water",                3.0),
        ("no electricity",              3.0),
        ("no power",                    3.0),
        ("ac not working",              3.0),
        ("air conditioning not",        3.0),
        ("pool is dirty",               3.0),
        ("not clean",                   2.5),
        ("unacceptable",                2.5),
        ("disgusting",                  2.5),
        ("appalling",                   2.5),
        ("terrible",                    2.0),
        ("awful",                       2.0),
        ("horrible",                    2.0),
        ("unhappy",                     2.0),
        ("disappointed",                2.0),
        ("complaint",                   2.0),
        ("complain",                    2.0),
        ("issue",                       1.5),
        ("problem",                     1.5),
        ("not working",                 1.5),
        ("not satisfied",               1.5),
        ("poor",                        1.0),
        ("bad",                         0.8),
    ],

    "general_enquiry": [
        ("do you allow pets",           3.0),
        ("are pets allowed",            3.0),
        ("pet friendly",                3.0),
        ("is there parking",            2.5),
        ("is parking available",        2.5),
        ("free parking",                2.5),
        ("is the pool heated",          2.5),
        ("how big is the pool",         2.5),
        ("is there a chef",             2.5),
        ("is breakfast included",       2.5),
        ("what amenities",              2.5),
        ("what is included",            2.5),
        ("what does the villa include", 2.5),
        ("do you have",                 1.5),
        ("is there",                    1.5),
        ("are there",                   1.5),
        ("does the villa",              1.5),
        ("does it have",                1.5),
        ("pets",                        2.0),
        ("parking",                     1.5),
        ("amenities",                   2.0),
        ("pool",                        1.0),
        ("chef",                        1.0),
        ("kitchen",                     1.0),
        ("gym",                         1.5),
        ("smoking",                     1.5),
    ],
}


# ── CLASSIFIER FUNCTION ───────────────────────────────────────────────────────

def classify_message(message: str) -> ClassificationResult:
    """
    Classifies an incoming guest message into one of six query types using
    weighted keyword scoring.

    Args:
        message: The raw guest message text.

    Returns:
        A ClassificationResult containing the predicted query_type,
        the classification_confidence (0.0–1.0), and the full score
        breakdown across all categories for debugging purposes.
    """

    # Normalize: lowercase + collapse extra whitespace for consistent matching.
    normalized = re.sub(r'\s+', ' ', message.lower().strip())

    # Accumulate weighted scores for each category.
    scores: dict[str, float] = {category: 0.0 for category in KEYWORD_TABLES}

    for category, keyword_list in KEYWORD_TABLES.items():
        for phrase, weight in keyword_list:
            # Use word-boundary-aware search so "rate" doesn't match "caretaker".
            # For multi-word phrases we use simple substring match since word
            # boundaries between words in a phrase work differently.
            if len(phrase.split()) > 1:
                if phrase in normalized:
                    scores[category] += weight
            else:
                # Single word: use word boundary to avoid partial matches.
                pattern = r'\b' + re.escape(phrase) + r'\b'
                if re.search(pattern, normalized):
                    scores[category] += weight

    total_score = sum(scores.values())

    # ── FALLBACK: if nothing matched at all, treat as general_enquiry ─────────
    if total_score == 0.0:
        return ClassificationResult(
            query_type="general_enquiry",
            classification_confidence=0.3,   # low confidence — truly ambiguous
            scores=scores,
        )

    # Identify the winning category (highest score).
    winning_category = max(scores, key=lambda c: scores[c])

    # ── COMPLAINT OVERRIDE ────────────────────────────────────────────────────
    # Complaints are always forced to the complaint category, regardless of
    # score ties, because a missed complaint is far more damaging than a
    # missed availability query. This is a business-logic safety net.
    complaint_score = scores.get("complaint", 0.0)
    if complaint_score > 0.0 and winning_category != "complaint":
        # If complaint scored at all and another category barely won, still
        # classify as complaint. Only override if complaint scored meaningfully.
        if complaint_score >= scores[winning_category] * 0.6:
            winning_category = "complaint"

    # ── CONFIDENCE CALCULATION ────────────────────────────────────────────────
    # confidence = winning_score / total_score
    # This naturally scales: if one category has 90% of total score = 0.90
    # confidence. If two categories tie = ~0.50 confidence.
    winning_score = scores[winning_category]
    raw_confidence = winning_score / total_score

    # Clamp to [0.0, 1.0] as a safety measure (should already be in range).
    classification_confidence = max(0.0, min(1.0, raw_confidence))

    return ClassificationResult(
        query_type=winning_category,          # type: ignore[arg-type]
        classification_confidence=classification_confidence,
        scores=scores,
    )