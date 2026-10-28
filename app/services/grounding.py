"""
Public-bot grounding helpers: structured LLM state, FACTS packs, metrics.
No network I/O. Pure Python.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import settings

logger = logging.getLogger("app.grounding")

_FACTUAL_HINT = re.compile(
    r"(?:price|cost|how much|bedroom|bathroom|amenit|pool|gym|spa|parking|location|"
    r"address|area|sqm|status|available|ownership|freehold|leasehold|beach|sea\s*view|"
    r"yield|roi|complet|finish|built|handover|show\s+me|find|search|villa|condo|"
    r"under\s+\d|budget|property|listing|details?|tell\s+me\s+about|"
    r"cena|sypialn|basen|własno|wlasno|lokalizac|szukam|pokaż|pokaz|"
    r"ราคา|ห้องนอน|สระ|ทำเล|ค้นหา|มี\s)",
    re.IGNORECASE,
)

_SMALLTALK = re.compile(
    r"^(?:hi|hello|hey|thanks|thank\s+you|ok|okay|bye|good\s+morning|good\s+evening|"
    r"cześć|czesc|dzień\s+dobry|dzięk|dziekuje|สวัสดี|ขอบคุณ|บาย)[\s!.?]*$",
    re.IGNORECASE,
)


def is_factual_listing_query(message: str) -> bool:
    """True when the turn is about listings / property facts (force tools)."""
    msg = (message or "").strip()
    if not msg:
        return False
    if _SMALLTALK.match(msg):
        return False
    if len(msg) < 3:
        return False
    return bool(_FACTUAL_HINT.search(msg))


def build_facts_block(
    cards: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    active_card: Optional[Dict[str, Any]] = None,
    language: str = "en",
) -> str:
    """
    FACTS (verified, the only source you may use) from fresh listing records.
    """
    from app.services.amenity_service import collect_property_amenities, AMENITY_LABELS_EN

    lines = ["FACTS (verified, the only source you may use):"]
    seen: set = set()

    def _one(card: Dict[str, Any], tag: str) -> None:
        if not isinstance(card, dict):
            return
        pid = str(card.get("property_id") or card.get("id") or "").strip()
        key = pid or str(card.get("name") or "")
        if key and key in seen:
            return
        if key:
            seen.add(key)
        name = str(card.get("name") or card.get("property_name") or "").strip()
        loc = str(card.get("location") or "").strip()
        price = str(card.get("price") or card.get("price_display") or "").strip()
        price_num = card.get("price_numeric_min")
        beds = str(card.get("bedrooms") or "").strip()
        baths = str(card.get("bathrooms") or "").strip()
        area = str(card.get("area") or card.get("built_area") or card.get("size") or "").strip()
        status = str(card.get("status") or "").strip()
        ownership = str(card.get("ownership") or card.get("tenure") or "").strip()
        ptype = str(card.get("property_type") or card.get("type") or "").strip()
        desc = str(card.get("description") or card.get("short_description") or "").strip()[:240]
        am_facts = collect_property_amenities(card)
        am_labels = [
            AMENITY_LABELS_EN.get(k, k)
            for k, f in am_facts.items()
            if getattr(f, "present", False)
        ]
        completion = str(
            card.get("completion_date")
            or card.get("completion")
            or (card.get("raw_firestore_data") or {}).get("completionDate")
            or ""
        ).strip()
        lines.append(f"--- {tag} ---")
        if pid:
            lines.append(f"id: {pid}")
        if name:
            lines.append(f"name: {name}")
        if loc:
            lines.append(f"location: {loc}")
        if ptype:
            lines.append(f"type: {ptype}")
        if price:
            lines.append(f"price: {price}")
        if price_num is not None:
            lines.append(f"price_numeric_min: {price_num}")
        if beds:
            lines.append(f"bedrooms: {beds}")
        if baths:
            lines.append(f"bathrooms: {baths}")
        if area:
            lines.append(f"area: {area}")
        if status:
            lines.append(f"status: {status}")
        if ownership:
            lines.append(f"ownership: {ownership}")
        else:
            lines.append("ownership: not documented in verified database")
        if completion:
            lines.append(f"completion: {completion}")
        lines.append(
            "amenities: " + (", ".join(am_labels) if am_labels else "none listed in verified data")
        )
        if desc:
            lines.append(f"description_excerpt: {desc}")

    if active_card:
        _one(active_card, "ACTIVE PROPERTY")
    for i, c in enumerate(cards or []):
        _one(c, f"RESULT {i + 1}")

    if len(lines) == 1:
        lines.append("(no verified listing records for this turn)")
    lines.append(
        "RULE: Answer ONLY from this FACTS block. "
        "Anything not listed here → say it is not in our verified data and offer the advisory team. "
        "Never use general knowledge about specific properties. Never repeat claims from earlier turns."
    )
    return "\n".join(lines)


def build_structured_state_block(
    conversation: Any,
    *,
    language: str = "en",
) -> str:
    """Current search filters, shown listing ids/names, active property — no assistant prose."""
    from app.services.property_context_service import load_property_context

    prefs = dict(getattr(conversation, "preferences", None) or {})
    ctx = load_property_context(conversation)
    filters = dict(prefs.get("active_filters") or {})
    for k in ("location", "property_type", "bedrooms", "min_bedrooms", "max_bedrooms",
              "max_price", "min_price", "has_pool"):
        if filters.get(k) is None and prefs.get(k) is not None:
            filters[k] = prefs.get(k)
        if filters.get(k) is None and k == "max_price" and prefs.get("budget_max") is not None:
            filters["max_price"] = prefs.get("budget_max")

    last_ids = [str(x) for x in (prefs.get("last_result_ids") or []) if x]
    seen_ids = [str(x) for x in (prefs.get("seen_property_ids") or []) if x][:20]
    names: List[str] = []
    ref_map = dict(ctx.reference_map or {})
    for label, pid in ref_map.items():
        if pid and label:
            names.append(f"{label}→{pid}")
    if ctx.active_property_reference:
        names.append(str(ctx.active_property_reference))

    filter_bits = ", ".join(f"{k}={v}" for k, v in filters.items() if v is not None) or "none"
    lines = [
        "=== STRUCTURED STATE (reference only — not a factual source) ===",
        f"language: {language}",
        f"active_filters: {filter_bits}",
        f"active_property_id: {ctx.active_property_id or 'none'}",
        f"active_property_name: {ctx.active_property_reference or 'none'}",
        f"last_result_ids: {', '.join(last_ids) or 'none'}",
        f"seen_property_ids: {', '.join(seen_ids) or 'none'}",
        f"ordinal_map: {', '.join(names[:12]) or 'none'}",
        f"decisions_summary: {(ctx.decisions_summary or 'none')[:300]}",
        f"rolling_summary: {(ctx.rolling_summary or '')[:500]}",
        "Resolve 'it' / 'the first one' using active_property_id and ordinal_map only.",
        "=== END STRUCTURED STATE ===",
    ]
    return "\n".join(lines)


def _recent_user_messages(history: List[Dict[str, Any]], n: int) -> List[str]:
    users = [
        str(h.get("content") or "")
        for h in (history or [])
        if str(h.get("role") or "").lower() == "user" and str(h.get("content") or "").strip()
    ]
    return users[-n:] if n > 0 else []


def _structured_assistant_listing_hint(conversation: Any) -> Optional[str]:
    """Ids/names of last shown listings — never prose — for pronoun resolution."""
    prefs = dict(getattr(conversation, "preferences", None) or {})
    ids = [str(x) for x in (prefs.get("last_result_ids") or []) if x]
    if not ids:
        return None
    from app.services.property_context_service import load_property_context
    ctx = load_property_context(conversation)
    bits = []
    for i, pid in enumerate(ids[:5]):
        label = None
        for k, v in (ctx.reference_map or {}).items():
            if str(v) == pid and "property" in str(k).lower():
                label = k
                break
        name = None
        if ctx.active_property_id == pid:
            name = ctx.active_property_reference
        bits.append(f"{i + 1}. id={pid}" + (f" name={name}" if name else "") + (f" ({label})" if label else ""))
    return "Previously shown listings (structured): " + "; ".join(bits)


def build_grounded_llm_messages(
    *,
    system_prompt: str,
    conversation: Any,
    user_message: str,
    language: str = "en",
    facts_block: Optional[str] = None,
    include_listing_hint: bool = True,
) -> List[Dict[str, str]]:
    """
    system + STRUCTURED STATE + optional FACTS + last N USER messages + current user.
    Never includes earlier assistant prose.
    """
    n = int(getattr(settings, "PUBLIC_LLM_HISTORY_USER_TURNS", 3) or 3)
    history = list(getattr(conversation, "message_history", None) or [])
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": build_structured_state_block(conversation, language=language)},
    ]
    if facts_block:
        messages.append({"role": "system", "content": facts_block})
    if include_listing_hint:
        hint = _structured_assistant_listing_hint(conversation)
        if hint:
            messages.append({"role": "system", "content": hint})

    # Prior user turns (exclude current if already in history as last)
    prior = _recent_user_messages(history, n)
    cur = (user_message or "").strip()
    if prior and cur and prior[-1].strip() == cur:
        prior = prior[:-1]
    for u in prior[-n:]:
        messages.append({"role": "user", "content": u})
    messages.append({"role": "user", "content": cur})
    return messages


def factual_temperature() -> float:
    t = float(getattr(settings, "PUBLIC_LLM_FACTUAL_TEMPERATURE", 0.2) or 0.2)
    return min(0.2, max(0.0, t))


def log_grounding_metrics(
    *,
    verifier_failed: bool,
    claim_types: Optional[List[str]] = None,
    regeneration_used: bool = False,
    deterministic_fallback: bool = False,
    session_id: Optional[str] = None,
) -> None:
    """Structured log line (no personal data) for staging hallucination rate."""
    payload = {
        "event": "grounding_metrics",
        "verifier_failed": bool(verifier_failed),
        "claim_types": list(claim_types or [])[:20],
        "regeneration_used": bool(regeneration_used),
        "deterministic_fallback": bool(deterministic_fallback),
        "session_hash": (str(session_id or "")[:8] or None),
    }
    logger.info("grounding_metrics %s", json.dumps(payload, ensure_ascii=False))
