"""
Canonical property field map for admin write/diff/knowledge paths.

Model aliases (price, location, bedrooms, …) are normalized to Firestore
column names. Unknown field names are rejected. Values are normalized before compare.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Canonical field names stored in diffs / updates / knowledge
CANONICAL_FIELDS: Set[str] = {
    "property_name",
    "project_name",
    "property_type",
    "status",
    "province",
    "district",
    "area",
    "location",  # display alias kept when only a single location string is known
    "price_display",
    "price_numeric_min",
    "price_numeric_max",
    "currency",
    "bedrooms_display",
    "bedrooms_min",
    "bedrooms_max",
    "bathrooms",
    "area_display",
    "area_sqm_min",
    "area_sqm_max",
    "has_pool",
    "sea_view",
    "beach_access",
    "description",
    "developer_name",
    "ownership_type",
    "websites",
}

# Alias → primary canonical field (may expand into multiple write keys)
_ALIAS_TO_CANONICAL: Dict[str, str] = {
    "price": "price_numeric_min",
    "price_thb": "price_numeric_min",
    "price_from": "price_numeric_min",
    "pricefrom": "price_numeric_min",
    "price_to": "price_numeric_max",
    "priceto": "price_numeric_max",
    "location": "province",
    "bedrooms": "bedrooms_min",
    "beds": "bedrooms_min",
    "bedroom": "bedrooms_min",
    "name": "property_name",
    "title": "property_name",
    "type": "property_type",
    "pool": "has_pool",
    "haspool": "has_pool",
}

# Attribute names used when reading property row objects
_PG_ATTRS: Dict[str, Sequence[str]] = {
    "property_name": ("property_name",),
    "project_name": ("project_name",),
    "property_type": ("property_type",),
    "status": ("status",),
    "province": ("province",),
    "district": ("district",),
    "area": ("area",),
    "location": ("province", "district", "area"),
    "price_display": ("price_display",),
    "price_numeric_min": ("price_numeric_min",),
    "price_numeric_max": ("price_numeric_max",),
    "currency": ("currency",),
    "bedrooms_display": ("bedrooms_display",),
    "bedrooms_min": ("bedrooms_min",),
    "bedrooms_max": ("bedrooms_max",),
    "bathrooms": ("bathrooms",),
    "area_display": ("area_display",),
    "area_sqm_min": ("area_sqm_min",),
    "area_sqm_max": ("area_sqm_max",),
    "has_pool": ("has_pool",),
    "sea_view": ("sea_view",),
    "beach_access": ("beach_access",),
    "description": ("description",),
    "developer_name": ("developer_name",),
    "ownership_type": ("ownership_type",),
    "websites": ("websites",),
}

# Marker when a field could not be read at proposal time — skipped in stale check
UNREADABLE = "__unreadable__"

_TRACKED_FOR_DIFF = (
    "property_name",
    "project_name",
    "property_type",
    "status",
    "province",
    "district",
    "area",
    "price_display",
    "price_numeric_min",
    "price_numeric_max",
    "currency",
    "bedrooms_display",
    "bedrooms_min",
    "bedrooms_max",
    "bathrooms",
    "area_display",
    "has_pool",
    "sea_view",
    "beach_access",
)


def localize_name(val: Any) -> Optional[str]:
    """Firestore name may be {pl, en} dict — prefer pl, then en, then first non-empty."""
    if val is None:
        return None
    if isinstance(val, dict):
        for k in ("pl", "en", "th", "name"):
            if val.get(k) and str(val.get(k)).strip():
                return str(val.get(k)).strip()
        for v in val.values():
            if v is not None and str(v).strip():
                return str(v).strip()
        return None
    s = str(val).strip()
    return s or None


# Same THB rates as intent_router.extract_search_filters_deterministic.
_FX_TO_THB = {"usd": 35.0, "eur": 38.0, "pln": 8.5, "thb": 1.0}
_UNIT_MULTIPLIER = {
    "million": 1_000_000,
    "millions": 1_000_000,
    "mln": 1_000_000,
    "mil": 1_000_000,
    "mn": 1_000_000,
    "m": 1_000_000,
    "thousand": 1_000,
    "tys": 1_000,
    "tys.": 1_000,
    "k": 1_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "b": 1_000_000_000,
    "mld": 1_000_000_000,
    "ล้าน": 1_000_000,
    "แสน": 100_000,
    "หมื่น": 10_000,
}
MONEY_PHRASE = (
    r"(?:฿|\$|€)?\s*[0-9][0-9,.\s'_]*\s*"
    r"(?:(?:millions|million|mln|mld|billion|thousand|tys\.?|ล้าน|แสน|หมื่น|bn|mn|mil|k|m|b)(?!\w))?"
    r"(?:\s*(?:thb|baht|฿|usd|dollars?|eur|euros?|€|บาท|pln|zł))?"
)
_MONEY_RE = re.compile(
    r"(?P<prefix>฿|\$|€)?\s*"
    r"(?P<number>[0-9][0-9,.\s'_]*)"
    r"\s*"
    r"(?:(?P<unit>millions|million|mln|mld|billion|thousand|tys\.?|ล้าน|แสน|หมื่น|bn|mn|mil|k|m|b)(?!\w))?"
    r"\s*"
    r"(?P<suffix>thb|baht|฿|usd|dollars?|eur|euros?|€|บาท|pln|zł)?",
    re.IGNORECASE,
)


def thb_per_unit(currency: str) -> float:
    """THB per 1 unit of currency. Settings can override the project rates."""
    code = (currency or "thb").lower()
    try:
        from app.core.config import settings

        custom = getattr(settings, "FX_RATES", None)
        if isinstance(custom, dict) and code in custom:
            return float(custom[code])
        override = getattr(settings, f"FX_{code.upper()}_TO_THB", None)
        if override:
            return float(override)
    except Exception:
        pass
    return _FX_TO_THB.get(code, 1.0)


def _parse_number_token(token: str) -> Optional[float]:
    text = (token or "").strip().replace("\u00a0", "").replace("_", "").replace("'", "").replace("’", "")
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


def _currency_code(prefix: str, suffix: str) -> str:
    blob = f"{prefix or ''} {suffix or ''}".lower()
    if "$" in blob or "usd" in blob or "dollar" in blob:
        return "USD"
    if "€" in blob or "eur" in blob or "euro" in blob:
        return "EUR"
    if "pln" in blob or "zł" in blob:
        return "PLN"
    return "THB"


def short_money(value: float) -> str:
    number = abs(float(value))
    sign = "-" if float(value) < 0 else ""

    def compact(amount: float, word: str) -> str:
        text = f"{amount:.2f}".rstrip("0").rstrip(".")
        if word == "k":
            return f"{sign}{text}k"
        return f"{sign}{text} {word}"

    if number >= 1_000_000_000:
        return compact(number / 1_000_000_000, "billion")
    if number >= 1_000_000:
        return compact(number / 1_000_000, "million")
    if number >= 1_000 and abs(number - round(number / 1000) * 1000) < 1:
        return compact(number / 1000, "k")
    return f"{sign}{number:,.0f} THB"


def describe_money(raw: Any) -> Optional[Dict[str, Any]]:
    """One money reading: THB amount, currency, and a conversion note when FX applies."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            value = float(raw)
        except Exception:
            return None
        return {
            "thb": value,
            "source_amount": value,
            "currency": "THB",
            "rate": 1.0,
            "multiplier": False,
            "note": "",
            "short": short_money(value),
        }
    text = str(raw).strip().replace("\u00a0", " ")
    if not text or text.lower() in {"none", "null", "n/a", "-", "—"}:
        return None
    best: Optional[Dict[str, Any]] = None
    for match in _MONEY_RE.finditer(text):
        number = _parse_number_token(match.group("number") or "")
        if number is None:
            continue
        unit = (match.group("unit") or "").lower()
        multiplier = _UNIT_MULTIPLIER.get(unit, 1.0)
        source = number * multiplier
        currency = _currency_code(match.group("prefix") or "", match.group("suffix") or "")
        rate = thb_per_unit(currency)
        thb = source * rate
        note = ""
        if currency != "THB":
            note = f"Converted from {source:,.0f} {currency} at {rate:g} THB per {currency}."
        candidate = {
            "thb": thb,
            "source_amount": source,
            "currency": currency,
            "rate": rate,
            "multiplier": bool(match.group("unit") or match.group("prefix") or match.group("suffix")),
            "note": note,
            "short": short_money(thb),
        }
        explicit = bool(match.group("unit") or match.group("prefix") or match.group("suffix"))
        candidate["multiplier"] = explicit
        if best is None or explicit or not best.get("multiplier"):
            best = candidate
    return best


