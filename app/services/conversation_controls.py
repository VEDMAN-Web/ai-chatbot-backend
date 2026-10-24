"""
Conversation controls for the public chatbot.

Filters are removable and replaceable. Locations must be known places.
General buying questions use a reviewed explanation, not a repeated property card.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Reviewed general guidance. Not legal advice. Not generated from model memory.
FOREIGN_OWNERSHIP_NOTE = (
    "This is general information, not legal advice. "
    "In Thailand, foreigners generally cannot own land. "
    "They can own a condominium unit freehold within the 49% foreign quota of that building, "
    "lease land (typically 30 years), or hold property through structures that need a lawyer's advice. "
    "Nationality (Indian, European, or other) generally does not change the land rule. "
    "Funds-transfer proof, visas, and tax residency depend on the person, so a lawyer or your advisor must confirm."
)

_NOT_A_PLACE = frozenset({
    "new", "newest", "latest", "new new", "new build", "off plan", "off-plan",
    "under construction", "pre sale", "pre-sale", "presale", "ready", "completed",
    "free", "free properties", "cheapest", "current", "current chat", "this chat",
    "the ones you showed", "previous", "previous results", "earlier", "earlier listings",
    "the first ones", "chat", "properties", "property", "thailand", "listings",
})

_RESET_PHRASES = (
    "forget my requirements", "forget everything", "forgot all my requirements",
    "forgot my requirements", "start over", "new search", "clear filters", "clear my filters",
    "reset filters", "ignore my earlier requirements", "never mind",
    "zapomnij o wymaganiach", "zapomnij wszystko", "zacznij od nowa", "wyczyść filtry",
    "wyczysc filtry", "zresetuj filtry", "nieważne", "niewazne",
    "ลืมความต้องการ", "ลืมทั้งหมด", "เริ่มใหม่", "ล้างตัวกรอง", "ไม่เป็นไร",
    "मेरी जरूरतें भूल जाओ", "सब भूल जाओ", "फिर से शुरू", "फ़िल्टर हटाओ", "फिल्टर हटाओ",
    "મારી જરૂરિયાતો ભૂલી જાઓ", "બધું ભૂલી જાઓ", "ફરીથી શરૂ", "ફિલ્ટર સાફ",
)

_FRESH_TOPIC_PHRASES = (
    "explore luxury properties",
    "show all featured luxury properties",
    "show all featured",
    "show villas in phuket",
    "how does buying property in thailand work",
    "how does property ownership",
)

_STATUS_NEW = (
    "off-plan", "off plan", "pre-sale", "presale", "pre sale",
    "under construction", "new build", "newest", "latest", "planned",
)
_STATUS_READY = ("ready to move in", "ready to move", "completed", "ready")


def _low(message: str) -> str:
    return (message or "").strip().lower()


def is_full_reset(message: str) -> bool:
    low = _low(message)
    if not low:
        return False
    if low in {"reset", "start over", "new search", "never mind", "nevermind"}:
        return True
    return any(p in low for p in _RESET_PHRASES)


def is_fresh_topic(message: str) -> bool:
    low = _low(message)
    return any(p in low for p in _FRESH_TOPIC_PHRASES)


def slots_to_clear(message: str) -> Set[str]:
    """Single-filter removals. 'all' means every active search slot."""
    low = _low(message)
    if is_full_reset(message):
        return {"all"}
    cleared: Set[str] = set()
    if any(p in low for p in (
        "any location", "no location", "anywhere", "any area",
        "bez lokalizacji", "ใดก็ได้", "कोई भी जगह", "કોઈ પણ જગ્યા",
    )):
        cleared.add("location")
    if any(p in low for p in (
        "no budget limit", "any budget", "any price", "no price limit", "all prices",
        "bez limitu", "ไม่จำกัดงบ", "कोई बजट नहीं", "બજેટ મર્યાદા નથી",
    )):
        cleared.add("budget")
    if any(p in low for p in (
        "any bedrooms", "any number of bedrooms", "any beds", "remove the bedroom",
        "bez sypialni", "ห้องนอนไม่จำกัด",
    )):
        cleared.add("bedrooms")
    if any(p in low for p in (
        "remove the pool filter", "no pool filter", "any pool", "without pool filter",
        "usuń filtr basenu", "usun filtr basenu",
    )):
        cleared.add("pool")
    if any(p in low for p in ("any type", "any property type", "remove property type")):
        cleared.add("property_type")
    return cleared


def clear_stale_for_fresh_topic(prefs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A general chip starts a fresh search. The button text can set location or type afterwards."""
    out = dict(prefs or {})
    for key in (
        "budget_max", "budget_display", "max_price", "location", "bedrooms",
        "bedrooms_num", "min_bedrooms", "max_bedrooms", "property_context",
        "selected_property_ids", "active_property_id", "focus_summary", "focus_validated",
        "last_result_ids", "last_shown_names",
    ):
        out.pop(key, None)
    active = dict(out.get("active_filters") or {})
    for key in ("location", "max_price", "bedrooms", "min_bedrooms", "max_bedrooms", "property_type"):
        active.pop(key, None)
    out["active_filters"] = active
    return out


