"""
Understanding admin chat commands that change data: which number is the price, which word is the status, what a new
listing is made of, and whether the message asks to delete or undo.

Pure functions (no database, no model). The chat uses them so a number inside a listing name ("3 Villas - Lamai") is never read
as the price, a negative price or an unknown status is refused instead of being guessed, and "add a listing", "delete" and
"undo" are not mistaken for searches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.services.action_service import ALLOWED_PROPERTY_TYPES, ALLOWED_STATUSES, name_slug

# ---------------------------------------------------------------------------------------------------------------------
# price
# ---------------------------------------------------------------------------------------------------------------------

_AMOUNT = re.compile(
    r"(?P<neg>[-−]\s*|minus\s+|negative\s+)?"
    r"(?P<cur1>฿\s*|thb\s*|baht\s*|\$\s*|€\s*|usd\s*|eur\s*|sgd\s*|pln\s*|zł\s*)?"
    r"(?P<num>\d{1,3}(?:\.\d{3})+|\d[\d,]*(?:\.\d+)?)"
    r"(?P<unit>\s*(?:million|mln|mil|m|k|thousand)\b)?"
    r"(?P<cur2>\s*(?:฿|thb\b|baht\b|usd\b|eur\b|sgd\b|pln\b|zł\b|\$|€))?",
    re.I,
)
_UNIT_FACTOR = {"million": 1_000_000, "mln": 1_000_000, "mil": 1_000_000, "m": 1_000_000, "k": 1_000, "thousand": 1_000}
# Words that, right before a number, say "this number is the new value" (not "of", "for": "price of 3 Villas").
_VALUE_ANCHORS = {"to", "at", "na", "as", "is", "=", ":", "price", "cena", "for"}


def _detect_currency_code(cur1: str, cur2: str) -> str:
    blob = f"{cur1 or ''} {cur2 or ''}".lower()
    if "$" in blob or "usd" in blob:
        return "USD"
    if "€" in blob or "eur" in blob:
        return "EUR"
    if "sgd" in blob:
        return "SGD"
    if "pln" in blob or "zł" in blob:
        return "PLN"
    return "THB"


def _parse_num_token(token: str) -> Optional[float]:
    text = (token or "").strip().replace("\u00a0", "").replace("_", "").replace("'", "")
    text = re.sub(r"\s+", "", text)
    if not text:
        return None
    if re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        text = text.replace(".", "")
    elif "," in text:
        text = text.replace(",", "")
    if text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class PriceReading:
    value: Optional[float] = None      # THB or native currency amount
    currency: str = "THB"              # Currency code ("THB", "USD", "EUR", etc.)
    error: Optional[str] = None        # why a price that was given cannot be used
    present: bool = False              # the message gave a price (usable or not)


def extract_price(message: str) -> PriceReading:
    """The price a command asks for. Only amounts that look like prices count: a number with a unit or currency
    ("5M", "5,000,000 THB", "450,000 EUR", "$250,000"), a number right after "to"/"at"/"price", or an amount of 1,000 or more.
    A number that is part of a listing name ("3 Villas - Lamai", "Villa 12") is ignored."""
    text = message or ""
    chosen = None
    for m in _AMOUNT.finditer(text):
        after = text[m.end():m.end() + 1]
        if after == "%":
            continue
        unit = (m.group("unit") or "").strip().lower()
        explicit = bool(unit or m.group("cur1") or m.group("cur2"))
        num_str = m.group("num")
        number = _parse_num_token(num_str)
        if number is None:
            continue
        value = number * _UNIT_FACTOR.get(unit, 1)
        before = text[:m.start()].rstrip()
        last_word = re.split(r"\s+", before)[-1].lower() if before else ""
        anchored = last_word in _VALUE_ANCHORS or before.endswith(("=", ":"))
        following = re.match(r"\s*([A-Za-z]+)", text[m.end():])
        names_something = bool(following) and not explicit and following.group(1).lower() not in {"thb", "baht", "usd", "eur", "sgd", "pln"}
        if names_something and not anchored:
            continue                      # "3 Villas", "2 bedrooms", "12 units": a count or a name, not a price
        if not (explicit or anchored or value >= 1000):
            continue
        negative = bool(m.group("neg"))
        curr = _detect_currency_code(m.group("cur1"), m.group("cur2"))
        chosen = (value, negative, curr)  # the last candidate wins: the price usually closes the sentence
    if chosen is None:
        return PriceReading()
    value, negative, curr = chosen
    if negative or value <= 0:
        return PriceReading(error="The price must be greater than 0 THB, so I did not stage any change.", present=True)
    return PriceReading(value=float(value), currency=curr, present=True)


# ---------------------------------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------------------------------

STATUS_WORDS: Dict[str, str] = dict(ALLOWED_STATUSES)
STATUS_WORDS.update({
    "ready": "Ready to move in", "ready to move": "Ready to move in", "move-in ready": "Ready to move in",
    "move in ready": "Ready to move in", "under-construction": "Under construction", "in construction": "Under construction",
    "sold": "Sold Out", "soldout": "Sold Out", "sold-out": "Sold Out",
    "reserve": "Reserved", "on sale": "For sale", "forsale": "For sale", "for-sale": "For sale",
    "offplan": "Off-plan", "pre sale": "Pre-sale", "pre-sale": "Pre-sale",
})
VALID_STATUS_LIST = ", ".join(sorted(set(ALLOWED_STATUSES.values())))

_STATUS_PHRASE = re.compile(
    r"\bstatus\b(?:\s+(?:of|for)\s+.+?)?\s*(?:to|as|=|:|is|na)\s+(?P<val>[A-Za-z][A-Za-z \-]*?)\s*(?=$|[,.;]|\band\b|\bwith\b|\bprice\b|\bat\b|\bfor\b)",
    re.I,
)
_MARK_PHRASE = re.compile(r"\b(?:mark|set|make|flag)\s+(?:it\s+|this\s+|that\s+)?(?:as\s+)?(?P<val>[A-Za-z][A-Za-z \-]*?)\s*$", re.I)


_AS_PHRASE = re.compile(r"\b(?:mark|set|flag|make)\b.*?\bas\s+(?P<val>[A-Za-z][A-Za-z \-]*?)\s*$", re.I)


@dataclass
class StatusReading:
    value: Optional[str] = None
    error: Optional[str] = None
    present: bool = False


def _map_status(raw: str) -> Optional[str]:
    cleaned = re.sub(r"\s+", " ", (raw or "").strip().lower().strip(" .,;:"))
    cleaned = re.sub(r"^(?:status\s+(?:is|to|as|=|:)\s+|status\s*[:=]\s*|status\s+)", "", cleaned)
    cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned)
    if cleaned in STATUS_WORDS:
        return STATUS_WORDS[cleaned]
    return None
def extract_status(message: str) -> StatusReading:
    """The listing status a command asks for, mapped to the allowed values. An unknown value is an error, never a silent default."""
    text = message or ""
    m = _STATUS_PHRASE.search(text)
    if m:
        raw = m.group("val")
        mapped = _map_status(raw)
        if mapped is None:
            # "status to ready to move in" ends at "in"; try progressively shorter phrases
            words = raw.split()
            for cut in range(len(words), 0, -1):
                mapped = _map_status(" ".join(words[:cut]))
                if mapped:
                    break
        if mapped:
            return StatusReading(value=mapped, present=True)
        return StatusReading(error=f"'{raw.strip()}' is not a valid status. Valid statuses: {VALID_STATUS_LIST}.", present=True)
    for pattern in (_MARK_PHRASE, _AS_PHRASE):
        m = pattern.search(text)
        mapped = _map_status(m.group("val")) if m else None
        if mapped:
            return StatusReading(value=mapped, present=True)
    low = text.lower()
    for phrase in ("ready to move in", "under construction", "sold out", "off-plan", "pre-sale"):
        if phrase in low and re.search(r"\b(mark|set|change|update|make)\b", low):
            return StatusReading(value=STATUS_WORDS[phrase], present=True)
    return StatusReading()


# ---------------------------------------------------------------------------------------------------------------------
# what the message asks for
# ---------------------------------------------------------------------------------------------------------------------

_TYPE_WORDS = r"(?:property|properties|listing|listings|villa|villas|condo|condos|condominium|apartment|house|townhouse|penthouse|land|project)"
_CREATE_LISTING = re.compile(
    rf"^\s*(?:please\s+|pls\s+|can you\s+|could you\s+)?(?:add|create|register|insert|enter|make)\s+(?:a\s+|an\s+|the\s+)?(?:brand[- ]new\s+|new\s+)?{_TYPE_WORDS}\b",
    re.I,
)
_CREATE_LEAD = re.compile(
    r"^\s*(?:please\s+|pls\s+)?(?:add|create|register|insert|enter|save|new)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?(?:buyer\s+|client\s+|customer\s+)?(?:lead|contact|inquiry|enquiry)\b",
    re.I,
)
_DELETE = re.compile(
    r"^\s*(?:please\s+|pls\s+|can you\s+|could you\s+)?(?:permanently\s+|completely\s+|hard[- ]?)?(?:delete|remove|archive|usun|usuń|erase|drop)\b",
    re.I,
)
_PERMANENT = re.compile(r"\b(permanent(?:ly)?|forever|hard[- ]?delete|irreversibl\w+)\b", re.I)
_UNDO = re.compile(
    r"^\s*(?:please\s+|pls\s+)?(?:undo|revert|roll\s*back|rollback|cofnij)\b",
    re.I,
)
_OP_ID = re.compile(r"\b((?:batch|crud)_[0-9a-f]{12})\b", re.I)


def detect_crud_intent(message: str) -> Optional[str]:
    """'create_lead', 'create_listing', 'delete', 'undo', or None."""
    text = (message or "").strip()
    if not text:
        return None
    if _UNDO.search(text):
        return "undo"
    if _CREATE_LEAD.search(text):
        return "create_lead"
    if _CREATE_LISTING.search(text):
        return "create_listing"
    if _DELETE.search(text):
        return "delete"
    return None


def wants_permanent_delete(message: str) -> bool:
    return bool(_PERMANENT.search(message or ""))


def operation_id_in(message: str) -> Optional[str]:
    m = _OP_ID.search(message or "")
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------------------------------------------------
# a new listing
# ---------------------------------------------------------------------------------------------------------------------

REQUIRED_NEW_LISTING = (
    ("property_name", "the listing name"),
    ("property_type", "the type (Villa, Condominium, Townhouse, Penthouse, Apartment, House, Land, Commercial)"),
    ("province", "the location (province or area)"),
    ("price_numeric_min", "the price in THB"),
    ("status", f"the status ({VALID_STATUS_LIST})"),
)
_KEYED = re.compile(
    r"(?P<key>name|title|id|type|location|province|area|district|status|price|bedrooms?|beds)\s*[:=]\s*(?P<val>(?:[^,;\n]|,(?=\d{3}(?!\d)))+)", re.I
)


@dataclass
class NewListing:
    fields: Dict[str, object] = field(default_factory=dict)
    property_id: Optional[str] = None
    missing: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _body_of(message: str) -> str:
    text = message.strip()
    if ":" in text:
        return text.split(":", 1)[1]
    m = re.search(rf"{_TYPE_WORDS}\s+(?:called|named)?\s*(.+)$", text, re.I)
    return m.group(1) if m else text


def parse_new_listing(message: str) -> NewListing:
    """The fields of 'Add a new listing: Sunset Pearl, villa, Phuket, 12,000,000 THB, available' (or name: ..., type: ... pairs)."""
    result = NewListing()
    text = (message or "").strip()
    fields: Dict[str, object] = {}

    # Check prefix for property type (e.g. "Add a new condo: Riverside Loft, Bangkok, 5 mln THB, under construction")
    prefix_type_m = re.search(
        rf"^\s*(?:please\s+|pls\s+|can you\s+|could you\s+)?(?:add|create|register|insert|enter|make)\s+(?:a\s+|an\s+|the\s+)?(?:brand[- ]new\s+|new\s+)?(?P<type>{_TYPE_WORDS})\b",
        text,
        re.I
    )
    if prefix_type_m:
        ptype = prefix_type_m.group("type").lower()
        if ptype not in ("property", "properties", "listing", "listings", "project"):
            fields["property_type"] = ALLOWED_PROPERTY_TYPES.get(ptype, ALLOWED_PROPERTY_TYPES.get(ptype.rstrip("s"), ptype.title()))

    body = _body_of(text)

    # Check for sentence pattern: "[called/named] <name> in <loc> for/at/price <price>"
    m_sentence = re.search(
        r"^(?:called\s+|named\s+)?(?P<name>.+?)\s+in\s+(?P<loc>[A-Za-z0-9 -]+?)\s+(?:for|at|price|with\s+price)\s+(?P<price>[0-9][0-9,.\s]*(?:million|mln|mil|m|k|thousand)?\s*(?:thb|baht|usd|eur|sgd|pln|\$|€)?)(?:\s*,\s*(?P<rest>.+))?$",
        body,
        re.I
    )
    if m_sentence:
        fields["property_name"] = m_sentence.group("name").strip()
        fields["province"] = m_sentence.group("loc").strip()
        reading = extract_price(m_sentence.group("price").strip())
        if reading.error:
            result.errors.append(reading.error)
        elif reading.value:
            fields["price_numeric_min"] = reading.value
            if reading.currency != "THB":
                fields["currency"] = reading.currency
        rest = (m_sentence.group("rest") or "").strip()
        if rest:
            st = _map_status(rest)
            if st:
                fields["status"] = st
            else:
                body = rest
        else:
            body = ""

    # Parse keyed patterns with : or =
    for m in _KEYED.finditer(body):
        key, val = m.group("key").lower(), m.group("val").strip()
        if key in ("name", "title"):
            fields["property_name"] = val
        elif key == "id":
            result.property_id = val.lower()
        elif key == "type":
            fields["property_type"] = ALLOWED_PROPERTY_TYPES.get(val.lower(), val)
        elif key in ("location", "province", "area", "district"):
            fields["province"] = val
        elif key == "status":
            mapped = _map_status(val)
            if mapped:
                fields["status"] = mapped
            else:
                result.errors.append(f"'{val}' is not a valid status. Valid statuses: {VALID_STATUS_LIST}.")
        elif key == "price":
            reading = extract_price(f"price to {val}")
            if reading.error:
                result.errors.append(reading.error)
            elif reading.value:
                fields["price_numeric_min"] = reading.value
                if reading.currency != "THB":
                    fields["currency"] = reading.currency
        elif key in ("bedrooms", "bedroom", "beds"):
            n = re.search(r"\d+", val)
            if n:
                fields["bedrooms_min"] = int(n.group(0))
    if fields or result.errors:
        body = _KEYED.sub("", body)

    leftovers: List[str] = []
    tokens = [t.strip(" .") for t in re.split(r",(?!\d{3}(?!\d))|[;\n]", body) if t.strip(" .")]
    from app.services.listing_shape import CITY_ALIASES

    for token in tokens:
        low = token.lower()

        # Keyed without colon: status Available
        m_st = re.match(r"^\s*status\s*[:=]?\s*(.+)$", token, re.I)
        if m_st:
            st_val = m_st.group(1).strip()
            mapped = _map_status(st_val)
            if mapped:
                fields["status"] = mapped
            else:
                result.errors.append(f"'{st_val}' is not a valid status. Valid statuses: {VALID_STATUS_LIST}.")
            continue

        # Keyed without colon: price 9,500,000 THB
        m_pr = re.match(r"^\s*price\s*[:=]?\s*(.+)$", token, re.I)
        if m_pr:
            reading = extract_price(m_pr.group(1))
            if reading.error:
                result.errors.append(reading.error)
            elif reading.value:
                fields["price_numeric_min"] = reading.value
                if reading.currency != "THB":
                    fields["currency"] = reading.currency
            continue

        # Keyed without colon: type Villa
        m_tp = re.match(r"^\s*type\s*[:=]?\s*(.+)$", token, re.I)
        if m_tp:
            tp_val = m_tp.group(1).strip().lower()
            if tp_val in ALLOWED_PROPERTY_TYPES:
                fields["property_type"] = ALLOWED_PROPERTY_TYPES[tp_val]
            else:
                fields["property_type"] = tp_val.title()
            continue

        # Keyed without colon: location Lamai
        m_lc = re.match(r"^\s*(?:location|province|city|area|district)\s*[:=]?\s*(.+)$", token, re.I)
        if m_lc:
            fields["province"] = m_lc.group(1).strip()
            continue

        # Type in location: "a villa in Lamai"
        m_type_in = re.match(r"^\s*(?:a\s+|an\s+)?(?P<type>villa|condominium|condo|apartment|house|townhouse|penthouse|land|hotel unit|commercial)\s+in\s+(?P<loc>.+)$", token, re.I)
        if m_type_in:
            t_val = m_type_in.group("type").lower()
            fields["property_type"] = ALLOWED_PROPERTY_TYPES.get(t_val, t_val.title())
            loc = m_type_in.group("loc").strip()
            if "province" in fields:
                fields["province"] = f"{fields['province']}, {loc}"
            else:
                fields["province"] = loc
            continue

        # Standalone type
        if low in ALLOWED_PROPERTY_TYPES and "property_type" not in fields:
            fields["property_type"] = ALLOWED_PROPERTY_TYPES[low]
            continue

        # Standalone status
        mapped = _map_status(token)
        if mapped and "status" not in fields and low not in ALLOWED_PROPERTY_TYPES:
            fields["status"] = mapped
            continue

        # Beds
        beds = re.fullmatch(r"(\d+)\s*(?:bed|beds|bedroom|bedrooms|br)", low)
        if beds:
            fields["bedrooms_min"] = int(beds.group(1))
            continue

        # Standalone price
        reading = extract_price(token if re.search(r"\d", token) else "")
        if reading.present and re.fullmatch(r"[\d\s,.฿$€a-zA-Z\-]*", token) and re.search(r"(thb|baht|฿|usd|\$|eur|€|sgd|pln|zł|\d\s*(?:m|k|million)\b|\d[\d,]{3,})", low):
            if reading.error:
                result.errors.append(reading.error)
            elif "price_numeric_min" not in fields:
                fields["price_numeric_min"] = reading.value
                if reading.currency != "THB":
                    fields["currency"] = reading.currency
            continue

        # Place word: if province already set, combine it (e.g. "Lamai", "Koh Samui")
        tok_slug = token.lower().replace(" ", "-")
        if tok_slug in CITY_ALIASES or token.lower() in ("koh samui", "samui", "phuket", "bangkok", "pattaya", "hua hin", "lamai"):
            if "province" in fields:
                fields["province"] = f"{fields['province']}, {token}"
            else:
                fields["province"] = token
            continue

        leftovers.append(token)

    if "property_name" not in fields and leftovers:
        fields["property_name"] = leftovers.pop(0)
    if "province" not in fields and leftovers:
        fields["province"] = leftovers.pop(0)

    for token in leftovers:
        tok_slug = token.lower().replace(" ", "-")
        if tok_slug in CITY_ALIASES or token.lower() in ("koh samui", "samui", "phuket", "bangkok", "pattaya", "hua hin"):
            fields["province"] = f"{fields.get('province', '')}, {token}".strip(", ")
            continue
        if token.lower() in ("sea view", "private pool", "pool", "garden", "gym", "balcony"):
            continue
        result.errors.append(f"I could not place '{token}'.")

    if fields.get("price_numeric_min"):
        price = float(fields["price_numeric_min"])
        fields["price_numeric_min"] = int(price) if price == int(price) else price
        curr = str(fields.get("currency") or "THB").upper()
        if curr in ("THB", ""):
            fields["price_display"] = f"{int(price):,} THB"
        else:
            fields["currency"] = curr
            fields["price_display"] = f"{int(price):,} {curr}"

    result.fields = fields
    if "property_name" in fields:
        result.property_id = result.property_id or name_slug(str(fields["property_name"]))
    result.missing = [label for key, label in REQUIRED_NEW_LISTING if key not in fields]
    return result


# ---------------------------------------------------------------------------------------------------------------------
# staging an archive (soft delete) and an undo; both wait for the staff member's yes
# ---------------------------------------------------------------------------------------------------------------------

def _awaiting(pending_id: str) -> str:
    return (
        "\u26a0\ufe0f **Awaiting confirmation** \u2014 nothing has been written yet.\n"
        f"Pending action ID: `{pending_id}`. Reply **Confirm** / **Yes** to apply, or **Cancel** to discard."
    )


def propose_archive(db, staff, staff_email: str, conversation_id: str, property_id: str, selected_websites: List[str]) -> Dict[str, object]:
    """Stage the archive of one listing. Returns {"ok", "text", "pending_action_id", "status"}; ok is False when nothing was staged."""
    from app.services.action_service import create_pending_action
    from app.services.admin_property_fields import resolve_existing_property
    from app.services.property_search import property_belongs_to_websites

    source, record = resolve_existing_property(db, property_id)
    if not source:
        return {"ok": False, "status": "not_found", "text": f"I could not find a listing with id `{property_id}`. Nothing was staged."}
    if selected_websites and not property_belongs_to_websites(record, selected_websites):
        return {"ok": False, "status": "forbidden",
                "text": f"Access denied: `{property_id}` does not belong to your currently selected website(s): {selected_websites}."}
    name = record.get("property_name") or record.get("name") if isinstance(record, dict) else getattr(record, "property_name", property_id)
    status = record.get("status") if isinstance(record, dict) else getattr(record, "status", None)
    from app.services.firestore_service import is_soft_deleted_record
    if str(status or "").lower() in ("archived", "deleted") or is_soft_deleted_record(record):
        return {"ok": False, "status": "already_archived", "text": f"**{name}** (`{property_id}`) is already archived. Nothing to do."}
    table = (
        "| Field | Current | New |\n|---|---|---|\n"
        f"| status | {status or '-'} | Archived |\n"
        "| visibility | Public | Hidden from the public website |"
    )
    action = create_pending_action(
        db=db, staff_user_id=staff_email, tool_name="archive_listing", conversation_id=conversation_id,
        action_payload={"property_id": property_id, "property_name": name, "current_status": status,
                        "site_scope": list(selected_websites or []), "diff_markdown": table},
    )
    text = (
        f"**Archive listing** {name} (`{property_id}`)\n\n{table}\n\n"
        "This is a soft delete: nothing is erased, and you can undo it for 6 days.\n\n" + _awaiting(str(action.id))
    )
    return {"ok": True, "status": "proposed", "text": text, "pending_action_id": str(action.id)}


def propose_undo(db, staff, staff_email: str, conversation_id: str, operation_id: Optional[str] = None) -> Dict[str, object]:
    """Stage the undo of one operation: the one named, or the staff member's latest that can still be undone."""
    from app.services.action_service import create_pending_action
    from app.services.admin_batch_service import get_batch_operations_history

    history = get_batch_operations_history(limit=100, staff_user=staff)
    mine = [
        op for op in history
        if op.get("can_undo") and (op.get("staff_user_id") == getattr(staff, "user_id", None) or op.get("staff_email") == staff_email)
        or (operation_id and op.get("operation_id") == operation_id and op.get("can_undo"))
    ]
    if operation_id:
        chosen = next((op for op in history if op.get("operation_id") == operation_id), None)
        if chosen is None:
            return {"ok": False, "status": "not_found", "text": f"I could not find operation `{operation_id}` (it may be expired or not yours to see)."}
        if not chosen.get("can_undo"):
            return {"ok": False, "status": "not_undoable", "text": f"Operation `{operation_id}` can no longer be undone ({chosen.get('status')}, {chosen.get('remaining_undo_time')})."}
    else:
        chosen = mine[0] if mine else None
        if chosen is None:
            return {"ok": False, "status": "nothing_to_undo", "text": "There is nothing to undo: none of your changes from the last 6 days can still be undone."}
    ids = list(chosen.get("affected_ids") or [])
    shown = ", ".join(f"`{i}`" for i in ids[:10]) + (f" and {len(ids) - 10} more" if len(ids) > 10 else "")
    table = (
        "| Operation | Type | Listings | Done | Time left |\n|---|---|---|---|---|\n"
        f"| `{chosen['operation_id']}` | {chosen.get('action_type')} | {len(ids)} | {chosen.get('created_at')} | {chosen.get('remaining_undo_time')} |"
    )
    action = create_pending_action(
        db=db, staff_user_id=staff_email, tool_name="undo_operation", conversation_id=conversation_id,
        action_payload={"operation_id": chosen["operation_id"], "action_type": chosen.get("action_type"),
                        "affected_ids": ids, "diff_markdown": table},
    )
    text = (
        f"**Undo** operation `{chosen['operation_id']}` ({chosen.get('action_type')}): restores {shown or 'the previous state'} "
        f"to how it was before.\n\n{table}\n\n" + _awaiting(str(action.id))
    )
    return {"ok": True, "status": "proposed", "text": text, "pending_action_id": str(action.id), "operation_id": chosen["operation_id"]}


