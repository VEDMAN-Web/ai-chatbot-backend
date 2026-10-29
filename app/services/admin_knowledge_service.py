import json
import os
import logging
from typing import Dict, Any, Optional
from app.services.admin_property_fields import (
    format_price_for_display,
    localize_name,
    parse_bedrooms,
    parse_price_amount,
    read_firestore_canonical,
)

logger = logging.getLogger(__name__)


def get_property_knowledge(
    db: Any,
    property_id: str,
    *,
    include_internal_notes: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Retrieves complete private/internal information for a property,
    extracting specifications strictly from live Cloud Firestore (and verified DB specs).
    NEVER uses hardcoded, static, or hallucinated architectural materials.
    If a specification is not documented in the production record, it explicitly states
    'Not documented in verified database'.

    Always includes price_* and bedrooms/area display fields for admin.
    internal_notes / developer_contact are omitted unless include_internal_notes=True.
    """
    if not property_id or not isinstance(property_id, str):
        return None

    clean_id = property_id.strip()
    id_aliases = {clean_id, clean_id.upper(), clean_id.lower()}
    if "PHK" in clean_id.upper():
        id_aliases.add(clean_id.upper().replace("PHK", "PHU"))
    if "PHU" in clean_id.upper():
        id_aliases.add(clean_id.upper().replace("PHU", "PHK"))

    # 1. Query property from live Cloud Firestore (primary authoritative store)
    fs_prop = None
    try:
        from app.services.firestore_service import get_property_by_id_from_firestore
        for alias in id_aliases:
            fs_prop = get_property_by_id_from_firestore(alias, target="admin")
            if fs_prop:
                break
    except Exception as e:
        logger.warning(f"Firestore get_property_by_id_from_firestore error: {e}")
        fs_prop = None

    if not fs_prop:
        return None

    price_keys = [
        "property_name", "status", "province", "district", "area", "location",
        "price_numeric_min", "price_numeric_max", "price_display", "currency",
        "bedrooms_min", "bedrooms_max", "bedrooms_display", "bathrooms",
        "area_display", "property_type", "has_pool",
    ]
    merged: Dict[str, Any] = {}
    if fs_prop:
        raw = fs_prop.get("raw_firestore_data") if isinstance(fs_prop.get("raw_firestore_data"), dict) else {}
        # Prefer flat card + raw
        doc = dict(raw or {})
        doc.update({k: v for k, v in fs_prop.items() if k != "raw_firestore_data" and v is not None})
        fs_vals = read_firestore_canonical(doc, price_keys)
        for k, v in fs_vals.items():
            if k not in merged or merged.get(k) is None or merged.get(k) == "":
                merged[k] = v

    # Specs
    fs_raw = (fs_prop.get("raw_firestore_data") or {}) if fs_prop else {}
    amenities = fs_prop.get("amenities") or fs_raw.get("amenities") or [] if fs_prop else []
    tags = fs_raw.get("tags") or []
    layouts = fs_raw.get("layouts") or []
    completion_status = merged.get("status") or (fs_prop.get("status") if fs_prop else None) or fs_raw.get("completionStatus")
    completion_date = fs_raw.get("completionDate")
    dev_name = (
        (fs_prop.get("developer_name") if fs_prop else None)
        or fs_raw.get("developerId")
    )

    inv_highlights = fs_raw.get("investmentHighlights")
    if isinstance(inv_highlights, (list, tuple)):
        notes_val = " ".join([str(x) for x in inv_highlights if x])
    elif isinstance(inv_highlights, dict):
        notes_val = inv_highlights.get("en") or inv_highlights.get("pl") or ""
    elif inv_highlights:
        notes_val = str(inv_highlights)
    else:
        notes_val = ""

    roof_material = "Not documented in verified database"
    roof_colour = "Not documented in verified database"
    flooring = "Not documented in verified database"
    walls = "Not documented in verified database"
    doors_windows = "Not documented in verified database"
    exterior_mat = "Not documented in verified database"
    interior_mat = "Not documented in verified database"
    arch_style = merged.get("property_type") or "Not documented in verified database"
    foundation_val = "Not documented in verified database"
    internal_notes = notes_val if notes_val else "No confidential notes recorded for this development."

    prop_id_val = (fs_prop.get("property_id") if fs_prop else None) or clean_id
    prop_name_val = localize_name(merged.get("property_name")) or (
        localize_name(fs_prop.get("name")) if fs_prop else None
    ) or clean_id
    prop_status = completion_status or "Available"
    prop_loc = merged.get("location") or (
        f"{merged.get('district') or ''}, {merged.get('province') or ''}".strip(", ")
    ) or "Thailand"

    price_min = merged.get("price_numeric_min")
    price_max = merged.get("price_numeric_max")
    price_display = merged.get("price_display")
    if price_display is None and price_min is not None:
        price_display = f"{format_price_for_display(price_min)} THB"
    currency = merged.get("currency") or "THB"

    # Normalize parsed numerics
    if price_min is not None:
        try:
            price_min = float(price_min)
        except Exception:
            price_min = parse_price_amount(price_min)
    if price_max is not None:
        try:
            price_max = float(price_max)
        except Exception:
            price_max = parse_price_amount(price_max)

    beds = {}
    if merged.get("bedrooms_min") is not None or merged.get("bedrooms_display"):
        beds = {
            "bedrooms_min": merged.get("bedrooms_min"),
            "bedrooms_max": merged.get("bedrooms_max"),
            "bedrooms_display": merged.get("bedrooms_display"),
        }
    else:
        beds = parse_bedrooms(merged.get("bedrooms_min"))

    price_present = price_min is not None or price_max is not None or (
        price_display is not None and str(price_display).strip()
    )
    price_empty = price_present and (
        (price_min is None or price_min == 0)
        and (price_max is None)
        and (not price_display or str(price_display).strip().lower() in ("", "n/a", "none", "—", "-"))
    )

    has_pool_flag = merged.get("has_pool")
    _notes_l = str(internal_notes or "").lower()
    notes_mention_pool = ("pool" in _notes_l) or ("basen" in _notes_l)
    pool_conflict_note = None
    if has_pool_flag is False and notes_mention_pool:
        pool_conflict_note = (
            "The record has no pool flag; the internal notes mention a private pool"
        )

    result: Dict[str, Any] = {
        "property_id": prop_id_val,
        "property_name": prop_name_val,
        "name": prop_name_val,
        "status": prop_status,
        "location": prop_loc,
        "price_numeric_min": price_min,
        "price_numeric_max": price_max,
        "price_display": price_display,
        "currency": currency,
        "bedrooms_display": beds.get("bedrooms_display") or merged.get("bedrooms_display"),
        "bedrooms_min": beds.get("bedrooms_min") if beds.get("bedrooms_min") is not None else merged.get("bedrooms_min"),
        "bedrooms_max": beds.get("bedrooms_max") if beds.get("bedrooms_max") is not None else merged.get("bedrooms_max"),
        "area_display": merged.get("area_display"),
        "bathrooms": merged.get("bathrooms"),
        "property_type": merged.get("property_type"),
        "has_pool": has_pool_flag,
        "pool_record_vs_notes": pool_conflict_note,
        "fact_source": "record_fields",
        "price_fields_present": bool(price_present),
        "price_fields_empty": bool(price_empty),
        "specifications": {
            "roof_material": roof_material,
            "roof_colour": roof_colour,
            "flooring": flooring,
            "walls": walls,
            "doors_and_windows": doors_windows,
            "exterior_materials": exterior_mat,
            "interior_materials": interior_mat,
            "architectural_style": arch_style,
            "construction_details": {
                "foundation": foundation_val,
                "completion_status": completion_status or "Not documented",
                "completion_date": completion_date or "Not documented",
                "layouts_count": len(layouts) if isinstance(layouts, list) else 0,
                "amenities": amenities,
                "tags": tags
            },
        },
        "is_private": True
    }

    if include_internal_notes:
        result["internal_notes"] = internal_notes
        result["specifications"]["developer_contact"] = dev_name or "Tajlandia Agency Desk"

    return result
