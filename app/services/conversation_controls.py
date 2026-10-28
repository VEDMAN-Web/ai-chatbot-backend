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
    low = _low(normalize_command_text(message))
    if not low:
        return False
    if low in {"reset", "start over", "new search", "never mind", "nevermind"}:
        return True
    if any(p in low for p in _RESET_PHRASES):
        return True
    forgot = bool(re.search(r"\b(forget|forgot|reset|clear)\b", low))
    target = bool(re.search(r"\b(requirements|filters|everything)\b", low))
    return forgot and target


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


def _money_short(amount_thb: float) -> str:
    """'20M' or '4.00M' without a comparison word."""
    millions = float(amount_thb or 0) / 1_000_000
    if millions >= 1:
        text = f"{millions:.2f}".rstrip("0").rstrip(".")
        return f"{text}M"
    return f"{int(amount_thb):,}"


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
    low = _low(normalize_command_text(message))
    if "freehold" in low:
        return bool(re.search(r"\b(cheapest|lowest price|very cheap|cheap)\b", low))
    return bool(re.search(
        r"\b(free properties|free listings|properties for free|very cheap|cheapest|lowest price|lowest-priced|cheap properties)\b"
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
    # Mortgage / loan / financing → advisor (answer_policy), never the ownership canned reply
    if re.search(r"\b(mortgage|loan|financ(?:e|ing)|credit)\b|จำนอง", low):
        return False
    buy = bool(re.search(
        r"\b(buy|own|purchase|tax|taxes|residency|residence|freehold|leasehold)\b"
        r"|kupić|kupic|cudzoziem|ภาษี|วีซ่า|ซื้อได้",
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


# Command words only. Fuzzy matching never runs on free-text property names.
_COMMAND_CANON = {
    "forget": "forget",
    "forgot": "forgot",
    "reset": "reset",
    "clear": "clear",
    "requirements": "requirements",
    "requirement": "requirements",
    "properties": "properties",
    "property": "properties",
    "cheapest": "cheapest",
    "cheap": "cheap",
    "bedroom": "bedroom",
    "bedrooms": "bedroom",
    "filters": "filters",
    "everything": "everything",
    "free": "free",
}
# Listed typos that sit just under the 0.85 cutoff against the longer canon word.
_TYPO_EXACT = {
    "cheep": "cheap",
    "propertys": "properties",
    "properies": "properties",
    "requiredments": "requirements",
    "requirments": "requirements",
    "chepest": "cheapest",
    "bedroon": "bedroom",
}


def fuzzy_command_token(token: str) -> Optional[str]:
    """Map a token to a command word when difflib ratio is at least 0.85."""
    from difflib import SequenceMatcher

    raw = (token or "").lower()
    if raw in _TYPO_EXACT:
        return _TYPO_EXACT[raw]
    if raw in _COMMAND_CANON:
        return _COMMAND_CANON[raw]
    if len(raw) < 4:
        return None
    best_word = None
    best_ratio = 0.0
    for canon in _COMMAND_CANON:
        if abs(len(canon) - len(raw)) > 3:
            continue
        ratio = SequenceMatcher(None, raw, canon).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_word = canon
    if best_word and best_ratio >= 0.85:
        return _COMMAND_CANON[best_word]
    return None


def normalize_command_text(message: str) -> str:
    """Rewrite whitelist typos inside a message. Leaves names and places alone."""
    text = message or ""

    def _swap(match: re.Match) -> str:
        token = match.group(0)
        canon = fuzzy_command_token(token)
        if not canon:
            return token
        if token.lower() in _COMMAND_CANON or token.lower() in _TYPO_EXACT:
            return canon
        return canon

    return re.sub(r"[A-Za-z]+", _swap, text)


def is_weather_question(message: str) -> bool:
    low = _low(normalize_command_text(message))
    return bool(re.search(r"\b(weather|forecast|pogoda|อากาศ)\b", low))


def is_advisor_request(message: str) -> bool:
    low = _low(normalize_command_text(message))
    if not low:
        return False
    patterns = (
        "connect me with an advisor",
        "talk to an advisor",
        "speak to a senior advisor",
        "speak to an advisor",
        "contact an agent",
        "contact an advisor",
        "call me",
        "please call me",
        "i want to talk to someone",
        "talk to someone",
        "request a callback",
        "book a consultation",
        "połącz mnie z doradcą",
        "polacz mnie z doradca",
        "chcę rozmawiać z doradcą",
        "ติดต่อที่ปรึกษา",
        "นัดคุยกับที่ปรึกษา",
        "advisor se baat",
        "salahkar se baat",
        "salahkar sathe",
        "baat karo",
        "call karo",
        "mujhe advisor",
        "mane advisor joiye",
    )
    return any(p in low for p in patterns)


def has_property_search_signal(message: str) -> bool:
    """A foreign place becomes a property search only when the user is asking about property."""
    low = _low(normalize_command_text(message))
    return bool(re.search(
        r"\b(buy|buying|property|properties|villa|villas|condo|condos|condominium|apartment|apartments|"
        r"house|houses|flat|land|invest|investment|listing|listings|price|prices|budget|bedroom|bedrooms|"
        r"rent|rental|real estate|willa|wille|nieruchomo\w*|mieszkan\w*|คอนโด|วิลล่า|อสังหา)\b",
        low,
    ))


def is_cheaper_request(message: str) -> bool:
    low = _low(normalize_command_text(message))
    return bool(re.search(r"\b(cheaper ones|cheaper|something cheaper|tańsze|tansze|ถูกกว่า)\b", low))


def is_sort_only_request(message: str) -> bool:
    low = _low(normalize_command_text(message))
    sort_word = bool(re.search(
        r"\b(cheapest|lowest price|show cheapest|most expensive|highest price|top end|luxury)\b",
        low,
    ))
    if not sort_word:
        return False
    if re.search(
        r"\b(villa|condo|bedroom|bedrooms|phuket|bangkok|pattaya|samui|under|budget|pool|beach|explore|properties)\b",
        low,
    ):
        return False
    return True


def is_search_amenity_refinement(message: str) -> bool:
    """'with private pool' refines the search. 'does it have a pool' asks about one listing."""
    low = _low(normalize_command_text(message))
    if re.search(r"\b(does|do|has|have|is there)\b", low):
        return False
    if re.search(r"\b(it|this one|that one|this property|that property)\b", low):
        return False
    return bool(re.search(
        r"\b(?:with|add)\s+(?:a\s+)?(?:private\s+)?(?:pool|beach(?:\s+access)?|sea\s*view)\b"
        r"|\bpet[- ]friendly\b|\bfurnished\b",
        low,
    ))


def opens_any_budget_search(message: str) -> bool:
    """'any under 1 million' drops place, type, and bedrooms. 'under 5 million' does not."""
    low = _low(normalize_command_text(message))
    return bool(re.search(r"\bany\s+(?:under|below|up to|budget)\b", low))


def is_open_place_search(message: str) -> bool:
    low = _low(message)
    return bool(re.search(r"\b(?:something|anything)\s+in\b", low))


def _stored_filter_snapshot(stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    src = dict(stored or {})
    active = dict(src.get("active_filters") or {})

    def pick(key: str, *tops: str):
        if key in active and active.get(key) is not None:
            return active.get(key)
        for top in tops:
            if src.get(top) is not None:
                return src.get(top)
        return None

    return {
        "location": pick("location", "location"),
        "property_type": pick("property_type", "property_type"),
        "bedrooms": pick("bedrooms", "bedrooms_num"),
        "min_bedrooms": pick("min_bedrooms", "min_bedrooms"),
        "max_bedrooms": pick("max_bedrooms", "max_bedrooms"),
        "max_price": pick("max_price", "budget_max"),
        "min_price": pick("min_price", "min_price"),
        "has_pool": pick("has_pool", "has_pool"),
        "beach_access": pick("beach_access", "beach_access"),
        "required_amenities": list(active.get("required_amenities") or []),
        "status": pick("status", "project_status"),
        "max_price_exclusive": bool(src.get("max_price_exclusive") or active.get("max_price_exclusive")),
        "sort_by_price": active.get("sort_by_price"),
    }


def merge_applied_filters(
    stored: Optional[Dict[str, Any]],
    extracted: Optional[Dict[str, Any]],
    message: str,
) -> Dict[str, Any]:
    """
    One filter object for the search, the 'Searching:' line, and saved preferences.
    A pure refinement changes one slot. A new search drops slots the message does not name.
    Budget from the previous turn stays only for a bedroom+place follow-up that sets no new price.
    """
    base = _stored_filter_snapshot(stored)
    ext = dict(extracted or {})
    low = _low(normalize_command_text(message))
    mentions_location = bool(ext.get("location")) or bool(ext.get("is_broad_thailand"))
    mentions_type = bool(ext.get("property_type")) or bool(ext.get("is_multiple_types"))
    mentions_beds = any(ext.get(k) is not None for k in ("bedrooms", "min_bedrooms", "max_bedrooms"))
    mentions_budget = ext.get("max_price") is not None or ext.get("min_price") is not None
    mentions_amenity = bool(
        ext.get("has_pool") or ext.get("beach_access") or ext.get("sea_view")
        or ext.get("pet_friendly") or ext.get("furnished") or ext.get("required_amenities")
    )
    mentions_status = bool(ext.get("status"))
    sort_value = ext.get("sort_by_price")

    def take_new() -> Dict[str, Any]:
        out = {
            "location": None if ext.get("is_broad_thailand") else ext.get("location"),
            "property_type": None if ext.get("is_multiple_types") else ext.get("property_type"),
            "bedrooms": ext.get("bedrooms"),
            "min_bedrooms": ext.get("min_bedrooms"),
            "max_bedrooms": ext.get("max_bedrooms"),
            "max_price": ext.get("max_price"),
            "min_price": ext.get("min_price"),
            "has_pool": True if ext.get("has_pool") else None,
            "beach_access": True if ext.get("beach_access") else None,
            "required_amenities": list(ext.get("required_amenities") or []),
            "status": ext.get("status"),
            "max_price_exclusive": bool(ext.get("max_price_exclusive")),
            "sort_by_price": sort_value,
        }
        if mentions_beds and ext.get("bedrooms") is not None and "min_bedrooms" not in ext and "max_bedrooms" not in ext:
            out["min_bedrooms"] = None
            out["max_bedrooms"] = None
        return out

    if opens_any_budget_search(message):
        # Drop filters carried over from earlier turns, then keep every filter this message states.
        out = {k: None for k in base}
        out["required_amenities"] = list(ext.get("required_amenities") or [])
        for key in (
            "location", "property_type", "bedrooms", "min_bedrooms", "max_bedrooms",
            "max_price", "min_price", "has_pool", "beach_access", "sea_view",
            "pet_friendly", "furnished", "status", "sort_by_price",
        ):
            if ext.get(key) is not None:
                out[key] = ext.get(key)
        if ext.get("is_broad_thailand"):
            out["location"] = None
            out["broad_thailand"] = True
        out["max_price_exclusive"] = bool(ext.get("max_price_exclusive"))
        if sort_value and not out.get("sort_by_price"):
            out["sort_by_price"] = sort_value
        return out

    if is_sort_only_request(message) or (
        sort_value and not any((mentions_location, mentions_type, mentions_beds, mentions_budget, mentions_amenity, mentions_status))
    ):
        out = dict(base)
        if re.search(r"\b(most expensive|highest price|top end|luxury)\b", low) or "expensive" in low:
            out["sort_by_price"] = "desc"
        elif re.search(r"\b(cheapest|lowest price|show cheapest)\b", low):
            out["sort_by_price"] = "asc"
        else:
            out["sort_by_price"] = sort_value
        if not any(out.get(k) for k in ("location", "property_type", "bedrooms", "min_bedrooms", "max_bedrooms", "max_price")):
            out["broad_thailand"] = True
        return out

    if is_cheaper_request(message) and not mentions_budget and not mentions_location and not mentions_type and not mentions_beds:
        out = dict(base)
        out["cheaper"] = True
        return out

    single_budget = mentions_budget and not any((mentions_location, mentions_type, mentions_beds, mentions_amenity, mentions_status))
    single_location = mentions_location and not any((mentions_budget, mentions_type, mentions_beds, mentions_amenity, mentions_status))
    single_amenity = mentions_amenity and not any((mentions_budget, mentions_location, mentions_type, mentions_beds, mentions_status))

    if single_budget:
        out = dict(base)
        out["max_price"] = ext.get("max_price", out.get("max_price"))
        out["min_price"] = ext.get("min_price", out.get("min_price"))
        out["max_price_exclusive"] = bool(ext.get("max_price_exclusive"))
        return out

    if single_location and is_open_place_search(message):
        return {
            "location": None if ext.get("is_broad_thailand") else ext.get("location"),
            "property_type": None,
            "bedrooms": None,
            "min_bedrooms": None,
            "max_bedrooms": None,
            "max_price": None,
            "min_price": None,
            "has_pool": None,
            "beach_access": None,
            "required_amenities": [],
            "status": None,
            "max_price_exclusive": False,
            "sort_by_price": sort_value,
            "broad_thailand": bool(ext.get("is_broad_thailand")),
        }

    if single_location:
        out = dict(base)
        if ext.get("is_broad_thailand"):
            out["location"] = None
            out["broad_thailand"] = True
        else:
            out["location"] = ext.get("location")
        if sort_value:
            out["sort_by_price"] = sort_value
        return out

    if single_amenity:
        out = dict(base)
        if ext.get("has_pool"):
            out["has_pool"] = True
        if ext.get("beach_access"):
            out["beach_access"] = True
        if ext.get("required_amenities"):
            out["required_amenities"] = list(ext.get("required_amenities") or [])
        for key in ("sea_view", "pet_friendly", "furnished"):
            if ext.get(key):
                out[key] = True
        return out

    # New combination. Drop slots the message does not name.
    out = take_new()
    keep_budget = (
        not mentions_budget
        and mentions_beds
        and mentions_location
        and not mentions_type
        and not mentions_status
        and not mentions_amenity
    )
    if keep_budget:
        out["max_price"] = base.get("max_price")
        out["min_price"] = base.get("min_price")
        out["max_price_exclusive"] = bool(base.get("max_price_exclusive"))
    if not mentions_location and not ext.get("is_broad_thailand"):
        out["location"] = None
    if not mentions_type:
        out["property_type"] = None
    if not mentions_beds:
        out["bedrooms"] = None
        out["min_bedrooms"] = None
        out["max_bedrooms"] = None
    if not mentions_amenity:
        out["has_pool"] = None
        out["beach_access"] = None
        out["required_amenities"] = []
    if not mentions_status:
        out["status"] = None
    return out


def active_filter_count(filters: Optional[Dict[str, Any]]) -> int:
    src = filters or {}
    count = 0
    if src.get("location"):
        count += 1
    if src.get("property_type"):
        count += 1
    if any(src.get(k) is not None for k in ("bedrooms", "min_bedrooms", "max_bedrooms")):
        count += 1
    if src.get("max_price") is not None or src.get("min_price") is not None:
        count += 1
    if src.get("has_pool") or src.get("beach_access") or src.get("required_amenities"):
        count += 1
    if src.get("status"):
        count += 1
    return count


def describe_applied_filters(filters: Optional[Dict[str, Any]]) -> str:
    """Plain-language filter line. No raw floats or comparison operators."""
    src = filters or {}
    bits: List[str] = []
    if src.get("broad_thailand") and not src.get("location"):
        bits.append("all Thailand")
    elif src.get("location"):
        bits.append(str(src["location"]))
    if src.get("property_type"):
        bits.append(str(src["property_type"]))
    if src.get("min_bedrooms") is not None and src.get("max_bedrooms") is not None:
        bits.append(f"{src['min_bedrooms']}-{src['max_bedrooms']} bedrooms")
    elif src.get("min_bedrooms") is not None:
        bits.append(f"{src['min_bedrooms']}+ bedrooms")
    elif src.get("max_bedrooms") is not None:
        bits.append(f"up to {src['max_bedrooms']} bedrooms")
    elif src.get("bedrooms") is not None:
        bits.append(f"{src['bedrooms']} bedrooms")
    if src.get("min_price") is not None and src.get("max_price") is not None:
        bits.append(f"{_money_short(src['min_price'])}-{_money_short(src['max_price'])} THB")
    elif src.get("max_price") is not None:
        bits.append(format_budget_phrase(
            float(src["max_price"]),
            exclusive=bool(src.get("max_price_exclusive")),
            with_usd=False,
        ))
    if src.get("beach_access"):
        bits.append("beach access")
    elif src.get("has_pool"):
        bits.append("pool")
    if src.get("status"):
        bits.append(str(src["status"]))
    return ", ".join(bits)
