"""
Persistent property-aware conversation context for the public chatbot.

Memory decides WHICH property the user means.
Live Firestore/SQL retrieval decides WHAT is currently true.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

CONTEXT_KEY = "property_context"

# Pronoun / anaphoric references that mean "keep active property"
_ACTIVE_REF_PATTERNS = [
    r"\bit\b", r"\bits\b", r"\bthis\b", r"\bthat\b",
    r"\bthis property\b", r"\bthat property\b",
    r"\bthis villa\b", r"\bthat villa\b",
    r"\bthis condo\b", r"\bthat condo\b",
    r"\bthis one\b", r"\bthat one\b",
    r"\bthe property\b", r"\bthe villa\b", r"\bthe condo\b",
    r"\bprevious property\b", r"\bthe previous\b",
    r"\btej nieruchomo[sś]ci\b", r"\bta willa\b", r"\bten apartament\b",
    r"\bโครงการนี้\b", r"\bวิลล่านี้\b", r"\bอันนี้\b", r"\bอันนั้น\b",
]

_ORDINAL_MAP = {
    "first": 0, "1st": 0, "1": 0, "pierwsz": 0, "อันแรก": 0,
    "second": 1, "2nd": 1, "2": 1, "drug": 1, "อันที่สอง": 1,
    "third": 2, "3rd": 2, "3": 2, "trzeci": 2, "อันที่สาม": 2,
    "fourth": 3, "4th": 3, "4": 3,
    "fifth": 4, "5th": 4, "5": 4,
}

_FACT_HINTS = re.compile(
    r"(?:price|cost|how much|amenit|pool|parking|bedroom|bathroom|location|address|"
    r"area|size|sqm|status|available|ownership|freehold|leasehold|beach|sea view|"
    r"floor plan|brochure|yield|roi|developer|furniture|cena|basen|sypialn|"
    r"gym|fitness|units?\b|studios?|payment|instal+ment|deposit|security|balcon|garden|wi-?fi|pets?\b|"
    r"mall|supermarket|restaurant|airport|distance|how far|fees?\b|discount|complet|finish|built|handover|"
    r"furnish|maintenance|taxes|visa|euro|\beur\b|zloty|z[lł]ot|\bpln\b|\busd\b|dollar|cheaper|expensive|"
    r"how many|lift|elevator|storey|floors?\b|\bview|good (?:investment|deal)|recommend|guarantee|"
    r"owns?\b|title|lease|mortgage|financ|per sq|one-bedroom|two-bedroom|1-bed|2-bed|"
    r"ราคา|สระ|ที่จอด|ทำเล|ห้องนอน|มี\s)",
    re.IGNORECASE,
)


@dataclass
class PropertyContext:
    conversation_id: str = ""
    active_property_id: Optional[str] = None
    active_property_reference: Optional[str] = None
    recently_discussed_ids: List[str] = field(default_factory=list)
    reference_map: Dict[str, str] = field(default_factory=dict)
    comparison_set: List[str] = field(default_factory=list)
    user_preferences: Dict[str, Any] = field(default_factory=dict)
    decisions_summary: str = ""
    unresolved_questions: List[str] = field(default_factory=list)
    rolling_summary: str = ""
    turn_count: int = 0
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PropertyContext":
        if not data or not isinstance(data, dict):
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        ctx = cls(**filtered)
        ctx.recently_discussed_ids = list(ctx.recently_discussed_ids or [])
        ctx.comparison_set = list(ctx.comparison_set or [])
        ctx.unresolved_questions = list(ctx.unresolved_questions or [])
        ctx.reference_map = dict(ctx.reference_map or {})
        ctx.user_preferences = dict(ctx.user_preferences or {})
        return ctx


@dataclass
class ResolveResult:
    property_id: Optional[str] = None
    property_ids: List[str] = field(default_factory=list)
    ambiguous: bool = False
    clarification: Optional[str] = None
    source: str = "none"  # explicit_id | ordinal | active | name | ambiguous | none
    used_active_context: bool = False


def _cards_from_history_turn(turn: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract property card dicts from an assistant history turn."""
    cards: List[Dict[str, Any]] = []
    sd = turn.get("structured_data") or {}
    if isinstance(sd, dict):
        for p in sd.get("properties") or []:
            if isinstance(p, dict) and (p.get("property_id") or p.get("id")):
                cards.append(p)
    for p in turn.get("properties") or []:
        if isinstance(p, dict) and (p.get("property_id") or p.get("id")):
            cards.append(p)
    return cards