# ---------------------------------------------------------------------------------------------------------------------
# reading one listing by name ("Show 3 Villas - Lamai", "Specs for Pirak Villa")
# ---------------------------------------------------------------------------------------------------------------------

_READ_PATTERNS = [
    re.compile(
        r"^\s*(?:please\s+)?(?:specs?|specifications?|details?|info(?:rmation)?|dossier|data|price|status)\s+(?:for|of|on|about)\s+(?P<name>.+?)\s*[?.!]*$",
        re.I,
    ),
    re.compile(
        r"^\s*(?:please\s+|pls\s+)?(?:show|display|open|get|pull\s+up|give\s+me|tell\s+me\s+about|find|look\s+up|lookup)\s+(?:me\s+)?"
        r"(?:the\s+)?(?:(?:listing|property)\s+)?(?:(?:specs?|specifications?|details?|info(?:rmation)?)\s+(?:for|of|on|about)\s+)?(?P<name>.+?)\s*[?.!]*$",
        re.I,
    ),
    re.compile(
        r"^\s*what(?:'s|\s+is)\s+(?:the\s+)?(?:price|status|location)\s+(?:of|for)\s+(?P<name>.+?)\s*[?.!]*$",
        re.I,
    ),
]
_SEARCH_WORDS = re.compile(r"\b(in|near|under|below|above|over|between|with|without|than|around|within)\b", re.I)