def parse_money(raw: Any) -> Optional[float]:
    """Parse a price phrase to THB. USD and EUR use the project FX rates."""
    details = describe_money(raw)
    if not details:
        return None
    return float(details["thb"])


def money_phrase(value: float, details: Optional[Dict[str, Any]] = None) -> str:
    """Full digits and the short form, plus an FX note when one was applied."""
    amount = float(value)
    phrase = f"{amount:,.0f} THB ({short_money(amount)})"
    note = (details or {}).get("note") or ""
    if note:
        phrase = f"{phrase}. {note}"
    return phrase


def price_sanity_message(new_thb: float, current: Optional[float] = None) -> Optional[str]:
    """Refuse a proposal when the new price is implausible. None means it can be proposed."""
    try:
        amount = float(new_thb)
    except (TypeError, ValueError):
        return "I could not read that price. Nothing was changed."
    shown = f"{amount:,.0f} THB"
    if amount < 100_000:
        if amount > 0:
            suggested = amount * 1_000_000
            return f"{shown} looks too low. Did you mean {suggested:,.0f} THB? Nothing was changed."
        return f"{shown} looks too low. Nothing was changed."
    if amount > 1_000_000_000:
        return f"{shown} looks too high. Please confirm the amount you want. Nothing was changed."
    if current is not None:
        try:
            stored = float(current)
        except (TypeError, ValueError):
            stored = 0
        if stored > 0 and (amount / stored > 10 or amount / stored < 0.1):
            return (
                f"{shown} is more than ten times away from the current price of {stored:,.0f} THB. "
                f"Did you mean {shown}? Nothing was changed."
            )
    return None