def clear_filter_keys(prefs: Optional[Dict[str, Any]], slots: Iterable[str]) -> Dict[str, Any]:
    out = dict(prefs or {})
    slot_set = set(slots)
    if "all" in slot_set:
        for key in (
            "active_filters", "budget_max", "budget_display", "max_price", "location",
            "property_type", "bedrooms", "bedrooms_num", "min_bedrooms", "max_bedrooms",
            "has_pool", "focus_summary", "what_we_fetched", "focus_validated",
            "last_no_match_text", "property_context", "selected_property_ids",
            "last_result_ids", "last_shown_names", "active_property_id",
        ):
            out.pop(key, None)
        return out
    active = dict(out.get("active_filters") or {})
    if "location" in slot_set:
        out.pop("location", None)
        active.pop("location", None)
    if "budget" in slot_set:
        for key in ("budget_max", "budget_display", "max_price"):
            out.pop(key, None)
        active.pop("max_price", None)
    if "bedrooms" in slot_set:
        for key in ("bedrooms", "bedrooms_num", "min_bedrooms", "max_bedrooms"):
            out.pop(key, None)
        active.pop("bedrooms", None)
        active.pop("min_bedrooms", None)
        active.pop("max_bedrooms", None)
    if "pool" in slot_set:
        out.pop("has_pool", None)
        active.pop("has_pool", None)
        feats = [f for f in (out.get("features") or []) if "pool" not in str(f).lower()]
        out["features"] = feats
    if "property_type" in slot_set:
        out.pop("property_type", None)
        active.pop("property_type", None)
    out["active_filters"] = active
    return out


def cleared_confirmation(slots: Iterable[str], language: str = "en") -> str:
    slot_set = set(slots)
    if "all" in slot_set:
        if language == "pl":
            return "Wyczyściłem budżet, lokalizację, typ, sypialnie i wybraną ofertę."
        if language == "th":
            return "ล้างงบประมาณ ทำเล ประเภท ห้องนอน และโครงการที่กำลังดูแล้วครับ"
        return "Cleared your budget, location, property type, bedrooms, and the property we were looking at."
    labels = []
    for key, en in (
        ("location", "location"),
        ("budget", "budget"),
        ("bedrooms", "bedrooms"),
        ("pool", "pool filter"),
        ("property_type", "property type"),
    ):
        if key in slot_set:
            labels.append(en)
    if not labels:
        return "Cleared that filter."
    return "Cleared " + ", ".join(labels) + "."


