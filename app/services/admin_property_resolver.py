"""
Admin property-name resolution via the read-only property_name_index.

Messages like "change price of pirak villa to 5 million" must resolve to a real
listing before any PendingAction is proposed. Never invents property ids.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.services.property_name_index import (
    FILLER_TOKENS,
    GENERIC_TOKENS,
    NameIndexEntry,
    distinctive_tokens,
    ensure_name_index_from_firestore,
    match_property_name,
    normalize_property_name,
    strip_filler_words,
)

logger = logging.getLogger(__name__)

# High bar for admin fuzzy confident match (index default is 0.86)
ADMIN_FUZZY_MIN = 0.88

# Extra conversational / question words that must never be treated as property names
ADMIN_FILLER_EXTRA = frozenset({
    "you", "do", "does", "did", "which", "what", "who", "where", "when", "how",
    "private", "ok", "okay", "yes", "no", "please", "pls", "just", "also",
    "can", "could", "would", "should", "will", "shall", "may", "might",
    "is", "are", "was", "were", "be", "been", "being",
    "i", "we", "they", "he", "she", "it", "its", "our", "your", "their",
    "change", "update", "set", "make", "put", "edit", "modify", "zmien", "aktualizuj",
    "price", "cena", "status", "listing", "property", "properties", "of", "for", "on",
    "to", "from", "with", "and", "or", "a", "an", "the", "this", "that", "these", "those",
    "show", "more", "all", "any", "some", "one", "two", "three",
    "million", "mln", "thb", "baht", "usd",
})

PREF_CANDIDATES = "admin_pending_name_candidates"
PREF_QUERY = "admin_pending_name_query"
PREF_WRITE_INTENT = "admin_pending_write_intent"

_EXTRACT_PATTERNS = [
    # change/update/set price|status of|for X to ...
    re.compile(
        r"(?:change|update|set|edit|modify|zmien|aktualizuj)\s+"
        r"(?:the\s+)?(?:price|status|cena|listing)?\s*"
        r"(?:of|for|on)\s+(.+?)(?:\s+(?:to|na|=|into)\b|\s*$)",
        re.I,
    ),
    # change/update X('s) price/status ...
    re.compile(
        r"(?:change|update|set|edit|zmien|aktualizuj)\s+(.+?)(?:'s)?\s+"
        r"(?:price|status|cena)\b",
        re.I,
    ),
    # price of X to / for X to
    re.compile(
        r"(?:price|cena|status)\s+(?:of|for)\s+(.+?)(?:\s+(?:to|na|=)\b|\s*$)",
        re.I,
    ),
    # delete/remove X
    re.compile(
        r"(?:delete|remove|archive|usun|usuń)\s+(?:the\s+)?(?:listing\s+|property\s+)?(.+?)(?:\s*$)",
        re.I,
    ),
]


@dataclass
class AdminNameResolution:
    """Outcome of admin property name resolution."""

    kind: str  # resolved | ambiguous | not_found | filler | empty
    query: Optional[str] = None
    property_id: Optional[str] = None
    official_name: Optional[str] = None
    candidates: List[Dict[str, str]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    message: Optional[str] = None
    reason: str = ""


def _is_filler_only(text: Optional[str]) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    norm = normalize_property_name(raw)
    if not norm:
        return True
    tokens = [t for t in norm.split() if t]
    if not tokens:
        return True
    blocked = FILLER_TOKENS | GENERIC_TOKENS | ADMIN_FILLER_EXTRA
    distinctive = [
        t for t in tokens
        if t not in blocked and len(t) >= 2 and not t.isdigit()
    ]
    return not distinctive


def extract_property_name_phrase(message: Optional[str]) -> Optional[str]:
    """
    Pull the likely listing name out of an admin write command.
    Returns None when nothing usable remains after stripping fillers.
    """
    if not message or not str(message).strip():
        return None
    raw = str(message).strip()

    # Strip trailing price/status values that confuse the name
    cleaned = re.sub(
        r"\bto\s+\d[\d,]*(?:\.\d+)?\s*(?:million|mln|m\b|thb|baht)?.*$",
        "",
        raw,
        flags=re.I,
    ).strip() or raw

    phrase: Optional[str] = None
    for pat in _EXTRACT_PATTERNS:
        m = pat.search(cleaned if cleaned else raw)
        if m:
            phrase = (m.group(1) or "").strip(" .,;:'\"")
            if phrase:
                break

    if not phrase:
        # Fallback: strip command verbs / field words, keep remainder
        stripped = strip_filler_words(raw)
        # Drop admin command vocabulary
        parts = [
            t for t in normalize_property_name(stripped).split()
            if t and t not in ADMIN_FILLER_EXTRA and t not in FILLER_TOKENS
        ]
        # Keep generics like "villa" if paired with a brand token
        phrase = " ".join(parts) if parts else None

    if not phrase:
        return None

    # Remove residual trailing "to 5 million" style crumbs
    phrase = re.sub(
        r"\b(?:to|na|=)\s+\d.*$",
        "",
        phrase,
        flags=re.I,
    ).strip(" .,;:'\"")
    # Only a price crumb at the very end ("... 5 million", "... 500 THB") is dropped. Digits that belong to the
    # name ("3 Villas - Lamai", "Rawai 88 Residence", "Villa 12") stay.
    phrase = re.sub(
        r"\s+\d[\d,]*(?:\.\d+)?\s*(?:million|mln|m|k|thb|baht)\s*$",
        "",
        phrase,
        flags=re.I,
    ).strip(" .,;:'\"")

    if _is_filler_only(phrase):
        return None
    return phrase or None


def format_which_one(
    candidates: Sequence[Dict[str, str]],
    language: str = "en",
) -> str:
    lines = []
    for i, c in enumerate(candidates[:8], start=1):
        pid = c.get("property_id") or ""
        name = c.get("name") or pid
        lines.append(f"{i}) **{name}** (`{pid}`)")
    body = "\n".join(lines)
    if (language or "en").lower().startswith("pl"):
        return f"Którą ofertę masz na myśli?\n{body}\n\nOdpowiedz numerem (np. `1`)."
    return f"Which one?\n{body}\n\nReply with the number (e.g. `1`)."


def format_not_found(
    query: str,
    suggestions: Sequence[str],
    language: str = "en",
) -> str:
    tips = list(suggestions or [])[:3]
    if (language or "en").lower().startswith("pl"):
        msg = f"Nie znalazłem nieruchomości o nazwie **{query}**."
        if tips:
            msg += " Najbliższe nazwy: " + "; ".join(f"**{t}**" for t in tips) + "."
        msg += " Nie zaproponuję żadnej zmiany bez realnego ID."
        return msg
    msg = f'I couldn\'t find a property named **{query}**.'
    if tips:
        msg += " Closest names: " + "; ".join(f"**{t}**" for t in tips) + "."
    msg += " I will not invent an id or propose a change."
    return msg


def _entry_to_cand(e: NameIndexEntry) -> Dict[str, str]:
    return {"property_id": e.doc_id, "name": e.display_name}


def resolve_admin_property_name(
    query_or_message: Optional[str],
    *,
    language: str = "en",
    already_extracted: bool = False,
    ensure_index: bool = True,
    db: Any = None,
) -> AdminNameResolution:
    """
    Resolve a property name (or full admin message) against the live name index.

    - Exactly one confident match → resolved (official name + real id)
    - Several close matches → ambiguous (ask which; propose nothing)
    - No match → not_found (+ up to 3 closest names); never invents an id
    - Filler-only → filler
    """
    raw = (query_or_message or "").strip()
    if not raw:
        return AdminNameResolution(kind="empty", reason="empty")

    phrase = raw if already_extracted else extract_property_name_phrase(raw)
    if phrase is None:
        # If caller passed a short token that is filler, say so; else try raw
        if _is_filler_only(raw):
            return AdminNameResolution(
                kind="filler",
                query=raw,
                reason="filler",
                message=(
                    "That looks like a question/filler word, not a property name. "
                    "Name the listing (e.g. `Pirak Villa`) before I propose a change."
                    if not (language or "en").lower().startswith("pl")
                    else "To wygląda na słowo wypełniające, nie nazwę oferty. Podaj konkretną nazwę nieruchomości."
                ),
            )
        phrase = raw

    if _is_filler_only(phrase):
        return AdminNameResolution(
            kind="filler",
            query=phrase,
            reason="filler",
            message=(
                "That looks like a question/filler word, not a property name. "
                "Name the listing before I propose a change."
                if not (language or "en").lower().startswith("pl")
                else "To wygląda na słowo wypełniające, nie nazwę oferty."
            ),
        )

    if ensure_index:
        try:
            ensure_name_index_from_firestore(target="user")
        except Exception as e:
            logger.debug(f"Name index ensure skipped: {e}")

    mr = match_property_name(phrase)

    # Confident single hit — still require fuzzy score high enough when reason is fuzzy
    if mr.confident is not None:
        # Ambiguous exact_multi with several near-identical aliases: ask if >1 candidate
        if mr.reason == "exact_multi" and mr.candidates and len(mr.candidates) > 1:
            # Only ask when display names differ meaningfully
            names = {normalize_property_name(c.display_name) for c in mr.candidates}
            if len(names) > 1:
                cands = [_entry_to_cand(c) for c in mr.candidates[:5]]
                return AdminNameResolution(
                    kind="ambiguous",
                    query=phrase,
                    candidates=cands,
                    reason="exact_multi",
                    message=format_which_one(cands, language=language),
                )
        return AdminNameResolution(
            kind="resolved",
            query=phrase,
            property_id=mr.confident.doc_id,
            official_name=mr.confident.display_name,
            reason=mr.reason or "confident",
        )

    # Non-confident: candidates from index (close matches) → ask which
    cands = [_entry_to_cand(c) for c in (mr.candidates or [])[:5]]
    if cands:
        # If only one candidate and it was near-confident fuzzy, still ask? Task says
        # "Several close matches: ask". One close match without confident → treat as
        # not confident enough → show as suggestion / ask if score gap unclear.
        # Prefer asking when 2+; for 1 soft match still ask confirmation via "Which one?"
        if len(cands) >= 2:
            return AdminNameResolution(
                kind="ambiguous",
                query=phrase,
                candidates=cands,
                suggestions=[c["name"] for c in cands],
                reason=mr.reason or "ambiguous",
                message=format_which_one(cands, language=language),
            )
        # Single soft candidate — present as not confident: ask which (1 option) or not_found with tip
        return AdminNameResolution(
            kind="ambiguous",
            query=phrase,
            candidates=cands,
            suggestions=[c["name"] for c in cands],
            reason="soft_single",
            message=format_which_one(cands, language=language),
        )

    # No candidates — try to surface 3 closest via a looser scan of the index
    suggestions = list(mr.suggestions or [])
    if not suggestions:
        suggestions = _closest_display_names(phrase, limit=3)

    return AdminNameResolution(
        kind="not_found",
        query=phrase,
        suggestions=suggestions,
        reason=mr.reason or "no_match",
        message=format_not_found(phrase, suggestions, language=language),
    )


def _closest_display_names(phrase: str, limit: int = 3) -> List[str]:
    """Best-effort closest names when match() returns empty candidates."""
    try:
        from difflib import SequenceMatcher
        from app.services.property_name_index import get_property_name_index

        idx = get_property_name_index()
        q = normalize_property_name(phrase)
        if not q or len(q) < 3:
            return []
        scored = []
        with idx._lock:  # noqa: SLF001
            entries = list(idx._entries)
        for e in entries:
            best = 0.0
            for a in e.aliases_norm or [normalize_property_name(e.display_name)]:
                if not a:
                    continue
                best = max(best, SequenceMatcher(None, q, a).ratio())
            if best >= 0.55:
                scored.append((best, e.display_name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out: List[str] = []
        seen = set()
        for _s, name in scored:
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        logger.debug(f"closest names skipped: {e}")
        return []


def store_admin_pending_resolution(
    preferences: Optional[Dict[str, Any]],
    *,
    query: Optional[str],
    candidates: List[Dict[str, str]],
    write_intent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    prefs = dict(preferences or {})
    clean = []
    for c in candidates or []:
        pid = str(c.get("property_id") or "").strip()
        name = str(c.get("name") or "").strip()
        if pid and name:
            clean.append({"property_id": pid, "name": name})
    if clean:
        prefs[PREF_CANDIDATES] = clean[:8]
        prefs[PREF_QUERY] = (query or "").strip() or None
        if write_intent:
            prefs[PREF_WRITE_INTENT] = dict(write_intent)
    else:
        prefs.pop(PREF_CANDIDATES, None)
        prefs.pop(PREF_QUERY, None)
        prefs.pop(PREF_WRITE_INTENT, None)
    return prefs


def clear_admin_pending_resolution(preferences: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    prefs = dict(preferences or {})
    prefs.pop(PREF_CANDIDATES, None)
    prefs.pop(PREF_QUERY, None)
    prefs.pop(PREF_WRITE_INTENT, None)
    return prefs


def try_resolve_admin_pending_choice(
    message: Optional[str],
    preferences: Optional[Dict[str, Any]],
) -> AdminNameResolution:
    """
    Resolve a numbered / named follow-up after an ambiguous admin ask.
    """
    prefs = preferences or {}
    cands = prefs.get(PREF_CANDIDATES) or []
    if not isinstance(cands, list) or not cands:
        return AdminNameResolution(kind="empty", reason="no_pending")

    clean = [
        {"property_id": str(c["property_id"]), "name": str(c["name"])}
        for c in cands
        if isinstance(c, dict) and c.get("property_id") and c.get("name")
    ]
    if not clean:
        return AdminNameResolution(kind="empty", reason="no_pending")

    raw = (message or "").strip()
    # bare number 1..N
    m = re.fullmatch(r"(\d{1,2})\)?", raw)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(clean):
            hit = clean[idx]
            return AdminNameResolution(
                kind="resolved",
                property_id=hit["property_id"],
                official_name=hit["name"],
                query=prefs.get(PREF_QUERY),
                reason="ordinal",
            )

    # "1)" / "option 2" / "first"
    ord_m = re.search(r"\b(?:option\s+)?(\d{1,2})\)?\b", raw, re.I)
    if ord_m and len(raw.split()) <= 4:
        idx = int(ord_m.group(1)) - 1
        if 0 <= idx < len(clean):
            hit = clean[idx]
            return AdminNameResolution(
                kind="resolved",
                property_id=hit["property_id"],
                official_name=hit["name"],
                query=prefs.get(PREF_QUERY),
                reason="ordinal",
            )

    n = normalize_property_name(raw)
    for c in clean:
        cn = normalize_property_name(c["name"])
        if n and cn and (n == cn or n in cn or cn in n):
            return AdminNameResolution(
                kind="resolved",
                property_id=c["property_id"],
                official_name=c["name"],
                query=prefs.get(PREF_QUERY),
                reason="name_pick",
            )

    return AdminNameResolution(
        kind="ambiguous",
        candidates=clean,
        query=prefs.get(PREF_QUERY),
        reason="still_ambiguous",
        message=format_which_one(clean),
    )


def get_admin_pending_write_intent(
    preferences: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    prefs = preferences or {}
    intent = prefs.get(PREF_WRITE_INTENT)
    return dict(intent) if isinstance(intent, dict) else None
