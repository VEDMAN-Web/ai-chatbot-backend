"""
Canonical property field map for admin write/diff/knowledge paths.

Model aliases (price, location, bedrooms, …) are normalized to Postgres/Firestore
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

# Postgres attribute names used when reading ORM Property
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


def parse_price_amount(raw: Any) -> Optional[float]:
    """
    Parse '5 million', '5M', '5 mln', '5,000,000', 5000000 → float THB.
    Returns None if unclear.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            return float(raw)
        except Exception:
            return None
    s = str(raw).strip().lower().replace("\u00a0", " ")
    if not s or s in ("none", "null", "n/a", "-", "—"):
        return None
    # Relative change phrases are not absolute amounts
    if re.search(r"\b(increase|decrease|raise|lower|by)\b", s) and "%" in s:
        return None
    s = s.replace("thb", "").replace("baht", "").replace(",", "").strip()
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(million|mln|m\.?|m\b)?",
        s,
    )
    if not m:
        digits = re.sub(r"[^\d.]", "", s)
        if not digits:
            return None
        try:
            return float(digits)
        except ValueError:
            return None
    num = float(m.group(1))
    unit = (m.group(2) or "").rstrip(".")
    if unit in ("million", "mln", "m"):
        num *= 1_000_000
    return num


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


def read_firestore_canonical(doc: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not doc:
        return out
    name = localize_name(doc.get("name") or doc.get("title") or doc.get("property_name"))
    price_from = doc.get("priceFrom") or doc.get("price_numeric_min") or doc.get("price")
    price_to = doc.get("priceTo") or doc.get("price_numeric_max")
    if isinstance(price_from, dict):
        price_from = price_from.get("min") or price_from.get("from") or localize_name(price_from)
    parsed_min = parse_price_amount(price_from)
    parsed_max = parse_price_amount(price_to)

    mapping = {
        "property_name": name,
        "status": doc.get("status") or doc.get("saleStatus"),
        "price_numeric_min": parsed_min if parsed_min is not None else price_from,
        "price_numeric_max": parsed_max if parsed_max is not None else price_to,
        "price_display": doc.get("price_display")
        or (f"{parsed_min:,.0f} THB" if parsed_min is not None else localize_name(doc.get("price"))),
        "currency": doc.get("currency") or "THB",
        "province": doc.get("province") or localize_name(doc.get("location")),
        "district": doc.get("district") or doc.get("area"),
        "area": doc.get("area") or localize_name(doc.get("location")),
        "location": localize_name(doc.get("location")) or doc.get("province"),
        "property_type": doc.get("property_type") or doc.get("type"),
        "bedrooms_min": doc.get("bedrooms_min") or doc.get("bedrooms"),
        "bedrooms_max": doc.get("bedrooms_max"),
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
) -> Tuple[Optional[str], Optional[Any]]:
    """
    Resolve a listing from Postgres first, then Firestore projects.
    Returns (source, record) where source is 'postgres' | 'firestore' | None.
    Postgres record is the ORM Property; Firestore record is a normalized dict
    with canonical keys plus '_firestore_doc_id' and '_raw'.
    """
    raw = (property_id_or_name or "").strip()
    if not raw:
        return None, None

    aliases = {raw, raw.lower(), raw.upper()}
    # 1) Postgres (skipped when STORE_PROPERTIES_WRITE=firestore)
    from app.repositories import properties_store

    if db is not None and properties_store.sql_enabled():
        try:
            from app.db.models import Property

            prop = (
                db.query(Property)
                .filter(Property.property_id.in_(list(aliases)))
                .first()
            )
            if not prop:
                # Fuzzy name match (exact case-insensitive)
                prop = (
                    db.query(Property)
                    .filter(Property.property_name.ilike(raw))
                    .first()
                )
            if prop:
                return "postgres", prop
        except Exception as e:
            logger.debug(f"resolve_existing_property PG skip: {e}")

    # 2) Firestore (admin client, then user)
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
        else:
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