def hydrate_names_from_history(conversation: Any, ctx: PropertyContext) -> PropertyContext:
    """
    Always merge shown card names + ordinals from the latest assistant listing turn.
    Fixes follow-ups like 'tell me more about Fantasea Condo' when prefs lost names
    but cards are still in message history / UI.
    """
    history = list(getattr(conversation, "message_history", None) or [])
    for turn in reversed(history[-40:]):
        if not isinstance(turn, dict) or turn.get("role") != "assistant":
            continue
        cards = _cards_from_history_turn(turn)
        if not cards:
            # Also accept bare property_ids on structured_data
            sd = turn.get("structured_data") or {}
            raw_ids = list((sd.get("property_ids") or []) if isinstance(sd, dict) else [])
            if not raw_ids:
                continue
            ordered = []
            seen = set()
            for pid in raw_ids:
                s = str(pid).strip()
                if s and s not in seen:
                    seen.add(s)
                    ordered.append(s)
            if ordered:
                need_ordinals = not any(
                    ctx.reference_map.get(lbl) for lbl in ("first property", "second property")
                )
                ctx = touch_discussed(ctx, ordered, refresh_ordinals=need_ordinals)
                if not ctx.active_property_id:
                    ctx = set_active_property(ctx, ordered[0])
            return ctx

        ordered: List[str] = []
        names: Dict[str, str] = {}
        seen = set()
        for c in cards:
            pid = str(c.get("property_id") or c.get("id") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            ordered.append(pid)
            nm = str(c.get("name") or c.get("property_name") or "").strip()
            if nm:
                names[pid] = nm
        if not ordered:
            continue
        need_ordinals = not any(
            ctx.reference_map.get(lbl) for lbl in ("first property", "second property")
        )
        ctx = touch_discussed(ctx, ordered, names, refresh_ordinals=need_ordinals or bool(names))
        # Ensure ordinals always reflect this latest batch when we have cards
        ctx.reference_map.update(_build_ordinal_map(ordered))
        for pid, nm in names.items():
            ctx.reference_map[nm] = pid
            ctx.reference_map[nm.lower()] = pid
        if not ctx.active_property_id:
            ctx = set_active_property(ctx, ordered[0], reference=names.get(ordered[0]))
        return ctx
    return ctx


def recover_active_from_history(conversation: Any, ctx: PropertyContext) -> PropertyContext:
    """
    If active_property_id is missing, recover from the most recent assistant turn
    that showed property cards / IDs. Prevents lost follow-ups after location/maps turns.
    Always hydrates names/ordinals from history when possible.
    """
    ctx = hydrate_names_from_history(conversation, ctx)
    if ctx.active_property_id:
        return ctx
    history = list(getattr(conversation, "message_history", None) or [])
    for turn in reversed(history[-30:]):
        if not isinstance(turn, dict) or turn.get("role") != "assistant":
            continue
        pids: List[str] = []
        sd = turn.get("structured_data") or {}
        if isinstance(sd, dict):
            for pid in sd.get("property_ids") or []:
                if pid:
                    pids.append(str(pid).strip())
            for p in sd.get("properties") or []:
                if isinstance(p, dict) and p.get("property_id"):
                    pids.append(str(p["property_id"]).strip())
        for p in turn.get("properties") or []:
            if isinstance(p, dict) and p.get("property_id"):
                pids.append(str(p["property_id"]).strip())
            elif isinstance(p, str) and p.strip():
                pids.append(p.strip())
        # Dedupe preserve order
        seen = set()
        ordered = []
        for pid in pids:
            if pid and pid not in seen:
                seen.add(pid)
                ordered.append(pid)
        if len(ordered) == 1:
            return set_active_property(ctx, ordered[0])
        if len(ordered) > 1:
            ctx = touch_discussed(ctx, ordered, refresh_ordinals=True)
            # Prefer the first card of the latest listing batch as soft active for "it"
            return set_active_property(ctx, ordered[0])
    return ctx


def load_property_context(conversation: Any) -> PropertyContext:
    prefs = getattr(conversation, "preferences", None) or {}
    raw = prefs.get(CONTEXT_KEY) if isinstance(prefs, dict) else None
    ctx = PropertyContext.from_dict(raw if isinstance(raw, dict) else {})
    if not ctx.conversation_id and getattr(conversation, "id", None):
        ctx.conversation_id = str(conversation.id)
    # Hydrate legacy pointers into context if context is empty
    if not ctx.active_property_id:
        selected = prefs.get("selected_property_ids") or []
        last = prefs.get("last_result_ids") or []
        if selected:
            ctx.active_property_id = str(selected[0])
        elif last and len(last) == 1:
            ctx.active_property_id = str(last[0])
        elif last:
            # Multiple in last batch — keep ordinals; soft-active = first for pronoun follow-ups
            ctx.active_property_id = str(last[0])
    if not ctx.recently_discussed_ids:
        merged = []
        for src in (prefs.get("selected_property_ids") or [], prefs.get("last_result_ids") or [], prefs.get("seen_property_ids") or []):
            for pid in src:
                s = str(pid).strip()
                if s and s not in merged:
                    merged.append(s)
        ctx.recently_discussed_ids = merged[:20]
    if not ctx.reference_map and ctx.recently_discussed_ids:
        # Prefer last_result_ids order for ordinals when available
        batch = list(prefs.get("last_result_ids") or []) or list(ctx.recently_discussed_ids)
        ctx.reference_map.update(_build_ordinal_map([str(x) for x in batch if x]))
    # Always hydrate names/ordinals from latest shown cards (even if active already set)
    ctx = recover_active_from_history(conversation, ctx)
    return ctx


def save_property_context(conversation: Any, ctx: PropertyContext) -> Dict[str, Any]:
    """Write context into conversation.preferences and return full prefs dict."""
    ctx.updated_at = datetime.now(timezone.utc).isoformat()
    if getattr(conversation, "id", None):
        ctx.conversation_id = str(conversation.id)
    prefs = dict(getattr(conversation, "preferences", None) or {})
    prefs[CONTEXT_KEY] = ctx.to_dict()
    # Keep legacy keys in sync for existing intent_router paths
    if ctx.active_property_id:
        prefs["selected_property_ids"] = [ctx.active_property_id]
    if ctx.recently_discussed_ids:
        # last_result_ids = current result batch if mapped, else recent list head
        batch = []
        for label in ("first property", "second property", "third property", "fourth property", "fifth property"):
            pid = ctx.reference_map.get(label)
            if pid:
                batch.append(pid)
        prefs["last_result_ids"] = batch or list(ctx.recently_discussed_ids[:5])
        seen = list(prefs.get("seen_property_ids") or [])
        for pid in ctx.recently_discussed_ids:
            if pid not in seen:
                seen.append(pid)
        prefs["seen_property_ids"] = seen[-100:]
    if ctx.comparison_set:
        prefs["comparison_set"] = list(ctx.comparison_set)
    conversation.preferences = prefs
    return prefs


def _build_ordinal_map(ids: List[str]) -> Dict[str, str]:
    labels = [
        ("first property", "1st", "first one", "1"),
        ("second property", "2nd", "second one", "2"),
        ("third property", "3rd", "third one", "3"),
        ("fourth property", "4th", "fourth one", "4"),
        ("fifth property", "5th", "fifth one", "5"),
    ]
    out: Dict[str, str] = {}
    for i, pid in enumerate(ids[:5]):
        for label in labels[i]:
            out[label] = pid
        out[str(i + 1)] = pid
    return out


def _normalize_id(pid: Optional[str]) -> Optional[str]:
    if not pid:
        return None
    s = str(pid).strip()
    return s or None


def touch_discussed(
    ctx: PropertyContext,
    property_ids: List[str],
    names: Optional[Dict[str, str]] = None,
    *,
    refresh_ordinals: bool = False,
) -> PropertyContext:
    """Record properties shown/discussed without necessarily changing active focus."""
    names = names or {}
    for pid in property_ids:
        clean = _normalize_id(pid)
        if not clean:
            continue
        if clean in ctx.recently_discussed_ids:
            ctx.recently_discussed_ids.remove(clean)
        ctx.recently_discussed_ids.insert(0, clean)
        if names.get(clean):
            ctx.reference_map[names[clean].lower()] = clean
            ctx.reference_map[names[clean]] = clean
    ctx.recently_discussed_ids = ctx.recently_discussed_ids[:20]
    # Only rebuild first/second/... when given an explicit result batch (search/compare)
    ordered = [_normalize_id(p) for p in property_ids if _normalize_id(p)]
    if refresh_ordinals and ordered:
        ctx.reference_map.update(_build_ordinal_map(ordered))
    return ctx


def set_active_property(
    ctx: PropertyContext,
    property_id: str,
    reference: Optional[str] = None,
) -> PropertyContext:
    clean = _normalize_id(property_id)
    if not clean:
        return ctx
    ctx.active_property_id = clean
    if reference:
        ctx.active_property_reference = reference
        ctx.reference_map[reference.lower()] = clean
        ctx.reference_map[reference] = clean
    # Do NOT refresh ordinals — keep "first/second property" stable from last search batch
    ctx = touch_discussed(ctx, [clean], {clean: reference} if reference else None, refresh_ordinals=False)
    return ctx


def set_comparison_set(ctx: PropertyContext, property_ids: List[str]) -> PropertyContext:
    clean = [_normalize_id(p) for p in property_ids]
    ctx.comparison_set = [p for p in clean if p][:5]
    ctx = touch_discussed(ctx, ctx.comparison_set, refresh_ordinals=True)
    return ctx


def clear_active_if_new_conversation(ctx: PropertyContext, conversation_id: str) -> PropertyContext:
    """Scope guard: never inherit another conversation's active property."""
    if ctx.conversation_id and str(ctx.conversation_id) != str(conversation_id):
        return PropertyContext(conversation_id=str(conversation_id))
    ctx.conversation_id = str(conversation_id)
    return ctx


def _message_has_active_ref(msg: str) -> bool:
    return any(re.search(p, msg, flags=re.IGNORECASE) for p in _ACTIVE_REF_PATTERNS)


def _extract_explicit_prop_id(msg: str) -> Optional[str]:
    m = re.search(r"\b(prop-[a-z0-9\-]+)\b", msg, flags=re.IGNORECASE)
    return m.group(1).upper() if m else None


def _extract_ordinal_index(msg: str) -> Optional[int]:
    low = msg.lower()
    # Prefer explicit "the first property/properties/ones" before bare "first" scans
    m = re.search(
        r"\b(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+"
        r"(?:ones?|propert(?:y|ies)|villas?|condos?|listings?|ofert[ay]?|รายการ)?\b",
        low,
    )
    if m:
        return _ORDINAL_MAP.get(m.group(1))
    for key, idx in _ORDINAL_MAP.items():
        if key.isdigit():
            continue
        if key in low:
            # avoid matching "first" inside unrelated words poorly — word-ish check
            if re.search(rf"\b{re.escape(key)}\b", low) or key.startswith("อัน") or key.startswith("pierwsz") or key.startswith("drug") or key.startswith("trzec"):
                return idx
    return None


def resolve_name_against_context(name_query: str, ctx: PropertyContext, conversation: Any = None) -> Optional[str]:
    """
    Map a human property name (e.g. 'Mono Champaca') to a property_id using
    reference_map and recently shown cards — before failing a live DB lookup.
    """
    q = (name_query or "").strip().lower()
    if len(q) < 3:
        return None

    # 1. reference_map exact / substring (prefer longest key)
    best_pid = None
    best_len = 0
    for ref, pid in (ctx.reference_map or {}).items():
        ref_l = str(ref).lower().strip()
        if len(ref_l) < 3:
            continue
        if q == ref_l or q in ref_l or ref_l in q:
            if len(ref_l) >= best_len:
                best_len = len(ref_l)
                best_pid = str(pid)
    if best_pid:
        return best_pid

    # 2. Recently discussed IDs that look like the name (slug contains tokens)
    tokens = [t for t in re.sub(r"[^a-z0-9]+", " ", q).split() if len(t) >= 3]
    for pid in ctx.recently_discussed_ids or []:
        pid_l = str(pid).lower()
        if tokens and all(t in pid_l for t in tokens):
            return str(pid)
        if q.replace(" ", "-") in pid_l or q.replace(" ", "") in pid_l.replace("-", ""):
            return str(pid)

    # 3. Scan recent assistant cards in history
    if conversation is not None:
        history = list(getattr(conversation, "message_history", None) or [])
        for turn in reversed(history[-40:]):
            if not isinstance(turn, dict) or turn.get("role") != "assistant":
                continue
            cards = []
            sd = turn.get("structured_data") or {}
            if isinstance(sd, dict):
                cards.extend(sd.get("properties") or [])
            cards.extend(turn.get("properties") or [])
            for c in cards:
                if not isinstance(c, dict):
                    continue
                cname = str(c.get("name") or c.get("property_name") or "").lower()
                cid = str(c.get("property_id") or c.get("id") or "").strip()
                if not cid:
                    continue
                if cname and (q == cname or q in cname or cname in q):
                    return cid
                if tokens and all(t in (cname + " " + cid.lower()) for t in tokens):
                    return cid
    return None


def resolve_property_reference(
    message: str,
    ctx: PropertyContext,
    language: str = "en",
) -> ResolveResult:
    """
    Resolve which property ID the user means from the message + persistent context.
    Does NOT fetch facts — only identity resolution.
    """
    msg = (message or "").strip()
    if not msg:
        return ResolveResult()

    msg_low = msg.lower()

    # 1. Explicit property ID
    eid = _extract_explicit_prop_id(msg)
    if eid:
        return ResolveResult(property_id=eid, property_ids=[eid], source="explicit_id")

    # 2. Ordinal against reference_map / recently discussed / last batch
    ord_idx = _extract_ordinal_index(msg_low)
    if ord_idx is not None:
        batch = []
        for label in ("first property", "second property", "third property", "fourth property", "fifth property"):
            if label in ctx.reference_map:
                batch.append(ctx.reference_map[label])
        if not batch:
            batch = list(ctx.recently_discussed_ids)
        # Prefer last search order stored in reference_map values uniqueness
        if ord_idx < len(batch):
            pid = batch[ord_idx]
            return ResolveResult(property_id=pid, property_ids=[pid], source="ordinal")
        return ResolveResult(
            ambiguous=True,
            clarification=_clarification(language, reason="ordinal_oob"),
            source="ambiguous",
        )

    # 3. Named reference from reference_map (property titles discussed)
    # Prefer longer keys to avoid partial collisions
    name_hits: List[Tuple[str, str]] = []
    for ref, pid in sorted(ctx.reference_map.items(), key=lambda kv: len(str(kv[0])), reverse=True):
        ref_l = str(ref).lower().strip()
        if len(ref_l) < 4:
            continue
        if ref_l in msg_low or (len(ref_l) >= 6 and any(
            ref_l in part for part in re.findall(r"[a-z0-9][a-z0-9\s\-']{2,80}", msg_low)
        )):
            name_hits.append((pid, ref_l))
    # Dedupe by pid
    uniq_pids = []
    for pid, _ in name_hits:
        if pid not in uniq_pids:
            uniq_pids.append(pid)
    if len(uniq_pids) == 1:
        return ResolveResult(property_id=uniq_pids[0], property_ids=uniq_pids, source="name")
    if len(uniq_pids) > 1:
        return ResolveResult(
            ambiguous=True,
            property_ids=uniq_pids[:5],
            clarification=_clarification(language, reason="multi_name", candidates=uniq_pids),
            source="ambiguous",
        )

    # 4. Pronoun / "this property" → active context
    if _message_has_active_ref(msg_low) or _FACT_HINTS.search(msg_low):
        if ctx.active_property_id:
            return ResolveResult(
                property_id=ctx.active_property_id,
                property_ids=[ctx.active_property_id],
                source="active",
                used_active_context=True,
            )
        # Ambiguous: several recent, no active
        if len(ctx.recently_discussed_ids) > 1 and _message_has_active_ref(msg_low):
            return ResolveResult(
                ambiguous=True,
                property_ids=list(ctx.recently_discussed_ids[:3]),
                clarification=_clarification(language, reason="no_active", candidates=ctx.recently_discussed_ids[:3]),
                source="ambiguous",
            )
        if len(ctx.recently_discussed_ids) == 1:
            pid = ctx.recently_discussed_ids[0]
            return ResolveResult(property_id=pid, property_ids=[pid], source="active", used_active_context=True)

    # 5. Comparison set question ("compare them", "which of these")
    if any(k in msg_low for k in ["compare", "which of these", "between them", "porównaj", "เปรียบเทียบ"]) and ctx.comparison_set:
        return ResolveResult(property_ids=list(ctx.comparison_set), source="comparison")

    return ResolveResult(source="none")


def _clarification(language: str, reason: str, candidates: Optional[List[str]] = None) -> str:
    cands = candidates or []
    if language == "pl":
        if reason == "multi_name" or reason == "no_active":
            listed = ", ".join(cands) if cands else "ostatnio omawianych ofert"
            return (
                f"Mówisz o więcej niż jednej nieruchomości ({listed}). "
                "Napisz proszę nazwę lub 'pierwsza / druga oferta', żebym nie zgadywał."
            )
        return "Nie jestem pewien, o którą nieruchomość chodzi. Podaj nazwę lub numer oferty."
    if language == "th":
        return "ตอนนี้อาจหมายถึงหลายโครงการได้ กรุณาระบุชื่อโครงการ หรือบอกว่าเป็นรายการแรก/ที่สองครับ"
    if reason == "multi_name" or reason == "no_active":
        listed = ", ".join(cands) if cands else "the recently discussed listings"
        return (
            f"I want to be sure which property you mean ({listed}). "
            "Please name it, or say 'the first property' / 'the second property'."
        )
    return "Which property are you referring to? Please share the name or say 'the first/second property'."


def update_rolling_summary(
    ctx: PropertyContext,
    user_message: str,
    assistant_message: str = "",
    resolved_property_id: Optional[str] = None,
    *,
    structured: Optional[Dict[str, Any]] = None,
) -> PropertyContext:
    """
    Compact rolling summary from STRUCTURED data only (filters, shown ids, active id,
    questions asked). Never stores assistant prose or listing description text.
    `assistant_message` is kept for call-site compatibility and ignored as prose.
    """
    ctx.turn_count = int(ctx.turn_count or 0) + 1
    bits: List[str] = []
    st = structured if isinstance(structured, dict) else {}
    active = st.get("active_id") or resolved_property_id or ctx.active_property_id
    if active:
        bits.append(f"active={active}")
    shown = st.get("shown_ids") or st.get("last_result_ids")
    if shown:
        bits.append("shown=" + ",".join(str(x) for x in list(shown)[:8] if x))
    filters = st.get("filters") or st.get("active_filters")
    if isinstance(filters, dict) and filters:
        fb = ",".join(f"{k}:{v}" for k, v in filters.items() if v is not None)
        if fb:
            bits.append(f"filters={fb[:160]}")
    um = (user_message or "").strip().replace("\n", " ")
    q = st.get("question") or (um[:120] if um else "")
    if q:
        # Store question text only (user), never assistant wording
        bits.append(f"Q:{str(q)[:120]}")
    if resolved_property_id:
        bits.append(f"discussed:{resolved_property_id}")

    prev = (ctx.rolling_summary or "").strip()
    line = " | ".join(bits)
    combined = (prev + " || " + line).strip(" |") if prev else line
    max_chars = int(getattr(settings, "PROPERTY_CONTEXT_SUMMARY_CHARS", 4000) or 4000)
    if len(combined) > max_chars:
        combined = combined[-max_chars:]
    ctx.rolling_summary = combined

    if "?" in um and not resolved_property_id:
        qq = um[:180]
        if qq and qq not in ctx.unresolved_questions:
            ctx.unresolved_questions.append(qq)
            ctx.unresolved_questions = ctx.unresolved_questions[-10:]
    elif resolved_property_id and ctx.unresolved_questions:
        ctx.unresolved_questions = ctx.unresolved_questions[1:]

    active_s = ctx.active_property_id or "none"
    ctx.decisions_summary = (
        f"active={active_s}; recent={','.join(ctx.recently_discussed_ids[:5])}; "
        f"compare={','.join(ctx.comparison_set)}; turns={ctx.turn_count}"
    )
    return ctx


def assemble_llm_context_block(
    ctx: PropertyContext,
    recent_messages: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Structured reference memory only — IDs, filters summary, ordinals.
    Does NOT include recent assistant/user prose (callers pass user turns separately).
    `recent_messages` is accepted for API compatibility and ignored as prose.
    """
    _ = recent_messages  # intentionally unused — never feed prior chat prose
    ref_bits = []
    for label, pid in list((ctx.reference_map or {}).items())[:12]:
        if pid:
            ref_bits.append(f"{label}→{pid}")
    lines = [
        "=== STRUCTURED STATE (reference memory only — not factual source) ===",
        f"active_property_id: {ctx.active_property_id or 'none'}",
        f"active_property_reference: {ctx.active_property_reference or 'none'}",
        f"recently_discussed_ids: {', '.join(ctx.recently_discussed_ids[:10]) or 'none'}",
        f"comparison_set: {', '.join(ctx.comparison_set) or 'none'}",
        f"ordinal_map: {', '.join(ref_bits) or 'none'}",
        f"decisions_summary: {ctx.decisions_summary or 'none'}",
        f"unresolved_questions: {'; '.join(ctx.unresolved_questions) or 'none'}",
        f"rolling_summary: {(ctx.rolling_summary or '')[:800]}",
        "RULE: Resolve 'it' / 'the first one' using IDs and ordinal_map above. "
        "For prices/amenities/ownership/availability use only the FACTS block or tool results — "
        "never prior assistant text.",
    ]
    return "\n".join(lines)


def is_property_followup_message(message: str, ctx: PropertyContext) -> bool:
    """True when message likely continues discussing a known property."""
    if not message:
        return False
    msg_low = message.lower().strip()
    if ctx.active_property_id and (_message_has_active_ref(msg_low) or _FACT_HINTS.search(msg_low)):
        return True
    if _extract_ordinal_index(msg_low) is not None and (ctx.recently_discussed_ids or ctx.reference_map):
        return True
    if _extract_explicit_prop_id(msg_low):
        return True
    # Name hit in reference map
    for ref in ctx.reference_map:
        if len(str(ref)) >= 4 and str(ref).lower() in msg_low:
            return True
    return False


def fetch_authorized_property(db: Any, property_id: str) -> Tuple[Optional[Any], Optional[Dict[str, Any]], str]:
    """
    Retrieve current authorized property record.
    Returns (orm_or_adapter, card_dict, source_label).
    Never invents data.
    """
    from app.services.property_search import get_property_by_id, get_property_by_name, format_property_card

    pid = _normalize_id(property_id)
    if not pid:
        return None, None, "none"

    # Firestore first (production authority)
    if getattr(settings, "ENABLE_FIRESTORE_SYNC", True):
        try:
            from app.services.firestore_service import get_property_by_id_from_firestore
            fs = get_property_by_id_from_firestore(pid)
            if fs:
                return fs, fs if isinstance(fs, dict) else None, "firestore"
        except Exception as e:
            logger.warning(f"Firestore property fetch failed for {pid}: {e}")

    prop = get_property_by_id(db, pid) if db else None
    if not prop:
        prop = get_property_by_name(db, pid) if db else None
    if prop:
        try:
            card = format_property_card(prop)
        except Exception:
            card = {
                "property_id": getattr(prop, "property_id", pid),
                "name": getattr(prop, "property_name", None),
                "price": getattr(prop, "price_display", None),
            }
        return prop, card, "sql"
    return None, None, "missing"
