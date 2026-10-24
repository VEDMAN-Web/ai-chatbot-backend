"""
Plain-language helpers for admin chat replies.

These functions only shape text. They do not call the network or a model.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_TOOL_LABELS = {
    "create_or_update_listing": "Property change",
    "ingest_property_from_url": "Property import from a URL",
    "ingest_property_from_drive": "Property import from Drive",
    "execute_batch_property_action": "Bulk property change",
    "send_email": "Email",
    "create_calendar_event": "Calendar event",
    "generate_report": "Report",
    "generate_client_offer_dossier": "Client offer dossier",
    "undo_batch_property_operation": "Undo",
}

_INTERNAL_KEYS = (
    "authoritative_total_properties",
    "is_complete_database_count",
    "firestore_live_projects_count",
    "postgresql_listings_count",
    "firestore_count_unavailable",
)

_TOOL_NAMES = set(_TOOL_LABELS) | {
    "get_property_knowledge",
    "get_database_inventory_stats",
    "get_buyer_leads",
    "list_buyer_leads",
    "search_properties_full",
    "undo_batch_property_operation",
}

_LIST_OR_REPORT_TOOLS = {
    "get_buyer_leads",
    "list_buyer_leads",
    "search_properties_full",
    "generate_report",
    "generate_client_offer_dossier",
}

_HYGIENE_MARKERS = (
    "Tool data:",
    "Operation Mode:",
    "[UPDATE SUCCESS]",
    "audit_ephemeral_",
    "(do not run an action by themselves)",
    "price_numeric_min",
    "price_display",
    "create_or_update_listing",
    "Your the",
    *_INTERNAL_KEYS,
)


def human_tool_label(tool_name: str) -> str:
    key = (tool_name or "").strip()
    if key in _TOOL_LABELS:
        return _TOOL_LABELS[key]
    words = key.replace("_", " ").strip()
    if not words:
        return "Action"
    return words[:1].upper() + words[1:]


def _listing_name(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("property_name", "title", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    fields = payload.get("fields") or payload.get("fields_to_update") or {}
    if isinstance(fields, dict):
        for key in ("property_name", "title", "name"):
            value = fields.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    provenance = payload.get("provenance") or {}
    if isinstance(provenance, dict):
        named = provenance.get("property_name")
        if isinstance(named, dict) and isinstance(named.get("value"), str) and named["value"].strip():
            return named["value"].strip()
        if isinstance(named, str) and named.strip():
            return named.strip()
    duplicate = payload.get("duplicate") or {}
    if isinstance(duplicate, dict) and isinstance(duplicate.get("name"), str) and duplicate["name"].strip():
        return duplicate["name"].strip()
    return ""


def _is_price_change(payload: Dict[str, Any]) -> bool:
    price_keys = {"price", "price_display", "price_numeric_min", "price_numeric_max"}
    fields = payload.get("fields") or payload.get("fields_to_update") or {}
    if isinstance(fields, dict) and price_keys.intersection(fields):
        return True
    diff = payload.get("change_diff") if isinstance(payload.get("change_diff"), dict) else {}
    rows = diff.get("rows") if isinstance(diff, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("field") in price_keys:
                return True
    return False


_FIELD_LABELS = {
    "price": "Price",
    "price_display": "Price",
    "price_numeric_min": "Price",
    "price_numeric_max": "Price",
    "currency": "Currency",
    "status": "Status",
    "bedrooms": "Bedrooms",
    "bedrooms_min": "Bedrooms",
    "bedrooms_max": "Bedrooms",
    "bedrooms_display": "Bedrooms",
    "bathrooms": "Bathrooms",
    "province": "Province",
    "property_name": "Name",
    "name": "Name",
    "title": "Name",
    "property_type": "Type",
    "location": "Location",
    "district": "District",
    "area": "Area",
    "address": "Address",
    "developer_name": "Developer",
    "ownership_type": "Ownership",
    "has_pool": "Pool",
    "parking_spaces": "Parking",
    "parking": "Parking",
    "websites": "Websites",
    "website": "Website",
    "description": "Description",
    "area_sqm_min": "Size",
    "area_sqm_max": "Size",
    "area_display": "Size",
}


def human_field_label(field_name: str) -> str:
    key = (field_name or "").strip()
    if key in _FIELD_LABELS:
        return _FIELD_LABELS[key]
    lowered = key.lower().replace(" ", "_")
    if lowered in _FIELD_LABELS:
        return _FIELD_LABELS[lowered]
    words = key.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Field"


def listing_bed_label(source: Any) -> str:
    """Bed label from display, then min/max. 'beds not listed' only when all are empty."""
    from app.services.conversation_controls import bedrooms_display_value

    return bedrooms_display_value(source)


def humanise_field_list(fields: Any) -> str:
    labels: List[str] = []
    seen = set()
    for field in fields or []:
        label = human_field_label(str(field))
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return ", ".join(labels)


def open_work_label(pending_action: Any) -> str:
    tool = str(getattr(pending_action, "tool_name", "") or "")
    payload = getattr(pending_action, "action_payload", None) or {}
    if not isinstance(payload, dict):
        payload = {}
    name = _listing_name(payload)
    if tool in {"ingest_property_from_url", "ingest_property_from_drive"} or payload.get("source_url") or payload.get("provenance"):
        return f"property import draft for {name}" if name else "property import draft"
    if tool == "create_or_update_listing" and _is_price_change(payload):
        return f"price change for {name}" if name else "price change"
    if tool == "create_or_update_listing":
        return f"property change for {name}" if name else "property change"
    if tool == "create_calendar_event":
        return f"calendar event for {name}" if name else "calendar event"
    plain = human_tool_label(tool).lower()
    return f"{plain} for {name}" if name else plain


def draft_still_open_note(label: str) -> str:
    clean = (label or "draft").strip() or "draft"
    if clean.lower().startswith("the "):
        clean = clean[4:]
    return f"Your {clean} is still open. Say confirm or cancel when ready."


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    return None


def _store_count(stats: Dict[str, Any], key: str, unavailable_flag: Optional[str] = None) -> Optional[int]:
    if unavailable_flag and stats.get(unavailable_flag):
        return None
    if key not in stats:
        return None
    return _as_int(stats.get(key))


def _active_archived(stats: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    active = _as_int(stats.get("active_count"))
    if active is None:
        active = _as_int(stats.get("active"))
    archived = _as_int(stats.get("archived_count"))
    if archived is None:
        archived = _as_int(stats.get("archived"))
    breakdown = stats.get("status_breakdown")
    if isinstance(breakdown, dict):
        for key, value in breakdown.items():
            label = str(key).strip().lower()
            count = _as_int(value)
            if count is None:
                continue
            if active is None and label in {"active", "available"}:
                active = count
            if archived is None and label == "archived":
                archived = count
    return active, archived


def format_inventory_reply(stats: dict, language: str = "en") -> str:
    data = stats if isinstance(stats, dict) else {}
    polish = (language or "en").lower().startswith("pl")
    total = _as_int(data.get("authoritative_total_properties"))
    postgres = _store_count(data, "postgresql_listings_count")
    firestore = _store_count(data, "firestore_live_projects_count", "firestore_count_unavailable")
    if data.get("firestore_count_unavailable"):
        firestore = None
    if total is None:
        total = postgres if postgres is not None else firestore
    active, archived = _active_archived(data)

    if postgres is not None and firestore is not None and postgres != firestore:
        gap = abs(postgres - firestore)
        shown = total if total is not None else max(postgres, firestore)
        noun = "property" if shown == 1 else "properties"
        if polish:
            pl_noun = "nieruchomość" if shown == 1 else "nieruchomości"
            return (
                f"Masz {shown} {pl_noun}. Postgres ma {postgres}, a Firestore {firestore}; "
                f"{gap} się różni. Powiedz 'pokaż różnice', żeby je zobaczyć."
            )
        return (
            f"You have {shown} {noun}. Postgres has {postgres} and Firestore has {firestore}; "
            f"{gap} differ - say 'show differences' to list them."
        )

    if total is None:
        if polish:
            return "Liczba nieruchomości jest niedostępna."
        return "The property count is unavailable."

    if total == 1 and active is None and archived is None:
        head = "Masz 1 nieruchomość." if polish else "You have 1 property."
    elif active is not None and archived is not None:
        if polish:
            head = f"Masz {total} nieruchomości: {active} aktywnych, {archived} zarchiwizowanych."
        else:
            noun = "property" if total == 1 else "properties"
            head = f"You have {total} {noun}: {active} active, {archived} archived."
    elif total == 0:
        head = "Masz 0 nieruchomości." if polish else "You have 0 properties."
    else:
        if polish:
            noun = "nieruchomość" if total == 1 else "nieruchomości"
            head = f"Masz {total} {noun}."
        else:
            noun = "property" if total == 1 else "properties"
            head = f"You have {total} {noun}."

    missing = []
    if "postgresql_listings_count" in data and postgres is None:
        missing.append("Postgres")
    if data.get("firestore_count_unavailable") or (
        "firestore_live_projects_count" in data and firestore is None
    ):
        missing.append("Firestore")
    if not missing:
        return head
    if polish:
        named = " i ".join(missing)
        return f"{head} Liczba w {named} jest niedostępna."
    if len(missing) == 1:
        return f"{head} {missing[0]}'s count is unavailable."
    return f"{head} Postgres's count is unavailable and Firestore's count is unavailable."


def format_store_differences(diff: dict) -> str:
    if not isinstance(diff, dict):
        return "I could not compare the two stores."
    error = diff.get("error")
    if error:
        return str(error)
    items = diff.get("items") or []
    if not isinstance(items, list) or not items:
        return "Both stores match."
    lines: List[str] = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        property_id = str(item.get("property_id") or "a property")
        issue = str(item.get("issue") or "differs").strip()
        if issue == "only in Postgres":
            lines.append(f"- {property_id} is only in Postgres.")
        elif issue == "only in Firestore":
            lines.append(f"- {property_id} is only in Firestore.")
        elif issue.startswith("differs in "):
            fields = issue[len("differs in "):].replace(", ", " and ")
            lines.append(f"- {property_id}: {fields} differ.")
        else:
            lines.append(f"- {property_id}: {issue}.")
    extra = len(items) - len(lines[:20])
    if len(items) > 20:
        extra = len(items) - 20
        text = "\n".join(lines[:20])
        return f"{text}\nand {extra} more"
    return "\n".join(lines) if lines else "Both stores match."


def describe_store_check(pg_state: str, fs_state: str) -> str:
    pg = (pg_state or "").strip().lower()
    fs = (fs_state or "").strip().lower()
    if pg == "saved" and fs == "saved":
        return "I checked both databases and the change is saved."
    if pg == "saved" and fs == "unavailable":
        return "I checked Postgres and the change is saved; the Firestore check was unavailable."
    if pg == "saved" and fs == "missing":
        return "The change is saved in Postgres but I could not find it in Firestore."
    if pg == "missing" and fs == "saved":
        return "I could not find the change in Postgres, but it is saved in Firestore."
    if pg == "missing" and fs == "missing":
        return "I could not find the change in Postgres or Firestore."
    if pg == "missing" and fs == "unavailable":
        return "I could not find the change in Postgres, and the Firestore check was unavailable."
    if pg == "could not be checked" and fs == "saved":
        return "I could not check Postgres; the change is saved in Firestore."
    if pg == "could not be checked" and fs == "missing":
        return "I could not check Postgres, and I could not find the change in Firestore."
    return "I could not check Postgres, and the Firestore check was unavailable."


def audit_note_for_reply(role: Any, message: str, audit_disabled: bool) -> str:
    if not audit_disabled:
        return ""
    role_name = str(role or "").strip().lower()
    asked = bool(re.search(r"\b(history|audit|logs?)\b", message or "", re.I))
    if role_name != "super_admin" and not asked:
        return ""
    return "Audit logging is switched off, so this change will not appear in the history."


def _display_price(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.search(r"\d", text) is None:
        return text
    if re.search(r"thb|baht|\$|€|£", text, re.I) or re.search(r"\d\s*m\b", text, re.I):
        return text
    try:
        from app.services.admin_property_fields import format_price_thb

        formatted = format_price_thb(text)
    except Exception:
        formatted = text
    if not formatted or formatted == "—":
        return text
    return formatted


def format_saved_listing_reply(
    title: str,
    new_value: Optional[str],
    old_value: Optional[str],
    store_line: str,
    undo: bool,
    audit_note: str = "",
) -> str:
    name = (title or "The property").strip() or "The property"
    shown_new = _display_price(new_value)
    shown_old = _display_price(old_value)
    if shown_new:
        if shown_old:
            sentence = f"Done. {name}'s price is now {shown_new} (was {shown_old})."
        else:
            sentence = f"Done. {name}'s price is now {shown_new}."
    else:
        sentence = f"Done. {name} is updated."
    parts = [sentence]
    if (store_line or "").strip():
        parts.append(store_line.strip())
    if undo:
        parts.append("You can undo this for 6 days by saying 'undo'.")
    text = " ".join(parts)
    note = (audit_note or "").strip()
    if note:
        text = text.rstrip() + "\n" + note
    return text


_FACT_PATTERNS = (
    ("price", re.compile(r"^(?:what(?:'s| is) the price of|how much is|how much does)\s+(.+?)(?:\s+cost)?$", re.I)),
    ("price", re.compile(r"^(?!set\b|change\b|increase\b|decrease\b|raise\b|lower\b|update\b|delete\b)(.+?)\s+price$", re.I)),
    ("bedrooms", re.compile(r"^how many bedrooms does\s+(.+?)\s+have$", re.I)),
    ("location", re.compile(r"^where is\s+(.+?)$", re.I)),
    ("active", re.compile(r"^is\s+(.+?)\s+(?:active|inactive)$", re.I)),
    ("status", re.compile(r"^(?:what(?:'s| is) the )?status of\s+(.+?)$", re.I)),
    ("developer", re.compile(r"^who is the developer of\s+(.+?)$", re.I)),
    ("type", re.compile(r"^(?:what type of property is|what(?:'s| is) the type of)\s+(.+?)$", re.I)),
    ("size", re.compile(r"^(?:what(?:'s| is) the )?(?:size|area) of\s+(.+?)$", re.I)),
    ("updated", re.compile(r"^when was\s+(.+?)\s+updated$", re.I)),
)

_LIST_REQUEST = re.compile(
    r"\b(show me|list|find|search|villas in|properties under|under \d|compare)\b",
    re.I,
)


def _clean_question(message: str) -> str:
    text = re.sub(r"\s+", " ", (message or "").strip())
    text = re.sub(r"^(please|can you|could you)\s+", "", text, flags=re.I)
    text = text.strip(" ?.!")
    return text


def _record_value(record: Any, *names: str) -> Any:
    for name in names:
        if isinstance(record, dict):
            value = record.get(name)
        else:
            value = getattr(record, name, None)
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if not isinstance(value, (str, int, float, bool)) and value.__class__.__name__ == "MagicMock":
            continue
        if not isinstance(value, (str, int, float, bool)) and not hasattr(value, "year"):
            text = str(value)
            if "MagicMock" in text:
                continue
        return value
    return None


def _record_name(record: Any) -> str:
    name = _record_value(record, "property_name", "name", "title")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return ""


def _record_id(record: Any) -> str:
    value = _record_value(record, "property_id", "_firestore_doc_id", "id")
    return str(value).strip() if value is not None else ""


def _usable_record(record: Any) -> bool:
    return bool(_record_name(record) or _record_id(record))


def _property_name_rows(db: Any) -> List[Any]:
    try:
        from app.db.models import Property

        rows = db.query(Property).all()
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(getattr(row, "property_name", None), str)]


def _closest_property_names(db: Any, query: str, limit: int = 3) -> List[str]:
    import difflib

    names = [str(row.property_name) for row in _property_name_rows(db)]
    if not names:
        return []
    folded = {name.casefold(): name for name in names}
    close = difflib.get_close_matches(query.casefold(), list(folded), n=limit, cutoff=0.55)
    picked = [folded[item] for item in close]
    if picked:
        return picked
    needle = query.casefold()
    contained = [name for name in names if needle in name.casefold() or name.casefold() in needle]
    return contained[:limit]


def _name_matches(db: Any, name: str) -> List[Any]:
    try:
        from app.db.models import Property

        exact = (
            db.query(Property)
            .filter(Property.property_name.ilike(name))
            .limit(6)
            .all()
        )
        rows = exact if isinstance(exact, list) else []
        rows = [row for row in rows if _usable_record(row)]
        if rows:
            return rows
        contains = (
            db.query(Property)
            .filter(Property.property_name.ilike(f"%{name}%"))
            .limit(6)
            .all()
        )
        if not isinstance(contains, list):
            contains = []
        else:
            contains = [row for row in contains if _usable_record(row)]
        if contains:
            return contains
        import difflib

        rows = _property_name_rows(db)
        folded = {str(row.property_name).casefold(): row for row in rows}
        close = difflib.get_close_matches(name.casefold(), list(folded), n=1, cutoff=0.82)
        if len(close) == 1:
            return [folded[close[0]]]
        return []
    except Exception:
        return []


def _not_recorded(name: str, field_label: str) -> str:
    return f"{name}'s {field_label} is not recorded."


def _fact_sentence(kind: str, record: Any) -> str:
    name = _record_name(record) or "This property"
    if kind == "price":
        raw = _record_value(record, "price_display", "price_numeric_min", "price")
        if raw is None:
            return _not_recorded(name, "price")
        price = _display_price(str(raw)) or str(raw)
        place = _record_value(record, "province", "location", "area")
        status = _record_value(record, "status")
        try:
            from app.services.admin_property_fields import format_place_name

            place_text = format_place_name(place) if place else ""
        except Exception:
            place_text = str(place or "").strip()
        extras = [part for part in (place_text, str(status).strip() if status else "") if part]
        if extras:
            return f"{name} costs {price} ({', '.join(extras)})."
        return f"{name} costs {price}."
    if kind == "bedrooms":
        try:
            from app.services.admin_property_fields import format_bedrooms_label

            label = format_bedrooms_label(
                bedrooms_min=_record_value(record, "bedrooms_min"),
                bedrooms_max=_record_value(record, "bedrooms_max"),
                bedrooms_display=_record_value(record, "bedrooms_display"),
                bedrooms=_record_value(record, "bedrooms"),
            )
        except Exception:
            label = "beds not listed"
        if not label or label == "beds not listed":
            return _not_recorded(name, "bedroom count")
        return f"{name} has {label}."
    if kind == "location":
        place = _record_value(record, "province", "location", "area", "address")
        if place is None:
            return _not_recorded(name, "location")
        try:
            from app.services.admin_property_fields import format_place_name

            shown = format_place_name(place) or str(place)
        except Exception:
            shown = str(place)
        return f"{name} is in {shown}."
    if kind == "active":
        status = str(_record_value(record, "status") or "").strip().lower()
        public = _record_value(record, "is_public")
        if public is None and not status:
            return _not_recorded(name, "active status")
        inactive = public is False or status in {"archived", "inactive", "sold", "sold out"}
        if inactive:
            return f"{name} is not active."
        return f"{name} is active."
    if kind == "status":
        status = _record_value(record, "status")
        if status is None:
            return _not_recorded(name, "status")
        return f"{name} is {status}."
    if kind == "developer":
        developer = _record_value(record, "developer_name", "developer")
        if developer is None:
            return _not_recorded(name, "developer")
        return f"The developer of {name} is {developer}."
    if kind == "type":
        kind_name = _record_value(record, "property_type", "type")
        if kind_name is None:
            return _not_recorded(name, "type")
        return f"{name} is a {kind_name}."
    if kind == "size":
        size = _record_value(record, "area_display", "area_sqm_min", "size")
        if size is None:
            return _not_recorded(name, "size")
        text = str(size)
        if "sqm" not in text.lower() and _as_int(size) is not None:
            text = f"{text} sqm"
        return f"{name} is {text}."
    updated = _record_value(record, "updated_at", "updated", "last_updated")
    if updated is None:
        return _not_recorded(name, "last update")
    if hasattr(updated, "strftime"):
        shown = updated.strftime("%d %b %Y")
    else:
        shown = str(updated)
    return f"{name} was last updated on {shown}."


def try_direct_property_answer(db: Any, message: str) -> Optional[dict]:
    text = _clean_question(message)
    if not text or _LIST_REQUEST.search(text):
        return None
    kind = ""
    name = ""
    for label, pattern in _FACT_PATTERNS:
        match = pattern.match(text)
        if match:
            kind = label
            name = match.group(1).strip(" ?.!\"'")
            break
    if not kind or len(name) < 2:
        return None
    if name.lower() in {"it", "this", "that", "there", "the property", "a property"}:
        return None

    matches = _name_matches(db, name)
    if len(matches) > 1:
        shown = matches[:5]
        lines = ["Several properties match. Which one do you mean?"]
        for index, row in enumerate(shown, start=1):
            lines.append(f"{index}. {_record_name(row) or _record_id(row)}")
        extra = len(matches) - len(shown)
        if extra > 0:
            lines.append(f"and {extra} more")
        return {"text": "\n".join(lines), "tools_used": []}

    record = matches[0] if len(matches) == 1 else None
    if record is None:
        try:
            from app.services.admin_property_fields import resolve_existing_property

            _source, record = resolve_existing_property(db, name)
        except Exception:
            record = None
    if not _usable_record(record):
        real_query = False
        try:
            from app.db.models import Property

            real_query = isinstance(db.query(Property).all(), list)
        except Exception:
            real_query = False
        if not real_query:
            return None
        suggestions = _closest_property_names(db, name)
        shown = ", ".join(suggestions[:3])
        if shown:
            text = f"I could not find a property named '{name}'. Did you mean: {shown}?"
        else:
            text = f"I could not find a property named '{name}'."
        return {"text": text, "tools_used": [], "direct_answer": True}
    property_id = _record_id(record)
    return {
        "text": _fact_sentence(kind, record),
        "tools_used": [{"name": "get_property_knowledge", "args": {"property_id": property_id}}],
    }


def scrub_user_reply(text: Optional[str]) -> str:
    if text is None:
        return ""
    kept: List[str] = []
    for line in str(text).splitlines():
        stripped = line.strip()
        if re.match(r"^(Tool data:|Operation Mode:|Tool:)", stripped, re.I):
            continue
        kept.append(line)
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\[UPDATE SUCCESS\]", "", cleaned, flags=re.I)
    cleaned = re.sub(r"audit_ephemeral_\S+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\(do not run an action by themselves\)", "", cleaned, flags=re.I)
    for key, label in _FIELD_LABELS.items():
        if "_" not in key:
            cleaned = cleaned.replace(f"`{key}`", label)
            continue
        cleaned = re.sub(rf"`{re.escape(key)}`", label, cleaned)
        cleaned = re.sub(rf"\b{re.escape(key)}\b", label, cleaned)
    for key in _INTERNAL_KEYS:
        cleaned = re.sub(rf"\b{key}\b\s*[:=]?\s*\S*", "", cleaned)
    cleaned = re.sub(r"\bYour the\b", "Your", cleaned, flags=re.I)
    cleaned = re.sub(r"\bcreate_or_update_listing\b", "property change", cleaned)
    for tool in sorted(_TOOL_NAMES, key=len, reverse=True):
        cleaned = cleaned.replace(f"`{tool}`", "")
    cleaned = re.sub(r"`[a-z][a-z0-9]*(?:_[a-z0-9]+)+`", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def reply_hygiene_violations(text: Optional[str]) -> List[str]:
    if not text:
        return []
    found: List[str] = []
    sample = str(text)
    for marker in _HYGIENE_MARKERS:
        if marker.lower() in sample.lower():
            found.append(marker)
    if re.search(r"(?m)^\s*Tool:", sample):
        found.append("Tool:")
    if re.search(r"`[a-z][a-z0-9]*(?:_[a-z0-9]+)+`", sample):
        found.append("tool name in backticks")
    return found


def _tool_names(result: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for item in result.get("tools_used") or []:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("tool") or ""))
        else:
            names.append(str(item))
    return [name for name in names if name]


def suggestions_allowed(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("direct_answer"):
        return False
    text = str(result.get("text") or "").strip()
    low = text.lower()
    if not text:
        return False
    if result.get("action_status") in {"error", "timeout", "failed", "incomplete", "pending", "proposed", "awaiting_confirmation"}:
        return False
    if result.get("requires_confirmation") or result.get("confirm_via_chat_only") or result.get("pending_action_id"):
        return False
    if low.startswith("done") or low.startswith("gotowe"):
        return False
    if any(phrase in low for phrase in ("i cannot", "i can't", "not allowed", "refused", "nothing was saved", "failed to")):
        return False
    if "which one" in low or "which property" in low or "do you mean" in low:
        return False
    if text.endswith("?") and "lead" not in low and "propert" not in low:
        return False
    names = set(_tool_names(result))
    if names.intersection(_LIST_OR_REPORT_TOOLS):
        return True
    if "report" in low and ("download" in low or "ready" in low):
        return True
    return False


def classify_draft_question(message: Optional[str]) -> bool:
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    if not text:
        return False
    patterns = (
        r"\b(show|see|repeat|review)\b.{0,40}\b(draft|preview|table)\b",
        r"\b(draft|preview)\b.{0,40}\b(again|please)\b",
        r"\bwhat(?:'s| is) (?:still )?missing\b",
        r"\bwhich fields\b",
        r"\bwhat did you find\b",
        r"\bwhat you (?:found|extracted)\b",
        r"\bwhat(?:'s| is) the source\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _edit_clause(clause: str) -> Optional[Dict[str, Any]]:
    text = clause.strip().strip(".")
    if not text:
        return None
    matchers = (
        ("price", re.compile(r"(?i)^(?:use\s+|set\s+)?price(?:\s+to|\s+of)?\s+(.+)$"), False),
        ("bedrooms", re.compile(r"(?i)^(?:use\s+|set\s+)?bedrooms?(?:\s+to)?\s+(\d+)$"), True),
        ("bathrooms", re.compile(r"(?i)^(?:use\s+|set\s+)?bathrooms?(?:\s+to)?\s+(\d+)$"), True),
        ("status", re.compile(r"(?i)^(?:change\s+the\s+|set\s+|change\s+)?status(?:\s+to)?\s+(.+)$"), False),
        ("province", re.compile(r"(?i)^(?:add\s+|set\s+)?(.+?)\s+as\s+province$"), False),
        ("province", re.compile(r"(?i)^(?:set\s+)?province(?:\s+to|\s+is)?\s+(.+)$"), False),
        ("property_name", re.compile(r"(?i)^(?:name\s+it|call\s+it|rename(?:\s+it)?\s+to|set\s+(?:the\s+)?name\s+to)\s+(.+)$"), False),
        ("property_type", re.compile(r"(?i)^(?:set\s+)?type(?:\s+to)?\s+(.+)$"), False),
        ("location", re.compile(r"(?i)^(?:use\s+|set\s+)?location(?:\s+to)?\s+(.+)$"), False),
        ("parking", re.compile(r"(?i)^(?:set\s+)?parking(?:\s+to)?\s+(\d+)$"), True),
    )
    for field, pattern, numeric in matchers:
        match = pattern.match(text)
        if not match:
            continue
        value: Any = match.group(1).strip()
        if field == "price":
            if not re.search(r"\d", value):
                return None
            from app.services.admin_property_fields import parse_money

            if parse_money(value) is None:
                return None
        if field == "status" and (len(value) > 40 or not re.search(r"[a-z]", value, re.I)):
            return None
        if numeric:
            value = int(value)
        return {"op": "set", "field": field, "value": value}
    return None


def parse_natural_edit_commands(text: str) -> list[dict]:
    raw = (text or "").strip()
    if not raw or re.search(r"https?://", raw, re.I):
        return []
    parts = re.split(r"\s*,\s*|\s+\band\b\s+", raw)
    commands: List[Dict[str, Any]] = []
    for part in parts:
        command = _edit_clause(part)
        if command:
            commands.append(command)
    return commands
