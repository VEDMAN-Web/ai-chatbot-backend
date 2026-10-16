"""
Resolve "Did you mean ...?" follow-ups stored in conversation.preferences.

pending_name_candidates: list of {property_id, name}
pending_name_query: original user query that produced the suggestions
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services.property_name_index import (
    distinctive_tokens,
    normalize_property_name,
)

PREF_CANDIDATES = "pending_name_candidates"
PREF_QUERY = "pending_name_query"

_AFFIRM = frozenset({
    "yes", "yeah", "yep", "yup", "correct", "right", "ok", "okay", "sure",
    "exactly", "that one", "this one", "the one",
    "tak", "zgadza sie", "zgadza się", "dobrze", "wlasnie", "właśnie", "pewnie",
    "ใช่", "ครับ", "ค่ะ", "ใช่ครับ", "ใช่ค่ะ", "ถูกต้อง",
    "haa", "ha", "haan", "han", "sahi", "barobar", "ho",
})

_ORDINAL_PATTERNS = [
    (re.compile(r"\b(?:the\s+)?first(?:\s+one)?\b|\b1(?:st)?\b|\bpierwsz\w*\b|แรก|ที่\s*1", re.I), 0),
    (re.compile(r"\b(?:the\s+)?second(?:\s+one)?\b|\b2(?:nd)?\b|\bdrug\w*\b|ที่สอง|ที่\s*2", re.I), 1),
    (re.compile(r"\b(?:the\s+)?third(?:\s+one)?\b|\b3(?:rd)?\b|\btrzec\w*\b|ที่สาม|ที่\s*3", re.I), 2),
]


@dataclass
class PendingNameResolution:
    """outcome of trying to resolve a Did-you-mean confirmation turn."""

    kind: str  # resolved | ask_which | clear_continue | none
    property_id: Optional[str] = None
    name: Optional[str] = None
    ask_text: Optional[str] = None


def _norm_msg(message: str) -> str:
    return normalize_property_name(message)


def is_affirmative_message(message: str) -> bool:
    raw = (message or "").strip().lower()
    # Check raw first (Thai/script affirmations are stripped by latin normalize)
    if raw in _AFFIRM:
        return True
    # Strip trailing politeness particles for Thai compounds
    for suffix in ("ครับ", "ค่ะ", "คะ"):
        if raw.endswith(suffix) and raw[: -len(suffix)].strip() in _AFFIRM:
            return True
    n = _norm_msg(message)
    if not n:
        return False
    if n in _AFFIRM:
        return True
    # short phrases like "yes please", "tak prosze"
    parts = n.split()
    if parts and parts[0] in _AFFIRM and len(parts) <= 3:
        return True
    return False


def _looks_like_new_request(message: str) -> bool:
    """True when the user is clearly starting a new search / question."""
    raw = (message or "").strip().lower()
    if len(raw) > 80:
        return True
    starters = (
        "show me", "find", "search", "looking for", "i want", "what about",
        "tell me about", "brochure", "price of", "how much",
        "pokaz", "pokaż", "szukam", "chce", "chcę", "ile kosztuje",
        "หา", "แสดง", "ราคา", "อยากได้",
    )
    if any(raw.startswith(s) for s in starters):
        return True
    if "?" in raw or "؟" in raw:
        return True
    return False


def _match_candidate_by_name(message: str, candidates: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    n = _norm_msg(message)
    if not n:
        return None
    # Exact / contained normalized name
    for c in candidates:
        cn = normalize_property_name(c.get("name") or "")
        if not cn:
            continue
        if n == cn or n in cn or cn in n:
            return c
    q_toks = distinctive_tokens(message)
    if not q_toks:
        return None
    hits = []
    for c in candidates:
        c_toks = set(distinctive_tokens(c.get("name") or ""))
        if q_toks and all(t in c_toks for t in q_toks):
            hits.append(c)
    if len(hits) == 1:
        return hits[0]
    return None


def _match_ordinal(message: str, n_cands: int) -> Optional[int]:
    raw = (message or "").strip()
    # bare number
    if re.fullmatch(r"[123]", raw):
        idx = int(raw) - 1
        return idx if 0 <= idx < n_cands else None
    for pat, idx in _ORDINAL_PATTERNS:
        if pat.search(raw) and 0 <= idx < n_cands:
            return idx
    return None


def clear_pending_name_candidates(preferences: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    prefs = dict(preferences or {})
    prefs.pop(PREF_CANDIDATES, None)
    prefs.pop(PREF_QUERY, None)
    return prefs


def store_pending_name_candidates(
    preferences: Optional[Dict[str, Any]],
    *,
    query: Optional[str],
    candidates: List[Dict[str, str]],
) -> Dict[str, Any]:
    prefs = dict(preferences or {})
    clean = []
    for c in candidates or []:
        pid = str(c.get("property_id") or "").strip()
        name = str(c.get("name") or "").strip()
        if pid and name:
            clean.append({"property_id": pid, "name": name})
    if clean:
        prefs[PREF_CANDIDATES] = clean[:3]
        prefs[PREF_QUERY] = (query or "").strip() or None
    else:
        prefs = clear_pending_name_candidates(prefs)
    return prefs


def try_resolve_pending_name(
    message: str,
    preferences: Optional[Dict[str, Any]],
    language: str = "en",
) -> PendingNameResolution:
    """
    Attempt to resolve a confirmation follow-up against pending_name_candidates.
    - none: no pending state (caller continues as normal; "yes" alone must not pick a property)
    - clear_continue: user started a new request; pending cleared by caller
    - ask_which: affirmative but multiple candidates
    - resolved: pick one candidate
    """
    prefs = preferences or {}
    candidates = prefs.get(PREF_CANDIDATES) or []
    if not isinstance(candidates, list) or not candidates:
        return PendingNameResolution(kind="none")

    # Sanitize
    clean = []
    for c in candidates:
        if isinstance(c, dict) and c.get("property_id") and c.get("name"):
            clean.append({"property_id": str(c["property_id"]), "name": str(c["name"])})
    if not clean:
        return PendingNameResolution(kind="none")

    # Ordinal selection
    ord_idx = _match_ordinal(message, len(clean))
    if ord_idx is not None:
        hit = clean[ord_idx]
        return PendingNameResolution(
            kind="resolved",
            property_id=hit["property_id"],
            name=hit["name"],
        )

    # Name of one candidate
    by_name = _match_candidate_by_name(message, clean)
    if by_name:
        return PendingNameResolution(
            kind="resolved",
            property_id=by_name["property_id"],
            name=by_name["name"],
        )

    # Affirmative
    if is_affirmative_message(message):
        if len(clean) == 1:
            hit = clean[0]
            return PendingNameResolution(
                kind="resolved",
                property_id=hit["property_id"],
                name=hit["name"],
            )
        return PendingNameResolution(
            kind="ask_which",
            ask_text=_ask_which_text(language, clean),
        )

    # New question / different property name -> drop pending, continue
    if _looks_like_new_request(message):
        return PendingNameResolution(kind="clear_continue")

    # Short message that is not affirm/ordinal/name: if it looks like another
    # property token search, clear and continue; else ask which when multi
    toks = distinctive_tokens(message)
    if toks and not is_affirmative_message(message):
        return PendingNameResolution(kind="clear_continue")

    if len(clean) > 1:
        return PendingNameResolution(
            kind="ask_which",
            ask_text=_ask_which_text(language, clean),
        )
    return PendingNameResolution(kind="clear_continue")


def _ask_which_text(language: str, candidates: List[Dict[str, str]]) -> str:
    lines = "\n".join(f"{i+1}. {c['name']}" for i, c in enumerate(candidates))
    lang = (language or "en").lower()
    if lang == "th":
        return (
            "คุณหมายถึงรายการไหนครับ ตอบหมายเลข หรือชื่อโครงการได้ครับ:\n"
            f"{lines}"
        )
    if lang == "pl":
        return (
            "Którą ofertę masz na myśli? Podaj numer lub nazwę:\n"
            f"{lines}"
        )
    return (
        "Which one did you mean? Reply with the number or the project name:\n"
        f"{lines}"
    )