def parse_price_amount(raw: Any) -> Optional[float]:
    """
    Parse '5 million', '5M', '5 mln', '5,000,000', 5000000 → float THB.
    Returns None if unclear. Percent phrases are not absolute amounts.
    """
    if isinstance(raw, str) and "%" in raw and re.search(r"\b(increase|decrease|raise|lower|by)\b", raw, re.I):
        return None
    return parse_money(raw)


def parse_relative_price_change(message: str, current: Optional[float]) -> Optional[float]:
    """'increase price by 80%' / 'decrease by 10%' against stored current."""
    if current is None or current <= 0:
        return None
    m = re.search(
        r"(increase|raise|bump|up|decrease|lower|reduce|down)\s+(?:the\s+)?(?:price\s+)?(?:by\s+)?(\d+(?:\.\d+)?)\s*%",
        (message or "").lower(),
    )
    if not m:
        m = re.search(
            r"(?:price\s+)?(?:up|down)\s+(\d+(?:\.\d+)?)\s*%",
            (message or "").lower(),
        )
        if not m:
            return None
        pct = float(m.group(1))
        # ambiguous direction — treat as increase if "up"
        direction = "increase" if "up" in (message or "").lower() else "decrease"
    else:
        direction = m.group(1)
        pct = float(m.group(2))
    factor = pct / 100.0
    if direction in ("increase", "raise", "bump", "up"):
        return round(current * (1.0 + factor), 2)
    return round(current * (1.0 - factor), 2)


