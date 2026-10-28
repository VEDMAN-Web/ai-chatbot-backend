"""
Public chatbot answer policy: verified listing facts only; advisor for market/legal/finance.
Never invents prices, ownership, yields, or amenities.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence


QUESTION_PROPERTY_FACT = "property_fact"
QUESTION_GENERAL_ADVISORY = "general_advisory"
QUESTION_UNCERTAIN_NAME = "uncertain_name"
QUESTION_FOREIGN_OWNERSHIP = "foreign_ownership"

_FACT_FIELDS = {
    "price": ("price", "price_display", "price_numeric_min", "price_thb_min"),
    "bedrooms": ("bedrooms", "bedrooms_min", "bedrooms_display"),
    "area": ("area", "area_display", "area_sqm_min", "built_area"),
    "location": ("location", "district", "province", "area_name"),
    "ownership": ("ownership", "ownership_type", "tenure"),
    "status": ("status",),
    "amenities": ("amenities", "features", "has_pool", "beach_access", "sea_view"),
    "developer": ("developer", "developer_name"),
    "completion": ("completion", "completion_date", "completionDate"),
}

_GENERAL_ADVISORY_RE = re.compile(
    r"\b("
    r"rental\s+yield|yield|roi|return\s+on\s+investment|appreciation|"
    r"transfer\s+tax|stamp\s+duty|mortgage|loan|financing|visa|residency|"
    r"is\s+it\s+a\s+good\s+investment|good\s+investment|worth\s+(?:buying|investing)|"
    r"market\s+(?:outlook|trend)|tax\s+(?:rate|rates)|capital\s+gains"
    r")\b",
    re.I,
)

_FOREIGN_OWNERSHIP_RE = re.compile(
    r"\b("
    r"foreign(?:er)?s?\s+(?:buy|own|purchase)|can\s+(?:a\s+)?foreign|"
    r"can\s+(?:an?\s+)?indian\s+buy|freehold\s+vs\s+leasehold|condo\s+quota|"
    r"foreign\s+(?:freehold|quota)|can\s+foreigners\s+buy"
    r")\b",
    re.I,
)


def lookup_taught_answer(
    question: str,
    site: Optional[str] = None,
    language: str = "en",
) -> Optional[str]:
    """Hook for admin-taught answers. Returns None until a later module plugs in."""
    _ = (question, site, language)
    return None


def classify_question_kind(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return QUESTION_PROPERTY_FACT
    if _FOREIGN_OWNERSHIP_RE.search(q) and not re.search(r"\bis\s+.+\s+freehold\b", q, re.I):
        return QUESTION_FOREIGN_OWNERSHIP
    if _GENERAL_ADVISORY_RE.search(q):
        return QUESTION_GENERAL_ADVISORY
    return QUESTION_PROPERTY_FACT


def _field_present(facts: Dict[str, Any], field: str) -> bool:
    keys = _FACT_FIELDS.get(field, (field,))
    for key in keys:
        value = facts.get(key)
        if value is None or value is False:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        text = str(value).strip().lower()
        if text in {"", "n/a", "na", "none", "null", "not documented", "beds: on request", "on request", "price on request"}:
            continue
        if field == "ownership" and text in {
            "foreign freehold / leasehold available",
            "freehold / leasehold",
        }:
            continue
        return True
    return False


def _missing_field_reply(field: str, advisor_email: str, property_name: Optional[str] = None) -> str:
    label = {
        "price": "price",
        "bedrooms": "bedroom count",
        "area": "area / size",
        "location": "location",
        "ownership": "ownership",
        "status": "status",
        "amenities": "amenities",
        "developer": "developer",
        "completion": "completion date",
    }.get(field, field)
    who = f" for {property_name}" if property_name else ""
    return (
        f"The {label}{who} is not in our verified data. "
        f"Our advisor ({advisor_email}) can confirm it."
    )


def _general_advisory_reply(advisor_email: str) -> Dict[str, Any]:
    text = (
        "That needs a personalised answer from an advisor — I do not give market, tax, "
        "mortgage, visa, or investment advice from model knowledge. "
        f"Please contact {advisor_email}."
    )
    return {
        "text": text,
        "quick_replies": ["Connect me with an advisor"],
        "fallback_type": "no_data",
        "source": "answer_policy",
    }


def did_you_mean_reply(candidates: Sequence[str], max_names: int = 3) -> str:
    names = [str(n).strip() for n in candidates if str(n or "").strip()][:max_names]
    if not names:
        return "I could not find that property in our verified data. Did you mean another listing?"
    if len(names) == 1:
        return f"Did you mean {names[0]}?"
    if len(names) == 2:
        return f"Did you mean {names[0]} or {names[1]}?"
    return f"Did you mean {names[0]}, {names[1]} or {names[2]}?"


def answer_policy(
    question_kind: str,
    facts: Optional[Dict[str, Any]] = None,
    *,
    question: Optional[str] = None,
    site: Optional[str] = None,
    language: str = "en",
    advisor_email: str = "advisor@varsoviaestate.com",
    missing_field: Optional[str] = None,
    candidate_names: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    Decide a grounded reply.
    Returns dict with keys: text, quick_replies, fallback_type, source.
    """
    facts = dict(facts or {})
    q = question or ""

    taught = lookup_taught_answer(q, site=site, language=language)
    if taught:
        return {
            "text": taught,
            "quick_replies": [],
            "fallback_type": "none",
            "source": "taught",
        }

    kind = (question_kind or "").strip() or classify_question_kind(q)

    if kind == QUESTION_UNCERTAIN_NAME or candidate_names:
        return {
            "text": did_you_mean_reply(candidate_names or []),
            "quick_replies": list(candidate_names or [])[:3],
            "fallback_type": "did_you_mean",
            "source": "answer_policy",
        }

    if kind == QUESTION_FOREIGN_OWNERSHIP:
        from app.services.conversation_controls import foreign_ownership_reply
        return {
            "text": foreign_ownership_reply(
                q,
                property_name=facts.get("name") or facts.get("property_name"),
                ownership=facts.get("ownership") or facts.get("ownership_type"),
                property_type=facts.get("property_type"),
            ),
            "quick_replies": ["Talk to an advisor", "Request the brochure"],
            "fallback_type": "none",
            "source": "foreign_ownership_canned",
        }

    if kind == QUESTION_GENERAL_ADVISORY:
        return _general_advisory_reply(advisor_email)

    # Property facts — only from the record
    field = missing_field
    if not field and q:
        low = q.lower()
        for candidate in (
            "ownership", "price", "bedrooms", "area", "location",
            "status", "amenities", "developer", "completion",
        ):
            if candidate in low or (candidate == "ownership" and "freehold" in low):
                field = candidate
                break
    if field and not _field_present(facts, field):
        name = facts.get("name") or facts.get("property_name")
        return {
            "text": _missing_field_reply(field, advisor_email, str(name) if name else None),
            "quick_replies": ["Connect me with an advisor"],
            "fallback_type": "no_data",
            "source": "answer_policy",
        }

    return {
        "text": None,
        "quick_replies": [],
        "fallback_type": "none",
        "source": "pass_through",
    }