def extract_listing_name_for_read(message: str) -> Optional[str]:
    """The listing a read command names, or None when the message is a search (villas in Phuket, under 5M, with a pool)."""
    for pattern in _READ_PATTERNS:
        m = pattern.search(message or "")
        if not m:
            continue
        name = m.group("name").strip(" .,;:'\"")
        if not name or _SEARCH_WORDS.search(name):
            return None
        if re.fullmatch(r"(?:all\s+)?(?:the\s+)?(?:my\s+)?(?:villas?|condos?|apartments?|properties|listings|leads|everything|more|it|that|this)", name, re.I):
            return None
        return name
    return None


def resolve_listing_for_read(db, message: str, selected_websites: Optional[List[str]] = None) -> Optional[str]:
    """The id of the one listing the message names (and the caller may see), or None."""
    name = extract_listing_name_for_read(message)
    if not name:
        return None
    from app.services.admin_property_fields import resolve_existing_property
    from app.services.admin_property_resolver import resolve_admin_property_name
    from app.services.property_search import property_belongs_to_websites

    resolved = resolve_admin_property_name(name, already_extracted=True, db=db)
    if resolved.kind != "resolved" or not resolved.property_id:
        return None
    source, record = resolve_existing_property(db, resolved.property_id)
    from app.services.firestore_service import is_soft_deleted_record
    if is_soft_deleted_record(record):
        return None
    if not source:
        return None
    if selected_websites and not property_belongs_to_websites(record, selected_websites):
        return None
    return resolved.property_id