def parse_bedrooms(raw: Any) -> Dict[str, Any]:
    """Accept '3', '3-4', '3 to 4' → bedrooms_min/max/display."""
    out: Dict[str, Any] = {}
    if raw is None:
        return out
    if isinstance(raw, (int, float)):
        n = int(raw)
        out["bedrooms_min"] = n
        out["bedrooms_max"] = n
        out["bedrooms_display"] = str(n)
        return out
    s = str(raw).strip()
    if not s:
        return out
    m = re.search(r"(\d+)\s*(?:-|–|to|až|do)\s*(\d+)", s, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        out["bedrooms_min"] = min(a, b)
        out["bedrooms_max"] = max(a, b)
        out["bedrooms_display"] = f"{min(a, b)}-{max(a, b)}"
        return out
    m = re.search(r"(\d+)", s)
    if m:
        n = int(m.group(1))
        out["bedrooms_min"] = n
        out["bedrooms_max"] = n
        out["bedrooms_display"] = str(n)
    return out


def format_bedrooms_label(
    bedrooms_min: Any = None,
    bedrooms_max: Any = None,
    bedrooms_display: Any = None,
    bedrooms: Any = None,
) -> str:
    """
    '2-3 beds', '3 beds', or 'beds not listed'.
    Never invents 1-4 and never appends a second 'bed'.
    """
    def _int(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text or text.lower() in {"n/a", "none", "null", "beds not listed"}:
            return None
        parsed = parse_bedrooms(text)
        if parsed.get("bedrooms_min") is None:
            return None
        return int(parsed["bedrooms_min"])

    lo = _int(bedrooms_min)
    hi = _int(bedrooms_max)
    if lo is None and hi is None:
        raw = bedrooms_display if bedrooms_display not in (None, "") else bedrooms
        blob = str(raw or "").strip().lower()
        if blob in {"", "n/a", "none", "1-4", "1-4 beds", "1–4 beds"}:
            return "beds not listed"
        parsed = parse_bedrooms(raw)
        lo = parsed.get("bedrooms_min")
        hi = parsed.get("bedrooms_max")
        if lo is None:
            return "beds not listed"
    if hi is None or int(hi) == int(lo):
        return f"{int(lo)} beds"
    return f"{min(int(lo), int(hi))}-{max(int(lo), int(hi))} beds"


def format_place_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(
        r"[A-Za-z]+",
        lambda match: match.group(0) if any(ch.isupper() for ch in match.group(0)[1:]) else match.group(0).capitalize(),
        text,
    )


def resolve_alias(field: str) -> Optional[str]:
    f = (field or "").strip()
    if not f:
        return None
    if f in CANONICAL_FIELDS:
        return f
    return _ALIAS_TO_CANONICAL.get(f.lower().replace(" ", "_"))


def expand_proposed_fields(
    raw_fields: Optional[Dict[str, Any]],
    *,
    current_stored: Optional[Dict[str, Any]] = None,
    user_message: Optional[str] = None,
    allow_websites: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Map model field names → canonical write keys.
    Returns (canonical_fields, errors). Rejects unknown names.
    Does NOT inject websites unless allow_websites and explicitly present.
    """
    errors: List[str] = []
    out: Dict[str, Any] = {}
    if not raw_fields:
        return out, ["fields_to_update is empty"]

    # Relative % change from message
    if user_message and current_stored:
        cur_price = current_stored.get("price_numeric_min")
        try:
            cur_f = float(cur_price) if cur_price is not None else None
        except (TypeError, ValueError):
            cur_f = parse_price_amount(cur_price)
        new_from_pct = parse_relative_price_change(user_message, cur_f)
        if new_from_pct is not None:
            out["price_numeric_min"] = new_from_pct
            out["price_display"] = f"{new_from_pct:,.0f} THB"

    for key, val in (raw_fields or {}).items():
        if key in ("mode", "property_id", "images", "image_urls", "cover_image"):
            continue
        if key == "websites" or key == "selected_websites":
            if allow_websites and val is not None:
                out["websites"] = val
            # silently drop injected websites when not explicitly allowed
            continue

        canon = resolve_alias(key)
        if canon is None:
            errors.append(
                f"Unknown field '{key}'. Allowed: {', '.join(sorted(CANONICAL_FIELDS))} "
                f"(aliases: price, location, bedrooms, …)."
            )
            continue

        if canon in ("price_numeric_min", "price_numeric_max") or key.lower() in (
            "price", "price_thb", "price_display"
        ):
            # Relative phrase in the value itself
            if isinstance(val, str) and "%" in val and current_stored:
                cur_f = parse_price_amount(current_stored.get("price_numeric_min"))
                rel = parse_relative_price_change(val, cur_f) or parse_relative_price_change(
                    f"increase price {val}", cur_f
                )
                if rel is not None:
                    out["price_numeric_min"] = rel
                    out["price_display"] = f"{rel:,.0f} THB"
                    continue
            if key.lower() == "price_display" and not isinstance(val, (int, float, Decimal)):
                out["price_display"] = localize_name(val) or str(val)
                parsed = parse_price_amount(val)
                if parsed is not None:
                    out.setdefault("price_numeric_min", parsed)
                continue
            parsed = parse_price_amount(val)
            if parsed is None and val is not None and str(val).strip():
                # Already satisfied by user_message relative change?
                if out.get("price_numeric_min") is not None:
                    continue
                errors.append(
                    f"Unclear price amount '{val}'. Use e.g. 5000000, 5M, 5 million, or 5,000,000."
                )
                continue
            if parsed is not None:
                if canon == "price_numeric_max" or key.lower() in ("price_to", "priceto"):
                    out["price_numeric_max"] = parsed
                else:
                    out["price_numeric_min"] = parsed
                    out.setdefault("price_display", f"{parsed:,.0f} THB")
            continue

        if canon in ("bedrooms_min", "bedrooms_max", "bedrooms_display") or key.lower() in (
            "bedrooms", "beds", "bedroom"
        ):
            beds = parse_bedrooms(val)
            out.update(beds)
            continue

        if canon == "province" and key.lower() == "location":
            loc = localize_name(val) or (str(val).strip() if val is not None else None)
            if loc:
                out["province"] = loc
                out["location"] = loc
            continue

        if canon == "property_name":
            name = localize_name(val)
            if name:
                out["property_name"] = name
            continue

        if canon in ("has_pool", "sea_view", "beach_access"):
            if isinstance(val, bool):
                out[canon] = val
            elif str(val).strip().lower() in ("1", "true", "yes", "tak", "y"):
                out[canon] = True
            elif str(val).strip().lower() in ("0", "false", "no", "nie", "n"):
                out[canon] = False
            else:
                out[canon] = bool(val)
            continue

        if val is None:
            continue
        if isinstance(val, dict) and canon == "property_name":
            out[canon] = localize_name(val)
        else:
            out[canon] = localize_name(val) if isinstance(val, dict) else val

    return out, errors


def normalize_compare_value(val: Any) -> str:
    """Normalize for equality: strip, case-fold, Decimal/int, commas, dict names."""
    if val is None or val == UNREADABLE or val == "—":
        return ""
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, dict):
        return (localize_name(val) or "").strip().lower()
    if isinstance(val, Decimal):
        try:
            f = float(val)
            if f == int(f):
                return str(int(f))
            return f"{f:g}"
        except Exception:
            return str(val).strip().lower()
    if isinstance(val, float):
        if val == int(val):
            return str(int(val))
        return f"{val:g}"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, (list, tuple)):
        return ",".join(normalize_compare_value(x) for x in val)
    s = str(val).strip().lower().replace(",", "").replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    # price-ish: drop currency words for compare
    s2 = re.sub(r"\b(thb|baht|million|mln)\b", "", s).strip()
    # If pure number after cleanup, use that
    try:
        f = float(s2.replace(" ", ""))
        if f == int(f):
            return str(int(f))
        return f"{f:g}"
    except Exception:
        pass
    return s


def values_equal(a: Any, b: Any) -> bool:
    if a == UNREADABLE or b == UNREADABLE:
        return True  # unreadable never causes stale
    na, nb = normalize_compare_value(a), normalize_compare_value(b)
    if na == nb:
        return True
    # Numeric tolerance for prices
    try:
        fa, fb = float(na), float(nb)
        return abs(fa - fb) < 0.01
    except Exception:
        return False


def read_pg_canonical(prop: Any, keys: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if prop is None:
        return out
    for k in keys:
        attrs = _PG_ATTRS.get(k, (k,))
        for attr in attrs:
            if hasattr(prop, attr):
                v = getattr(prop, attr, None)
                if v is not None and v != "":
                    if k == "property_name":
                        out[k] = localize_name(v)
                    elif k == "location" and k not in out:
                        # Compose later
                        pass
                    else:
                        out[k] = v
                    break
    # Compose location display from province/district/area if needed
    if "location" in keys and "location" not in out:
        parts = [out.get("province") or getattr(prop, "province", None),
                 out.get("district") or getattr(prop, "district", None),
                 out.get("area") or getattr(prop, "area", None)]
        loc = ", ".join(str(p) for p in parts if p)
        if loc:
            out["location"] = loc
    # Mirror price_display from numeric if missing
    if out.get("price_numeric_min") is not None and not out.get("price_display"):
        try:
            out["price_display"] = f"{float(out['price_numeric_min']):,.0f} THB"
        except Exception:
            pass
    return out


def firestore_identity(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Name, place, bedrooms and status from the document fields, not the id."""
    if not isinstance(doc, dict):
        return {}

    def text(*names: str) -> Optional[str]:
        for key in names:
            value = doc.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for item in value.values():
                    if isinstance(item, str) and item.strip() and len(item.strip()) <= 120:
                        return item.strip()
        return None

    name = text("name", "propertyName", "projectName", "title", "property_name")
    place = text("province", "city", "district", "area", "location")
    status = text("status", "saleStatus", "completionStatus")
    if not status and "isActive" in doc:
        status = "Available" if doc.get("isActive") else "Sold Out"
    bedrooms = doc.get("bedroomsMin")
    if bedrooms is None:
        bedrooms = doc.get("bedrooms_min")
    if bedrooms is None and isinstance(doc.get("bedrooms"), (int, float)):
        bedrooms = doc.get("bedrooms")
    bedrooms_max = doc.get("bedroomsMax") if doc.get("bedroomsMax") is not None else doc.get("bedrooms_max")
    out: Dict[str, Any] = {}
    if name:
        out["name"] = name
        out["property_name"] = name
    if place:
        out["province"] = place
        out["location"] = place
    if status:
        out["status"] = status
    if bedrooms is not None:
        out["bedrooms"] = bedrooms
        out["bedrooms_min"] = bedrooms
    if bedrooms_max is not None:
        out["bedrooms_max"] = bedrooms_max
    return out


def read_firestore_canonical(doc: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not doc:
        return out
    identity = firestore_identity(doc)
    name = localize_name(
        identity.get("name")
        or doc.get("name")
        or doc.get("propertyName")
        or doc.get("projectName")
        or doc.get("title")
        or doc.get("property_name")
    )
    price_from = doc.get("priceFrom") or doc.get("price_numeric_min") or doc.get("price")
    price_to = doc.get("priceTo") or doc.get("price_numeric_max")
    if isinstance(price_from, dict):
        price_from = price_from.get("min") or price_from.get("from") or localize_name(price_from)
    parsed_min = parse_price_amount(price_from)
    parsed_max = parse_price_amount(price_to)

    mapping = {
        "property_name": name,
        "status": identity.get("status") or doc.get("status") or doc.get("saleStatus"),
        "price_numeric_min": parsed_min if parsed_min is not None else price_from,
        "price_numeric_max": parsed_max if parsed_max is not None else price_to,
        "price_display": doc.get("price_display")
        or (f"{parsed_min:,.0f} THB" if parsed_min is not None else localize_name(doc.get("price"))),
        "currency": doc.get("currency") or "THB",
        "province": identity.get("province") or doc.get("province") or localize_name(doc.get("location")),
        "district": doc.get("district") or doc.get("area"),
        "area": doc.get("area") or localize_name(doc.get("location")),
        "location": identity.get("location") or localize_name(doc.get("location")) or doc.get("province"),
        "property_type": doc.get("property_type") or doc.get("type"),
        "bedrooms_min": identity.get("bedrooms_min") or doc.get("bedrooms_min") or doc.get("bedroomsMin") or doc.get("bedrooms"),
        "bedrooms_max": identity.get("bedrooms_max") or doc.get("bedrooms_max") or doc.get("bedroomsMax"),
        "bedrooms_display": doc.get("bedrooms_display") or (
            str(doc.get("bedrooms")) if doc.get("bedrooms") is not None else None
        ),
        "bathrooms": doc.get("bathrooms"),
        "area_display": doc.get("area_display") or doc.get("areaSize"),
        "has_pool": doc.get("has_pool") if doc.get("has_pool") is not None else doc.get("pool"),
        "sea_view": doc.get("sea_view"),
        "beach_access": doc.get("beach_access"),
        "developer_name": doc.get("developer_name") or doc.get("developerId"),
        "websites": doc.get("websites"),
    }
    for k in keys:
        if k in mapping and mapping[k] is not None and mapping[k] != "":
            out[k] = mapping[k]
        elif k in doc and doc.get(k) is not None:
            out[k] = doc.get(k)
    # Expand bedrooms if only raw bedrooms present
    if "bedrooms_min" in keys and "bedrooms_min" not in out and doc.get("bedrooms") is not None:
        out.update(parse_bedrooms(doc.get("bedrooms")))
    return out


def format_price_for_display(val: Any) -> str:
    """Always human thousands-separated ints — never scientific notation (3e+07)."""
    parsed = parse_price_amount(val)
    if parsed is not None:
        try:
            n = int(round(float(parsed)))
            return f"{n:,}"
        except Exception:
            return f"{parsed:,.0f}" if isinstance(parsed, (int, float)) else str(parsed)
    if val is None or val == UNREADABLE:
        return "—"
    s = localize_name(val) or str(val)
    # Guard against accidental scientific notation strings
    if re.match(r"^-?\d+(\.\d+)?e[+-]?\d+$", s.strip(), re.I):
        try:
            return f"{int(float(s)):,}"
        except Exception:
            pass
    return s if s else "—"


def format_price_thb(val: Any) -> str:
    base = format_price_for_display(val)
    if base == "—":
        return base
    if "THB" in base.upper():
        return base
    return f"{base} THB"


def coerce_price_storage(val: Any) -> Optional[Any]:
    """Store price_numeric_min as int (or Decimal) — never float that prints as 3e+07."""
    parsed = parse_price_amount(val)
    if parsed is None:
        return None
    try:
        return int(round(float(parsed)))
    except Exception:
        try:
            return Decimal(str(parsed))
        except (InvalidOperation, ValueError):
            return parsed


def resolve_existing_property(
    db: Optional[Any],
    property_id_or_name: Optional[str],
    *,
    exact_id_only: bool = False,
) -> Tuple[Optional[str], Optional[Any]]:
    """
    Resolve a listing from the Firestore projects collection.
    Returns (source, record) where source is 'firestore' | None.
    The record is a normalized dict with canonical keys plus '_firestore_doc_id' and '_raw'.
    If exact_id_only is True, matches ONLY by exact listing/document ID (no fuzzy or substring search).
    """
    raw = (property_id_or_name or "").strip()
    if not raw:
        return None, None

    aliases = {raw} if exact_id_only else {raw, raw.lower(), raw.upper()}
    # Firestore (admin client, then user)
    try:
        from app.services.firestore_service import (
            get_firestore_client,
            get_property_by_id_from_firestore,
        )
        from app.services.admin_batch_service import _resolve_firestore_document

        fs_prop = None
        for target in ("admin", "user"):
            for alias in aliases:
                try:
                    fs_prop = get_property_by_id_from_firestore(alias, target=target)
                except Exception:
                    fs_prop = None
                if fs_prop:
                    break
            if fs_prop:
                break

        doc_id = None
        doc: Dict[str, Any] = {}
        if fs_prop:
            doc_id = (
                fs_prop.get("property_id")
                or fs_prop.get("id")
                or fs_prop.get("doc_id")
                or raw
            )
            raw_fs = fs_prop.get("raw_firestore_data") if isinstance(fs_prop.get("raw_firestore_data"), dict) else {}
            doc = dict(raw_fs or {})
            doc.update({k: v for k, v in fs_prop.items() if k != "raw_firestore_data" and v is not None})
            doc.update(firestore_identity(raw_fs or {}))
        elif not exact_id_only:
            for target in ("admin", "user"):
                fs = get_firestore_client(target=target) or (
                    get_firestore_client() if target == "admin" else None
                )
                if not fs:
                    continue
                try:
                    doc_id, doc = _resolve_firestore_document(
                        fs.collection("projects"),
                        {"property_id": raw, "name": raw},
                        exact_id_only=exact_id_only,
                    )
                except Exception:
                    doc_id, doc = None, {}
                if doc_id and doc:
                    break

        if doc_id and doc:
            keys = list(CANONICAL_FIELDS) + [
                "bedrooms_min", "bedrooms_max", "bedrooms_display", "bathrooms",
                "area_display", "has_pool", "currency",
            ]
            canon = read_firestore_canonical(doc, keys)
            # Ensure property_name is a plain string
            if canon.get("property_name"):
                canon["property_name"] = localize_name(canon["property_name"]) or str(
                    canon["property_name"]
                )
            # Coerce price to int for storage/display stability
            if canon.get("price_numeric_min") is not None:
                coerced = coerce_price_storage(canon["price_numeric_min"])
                if coerced is not None:
                    canon["price_numeric_min"] = coerced
                    if not canon.get("price_display"):
                        canon["price_display"] = format_price_thb(coerced)
            record = {
                **canon,
                "property_id": str(doc_id),
                "_firestore_doc_id": str(doc_id),
                "_raw": doc,
                "_source": "firestore",
            }
            return "firestore", record
    except Exception as e:
        logger.debug(f"resolve_existing_property Firestore skip: {e}")

    return None, None