def is_known_location(name: Optional[str]) -> bool:
    raw = (name or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low in _NOT_A_PLACE or any(part in _NOT_A_PLACE for part in low.split()):
        return False
    from app.services.intent_router import LOCATION_ALIASES

    for kw, canon in LOCATION_ALIASES:
        canon_l = canon.lower()
        if low == kw or low == canon_l or canon_l in low or kw in low:
            return True
    return False


def project_status_from_message(message: str) -> Optional[str]:
    low = _low(message)
    if not low:
        return None
    # "new new properties", "newest", "latest", "off-plan" — never a place name.
    if re.search(r"\bnew(?:\s+new)?\s+propert", low) or any(p in low for p in _STATUS_NEW):
        return "Off-plan"
    if re.search(r"\bnew\b", low) and re.search(r"\b(propert|listing|build|project)", low):
        return "Off-plan"
    if any(p in low for p in _STATUS_READY) and "already" not in low:
        # "ready" alone is too weak unless it is about the property status
        if any(p in low for p in ("ready to move", "completed", "ready properties", "ready homes")):
            return "Ready"
    return None


def price_is_exclusive(message: str) -> bool:
    """'under / below' is strictly less than the cap. 'up to' includes it."""
    low = _low(message)
    if not low:
        return False
    inclusive = bool(re.search(
        r"\b(up to|upto|at most|maximum|max(?:imum)?|nie więcej niż|nie wiecej niz|ไม่เกิน)\b",
        low,
    ))
    exclusive = bool(re.search(
        r"\b(under|below|less than|cheaper than|poniżej|ponizej|mniej niż|mniej niz|ต่ำกว่า)\b",
        low,
    ))
    if exclusive and not inclusive:
        return True
    return False


def format_budget_phrase(amount_thb: float, exclusive: bool = False, with_usd: bool = True) -> str:
    """One budget phrase for the whole chat. 'under' is strict; 'up to' includes the cap."""
    amount = float(amount_thb or 0)
    millions = amount / 1_000_000
    amount_label = f"{millions:g}M THB" if millions >= 1 else f"{int(amount):,} THB"
    word = "under" if exclusive else "up to"
    phrase = f"{word} {amount_label}"
    if with_usd and amount:
        usd_m = amount / 35 / 1_000_000
        phrase = f"{phrase} (about ${usd_m:.2f}M)"
    return phrase


def price_within_budget(price: Optional[float], max_price: Optional[float], exclusive: bool) -> bool:
    if price is None or max_price is None:
        return False
    if exclusive:
        return float(price) < float(max_price)
    return float(price) <= float(max_price)


def is_free_or_cheapest(message: str) -> bool:
    low = _low(message)
    if "freehold" in low:
        return bool(re.search(r"\b(cheapest|lowest price|very cheap)\b", low))
    return bool(re.search(
        r"\b(free properties|free listings|properties for free|very cheap|cheapest|lowest price|lowest-priced)\b"
        r"|(^|\s)free(\s+propert|\s+listing)",
        low,
    ))


def is_recall_shown(message: str) -> bool:
    low = _low(message)
    return any(p in low for p in (
        "current chat", "this chat", "the ones you showed", "properties you showed",
        "previous results", "earlier listings", "the first ones", "you showed",
        "showed in current", "already shown",
    ))


def is_foreign_ownership_question(message: str) -> bool:
    low = _low(message)
    if not low:
        return False
    buy = bool(re.search(
        r"\b(buy|own|purchase|mortgage|tax|taxes|residency|residence|freehold|leasehold)\b"
        r"|kupić|kupic|cudzoziem|ภาษี|จำนอง|วีซ่า|ซื้อได้",
        low,
    ))
    who = bool(re.search(
        r"\b(foreigner|foreign|indian|india|european|can i|can an|can a)\b|ชาวต่างชาติ|cudzoziem",
        low,
    ))
    if "can i buy" in low or "can indian buy" in low or "can an indian buy" in low:
        return True
    if "can a foreigner buy" in low or "can foreigners buy" in low:
        return True
    return buy and who


def foreign_ownership_reply(
    message: str,
    *,
    property_name: Optional[str] = None,
    ownership: Optional[str] = None,
    property_type: Optional[str] = None,
) -> str:
    parts = [FOREIGN_OWNERSHIP_NOTE]
    own = (ownership or "").strip()
    ptype = (property_type or "").strip()
    name = (property_name or "").strip()
    low = _low(message)
    if name and (own or ptype):
        parts.append(
            f"In our data {name} is {own or 'listed'} "
            f"{('in a ' + ptype) if ptype else ''}. "
            "Your advisor will confirm the foreign quota and the legal steps."
        )
    if "can i buy" in low or "can indian" in low or "can a foreigner" in low or "can an indian" in low:
        condo = "condo" in ptype.lower() or "apartment" in ptype.lower()
        freehold = "freehold" in own.lower()
        if condo and freehold:
            parts.append(
                "Yes, conditionally: a condominium freehold is the structure foreigners can generally buy, "
                "if the building still has foreign quota and your funds and paperwork check out."
            )
        elif ptype and "land" in ptype.lower() or (own and "land" in own.lower()):
            parts.append(
                "No, not as a land purchase in your own name. A lease or another structure may be possible, "
                "and a lawyer must confirm it for this listing."
            )
        else:
            parts.append(
                "Yes, conditionally, only when the listing is a condominium freehold inside the foreign quota, "
                "or a lease. Your advisor will confirm which one applies here."
            )
    parts.append("I can ask an advisor to confirm this, or send the brochure for this listing.")
    return " ".join(p.strip() for p in parts if p.strip())


def message_starts_new_search(message: str) -> bool:
    """New criteria replace a focused property. Pagination and advisory questions do not."""
    if is_full_reset(message) or is_recall_shown(message) or is_foreign_ownership_question(message):
        return False
    low = _low(message)
    if re.search(r"^(any other|others?|more|show me other|something else)\??$", low):
        return False
    if re.search(r"\b\d+\s*-?\s*bed", low):
        return True
    if re.search(r"\b(something cheaper|cheaper|under|below|up to)\b", low) and re.search(r"\d", low):
        return True
    if re.search(r"\b(phuket|bangkok|pattaya|samui|hua hin|krabi|chiang mai)\b", low) and re.search(
        r"\b(want|show|bed|bedroom|under|budget|villa|condo|in)\b", low
    ):
        return True
    if re.search(r"\b(villa|condo|condominium|apartment)s?\b", low) and re.search(
        r"\b(show|want|looking|in)\b", low
    ):
        return True
    return False


def is_focused_attribute_question(message: str) -> bool:
    if message_starts_new_search(message) or is_foreign_ownership_question(message):
        return False
    low = _low(message)
    pronoun = bool(re.search(
        r"\b(this|it|that one|this one|the first one|the second one|the third one)\b",
        low,
    ))
    attribute = bool(re.search(
        r"\b(near|beach|pool|spa|price|bedroom|location|where|own|freehold|gym|parking)\b",
        low,
    ))
    return pronoun and attribute


def sanitize_extracted_filters(message: str, filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(filters or {})
    status = project_status_from_message(message)
    if status:
        out["status"] = status
    loc = out.get("location")
    if loc and not is_known_location(str(loc)):
        out.pop("location", None)
        out.pop("location_fuzzy", None)
        out["keyword"] = str(loc)
    if out.get("max_price") is not None and price_is_exclusive(message):
        out["max_price_exclusive"] = True
    if is_free_or_cheapest(message):
        out["sort_by_price"] = "asc"
        low = _low(message)
        if re.search(r"\bfree\b", low) and "freehold" not in low:
            out["explain_not_free"] = True
    if is_recall_shown(message):
        out.pop("location", None)
        out["recall_shown"] = True
    return out


def exclude_already_shown(property_ids: Iterable[str], seen_ids: Iterable[str]) -> List[str]:
    seen = {str(s).strip().lower() for s in (seen_ids or []) if s}
    return [str(i) for i in property_ids if str(i).strip().lower() not in seen]


_CLAIM_RE = re.compile(
    r"\b(strategically positioned|asoke corridor|european investors|neighbourhood|neighborhood|investors)\b",
    re.I,
)


def strip_unverified_marketing(text: str, verified_blob: str = "") -> str:
    if not text:
        return text
    blob = (verified_blob or "").lower()
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: List[str] = []
    for sentence in sentences:
        match = _CLAIM_RE.search(sentence)
        if match and match.group(0).lower() not in blob:
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


_TYPE_WORDS = frozenset({
    "villa", "pool villa", "condominium", "condo", "apartment", "penthouse", "townhouse", "residential",
})


def bedrooms_display_value(bedrooms: Any, property_type: Any = None) -> str:
    raw = str(bedrooms or "").strip()
    ptype = str(property_type or "").strip()
    if not raw or raw.lower() in {"n/a", "na", "none", "null"}:
        return "Beds: on request"
    if raw.lower() in _TYPE_WORDS or (ptype and raw.lower() == ptype.lower()):
        return "Beds: on request"
    return raw


def align_cards_to_named_text(text: str, properties: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not text or not properties:
        return properties
    if re.search(r"plus\s+\d+\s+more", text, re.I):
        return properties
    mentioned = []
    for prop in properties:
        name = str(prop.get("name") or prop.get("property_name") or "").strip()
        if name and name.lower() in text.lower():
            mentioned.append(prop)
    if mentioned and len(mentioned) < len(properties):
        return mentioned
    return properties


def normalize_pool_label(prop: Optional[Dict[str, Any]]) -> Optional[str]:
    """One pool wording. Conflicting private/shared tags become a confirmation label."""
    if not isinstance(prop, dict):
        return None
    bits: List[str] = []
    for key in ("pool_type", "description", "highlights", "amenities", "tags", "features"):
        value = prop.get(key)
        if isinstance(value, list):
            bits.extend(str(item) for item in value)
        elif value:
            bits.append(str(value))
    blob = " ".join(bits).lower()
    if "pool" not in blob and not prop.get("has_pool"):
        return None
    private = "private pool" in blob or "pool_private" in blob
    shared = "shared pool" in blob or "communal pool" in blob or "common pool" in blob
    if private and shared:
        pid = prop.get("property_id") or prop.get("id") or ""
        logger.warning("data_quality pool conflict property_id=%s", pid)
        return "Pool (type to be confirmed)"
    return None


def natural_amenity_yes(property_name: str, amenity: str) -> str:
    name = (property_name or "This listing").strip()
    thing = (amenity or "facility").strip()
    return f"Yes, {name} lists a {thing} among its facilities."
