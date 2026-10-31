"""
Disambiguation service for zero-hallucination guardrail.

find_ambiguous_matches(db, query_text, staff_websites=None) -> List[dict]
  - Runs a broad name search via search_properties_safe
  - Returns list of {property_id, property_name, district, province, status} dicts
  - Returns [] if 0 or 1 match (no ambiguity)
  - Returns list of 2+ when multiple properties share similar name

format_disambiguation_reply(matches: List[dict], original_query: str) -> str
  - Returns a human-readable message listing the ambiguous matches
  - Format: "We have N listings matching '...': 1. **Name** (ID: X, district, status) 2. ..."
  - Ends with: "Which one would you like to explore?"

is_ambiguous_result(matches: List[dict]) -> bool
  - Returns True if len(matches) >= 2
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_DISAMBIGUATION_RESULTS = 10


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_match_dict(prop: Any) -> Dict[str, Any]:
    """Convert a property ORM object or dict into a standard disambiguation dict."""
    if isinstance(prop, dict):
        return {
            "property_id": prop.get("property_id") or prop.get("id") or "",
            "property_name": prop.get("property_name") or prop.get("name") or "",
            "district": prop.get("district") or "",
            "province": prop.get("province") or "",
            "status": prop.get("status") or "",
        }
    # SQLAlchemy ORM object
    return {
        "property_id": getattr(prop, "property_id", "") or "",
        "property_name": getattr(prop, "property_name", "") or "",
        "district": getattr(prop, "district", "") or "",
        "province": getattr(prop, "province", "") or "",
        "status": getattr(prop, "status", "") or "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_ambiguous_matches(
    db: Any,
    query_text: str,
    staff_websites: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Run a broad name search and return a list of matching property dicts.

    Returns:
      - []          if 0 or 1 results (no ambiguity)
      - [dict, ...] if 2+ results share a similar name (ambiguous)
    """
    if not query_text or not query_text.strip():
        return []

    try:
        from app.services.property_search import PropertyFilterParams, search_properties_safe

        filters = PropertyFilterParams(
            query=query_text.strip()[:200],
            limit=MAX_DISAMBIGUATION_RESULTS,
            offset=0,
            is_public=None,  # search across all visibility for admin disambiguation
            websites=staff_websites if staff_websites else None,
        )
        results = search_properties_safe(db, filters)
    except Exception as exc:
        logger.warning("Disambiguation search failed: %s", exc)
        return []

    if not results or len(results) <= 1:
        return []

    matches: List[Dict[str, Any]] = []
    for prop in results:
        try:
            matches.append(_extract_match_dict(prop))
        except Exception:
            continue

    return matches if len(matches) >= 2 else []


def format_disambiguation_reply(
    matches: List[Dict[str, Any]],
    original_query: str,
) -> str:
    """
    Build a human-readable clarification message from ambiguous matches.

    Format:
      We have N listings matching 'query':
      1. **Name** (ID: X, District, status)
      2. **Name** (ID: X, District, status)
      ...
      Which one would you like to explore?
    """
    if not matches:
        return ""

    n = len(matches)
    query_display = (original_query or "").strip()
    lines = [f"We have {n} listings matching '{query_display}':"]

    for i, match in enumerate(matches, start=1):
        name = match.get("property_name") or "Unknown Property"
        pid = match.get("property_id") or "N/A"
        district = match.get("district") or ""
        province = match.get("province") or ""
        status = match.get("status") or ""

        location_parts = [p for p in [district, province] if p]
        location_str = ", ".join(location_parts) if location_parts else ""

        meta_parts = [f"ID: {pid}"]
        if location_str:
            meta_parts.append(location_str)
        if status:
            meta_parts.append(status)

        meta = ", ".join(meta_parts)
        lines.append(f"{i}. **{name}** ({meta})")

    lines.append("Which one would you like to explore?")
    return "\n".join(lines)


def is_ambiguous_result(matches: List[Dict[str, Any]]) -> bool:
    """Return True when there are 2 or more ambiguous matches."""
    return len(matches) >= 2
