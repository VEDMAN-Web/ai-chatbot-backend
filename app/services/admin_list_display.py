"""Small display helpers shared by admin chat lists."""

from __future__ import annotations

import re
from typing import Any, Optional

_EMPTY_BED_LABELS = {
    "",
    "n/a",
    "none",
    "null",
    "beds not listed",
    "not listed",
    "1-4",
    "1-4 beds",
    "1–4",
    "1–4 beds",
}


def _read(source: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(source, dict):
            value = source.get(key)
        else:
            value = getattr(source, key, None)
        if value not in (None, ""):
            return value
    return None


def _usable_display(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if text.lower() in _EMPTY_BED_LABELS:
        return None
    return text


def bedrooms_display_value(source: Any) -> str:
    """
    Bed label for an admin search row.
    Use a real bedrooms_display first, then bedrooms_min / bedrooms_max.
    'beds not listed' only when display, min, and max are all empty.
    """
    from app.services.admin_property_fields import format_bedrooms_label

    display = _usable_display(_read(source, "bedrooms_display", "bedroomsDisplay"))
    if display:
        label = format_bedrooms_label(bedrooms_display=display)
        if label != "beds not listed":
            return _tidy(label)

    label = format_bedrooms_label(
        bedrooms_min=_read(source, "bedrooms_min", "bedroomsMin"),
        bedrooms_max=_read(source, "bedrooms_max", "bedroomsMax"),
        bedrooms=_numeric_bedrooms(_read(source, "bedrooms")),
    )
    return _tidy(label)


def _numeric_bedrooms(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().lower()
    if text in _EMPTY_BED_LABELS or "bed" in text and not re.search(r"\d", text):
        return None
    return value


def overlay_missing_bedrooms(db: Any, cards: Any) -> Any:
    """Copy Firestore bedroom counts onto search cards that do not have them."""
    if db is None or not isinstance(cards, list):
        return cards
    try:
        from app.repositories import properties_store

        rows = properties_store.list_rows(db)
    except Exception:
        return cards
    if not isinstance(rows, list):
        return cards
    by_id = {}
    for row in rows:
        pid = getattr(row, "property_id", None)
        if isinstance(pid, str):
            by_id[pid] = row
    for card in cards:
        if not isinstance(card, dict):
            continue
        if _read(card, "bedrooms_min", "bedroomsMin") is not None or _read(card, "bedrooms_max", "bedroomsMax") is not None:
            continue
        row = by_id.get(str(card.get("property_id") or card.get("id") or ""))
        if row is None:
            continue
        lo = getattr(row, "bedrooms_min", None)
        hi = getattr(row, "bedrooms_max", None)
        if isinstance(lo, (int, float)) and not isinstance(lo, bool):
            card["bedrooms_min"] = int(lo)
        if isinstance(hi, (int, float)) and not isinstance(hi, bool):
            card["bedrooms_max"] = int(hi)
        display = getattr(row, "bedrooms_display", None)
        if _usable_display(display) and not _usable_display(card.get("bedrooms_display")):
            card["bedrooms_display"] = display
    return cards


def _tidy(label: str) -> str:
    text = re.sub(r"\bbeds?\s+beds?\b", "beds", str(label or ""), flags=re.I).strip()
    return text or "beds not listed"
