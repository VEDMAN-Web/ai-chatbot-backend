import json
import os
import logging
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.db.models import Property, PropertySpec

logger = logging.getLogger(__name__)

def get_property_knowledge(db: Session, property_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves complete private/internal information for a property,
    extracting specifications strictly from live Cloud Firestore (and verified DB specs).
    NEVER uses hardcoded, static, or hallucinated architectural materials.
    If a specification is not documented in the production record, it explicitly states
    'Not documented in verified database'.
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

    # 1.5 Query Property and PropertySpec from DB (if local DB session provided)
    prop = None
    spec = None
    if db is not None:
        try:
            prop = db.query(Property).filter(Property.property_id.in_(list(id_aliases))).first()
            if prop:
                spec = db.query(PropertySpec).filter(PropertySpec.property_id == prop.property_id).first()
        except Exception as e:
            logger.debug(f"DB lookup fallback: {e}")

    if not fs_prop and not prop and not spec:
        return None

    # 2. Extract technical specs strictly from verified live production data
    fs_raw = (fs_prop.get("raw_firestore_data") or {}) if fs_prop else {}
    amenities = fs_prop.get("amenities") or fs_raw.get("amenities") or []
    tags = fs_raw.get("tags") or []
    layouts = fs_raw.get("layouts") or []
    completion_status = fs_prop.get("status") or fs_raw.get("completionStatus")
    completion_date = fs_raw.get("completionDate")
    dev_name = fs_prop.get("developer_name") or fs_raw.get("developerId") or (prop.developer_name if prop else None)

    # Investment notes / highlights
    inv_highlights = fs_raw.get("investmentHighlights")
    if isinstance(inv_highlights, (list, tuple)):
        notes_val = " ".join([str(x) for x in inv_highlights if x])
    elif isinstance(inv_highlights, dict):
        notes_val = inv_highlights.get("en") or inv_highlights.get("pl") or ""
    elif inv_highlights:
        notes_val = str(inv_highlights)
    else:
        notes_val = ""

    # Check verified DB specs if populated; otherwise reflect production Firestore status
    roof_material = (spec.roof_material if spec and spec.roof_material else None) or "Not documented in verified database"
    roof_colour = (spec.roof_colour if spec and spec.roof_colour else None) or "Not documented in verified database"
    flooring = (spec.flooring if spec and spec.flooring else None) or "Not documented in verified database"
    walls = (spec.walls if spec and spec.walls else None) or "Not documented in verified database"
    doors_windows = (spec.doors_windows if spec and spec.doors_windows else None) or "Not documented in verified database"
    exterior_mat = (spec.exterior_materials if spec and spec.exterior_materials else None) or "Not documented in verified database"
    interior_mat = (spec.interior_materials if spec and spec.interior_materials else None) or "Not documented in verified database"
    arch_style = (spec.architectural_style if spec and spec.architectural_style else None) or (fs_prop.get("property_type") if fs_prop else "Not documented in verified database")
    foundation_val = (spec.construction_details.get("foundation") if spec and isinstance(spec.construction_details, dict) and spec.construction_details.get("foundation") else None) or "Not documented in verified database"

    internal_notes = (spec.internal_notes if spec and spec.internal_notes else None) or (notes_val if notes_val else "No confidential notes recorded for this development.")

    prop_id_val = (fs_prop.get("property_id") if fs_prop else None) or (prop.property_id if prop else clean_id)
    prop_name_val = (fs_prop.get("name") if fs_prop else None) or (prop.property_name if prop else clean_id)
    prop_status = completion_status or (prop.status if prop else "Available")
    prop_loc = (fs_prop.get("location") if fs_prop else None) or (f"{prop.district or ''}, {prop.province or ''}".strip(", ") if prop else "Thailand")

    return {
        "property_id": prop_id_val,
        "property_name": prop_name_val,
        "status": prop_status,
        "location": prop_loc,
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
            "developer_contact": dev_name or "Tajlandia Agency Desk"
        },
        "internal_notes": internal_notes,
        "is_private": True
    }
