import json
import os
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.db.models import Property, PropertySpec

SPECS_JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "internal_knowledge_specs.json")


def get_property_knowledge(db: Session, property_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves complete private/internal information for a property,
    merging PostgreSQL 'property_specs' with internal_knowledge_specs.json.
    Strictly restricted to internal staff / admin agents.
    """
    clean_id = property_id.strip().upper()
    # Normalize ID variations (e.g. PROP-PHK-001 vs PROP-PHU-001)
    id_aliases = {clean_id}
    if "PHK" in clean_id:
        id_aliases.add(clean_id.replace("PHK", "PHU"))
    if "PHU" in clean_id:
        id_aliases.add(clean_id.replace("PHU", "PHK"))

    # 1. Query Property and PropertySpec from DB
    prop = db.query(Property).filter(Property.property_id.in_(list(id_aliases))).first()
    spec = None
    if prop:
        spec = db.query(PropertySpec).filter(PropertySpec.property_id == prop.property_id).first()

    # 2. Query internal JSON specs
    json_spec = {}
    if os.path.exists(SPECS_JSON_PATH):
        try:
            with open(SPECS_JSON_PATH, "r", encoding="utf-8") as f:
                all_specs = json.load(f)
                for item in all_specs:
                    item_id = item.get("property_id", "").strip().upper()
                    if item_id in id_aliases:
                        json_spec = item
                        break
        except Exception:
            pass

    if not prop and not json_spec:
        return None

    # Merge specs
    merged_specs = json_spec.get("specifications", {})
    construction = merged_specs.get("construction_details", {})
    
    # DB specs take precedence if populated
    roof_material = (spec.roof_material if spec and spec.roof_material else merged_specs.get("roof_material")) or "N/A"
    roof_colour = (spec.roof_colour if spec and spec.roof_colour else merged_specs.get("roof_colour")) or "N/A"
    flooring = (spec.flooring if spec and spec.flooring else merged_specs.get("flooring")) or "N/A"
    walls = (spec.walls if spec and spec.walls else merged_specs.get("walls")) or "N/A"
    doors_windows = (spec.doors_windows if spec and spec.doors_windows else merged_specs.get("doors_and_windows")) or "N/A"
    exterior_mat = (spec.exterior_materials if spec and spec.exterior_materials else merged_specs.get("exterior_materials")) or "N/A"
    interior_mat = (spec.interior_materials if spec and spec.interior_materials else merged_specs.get("interior_materials")) or "N/A"
    arch_style = (spec.architectural_style if spec and spec.architectural_style else merged_specs.get("architectural_style")) or "N/A"
    internal_notes = (spec.internal_notes if spec and spec.internal_notes else json_spec.get("internal_notes")) or "No private notes available."

    return {
        "property_id": prop.property_id if prop else json_spec.get("property_id"),
        "property_name": prop.property_name if prop else json_spec.get("property_name"),
        "status": prop.status if prop else "Available",
        "location": f"{prop.district or ''}, {prop.province or ''}".strip(", ") if prop else "Thailand",
        "specifications": {
            "roof_material": roof_material,
            "roof_colour": roof_colour,
            "flooring": flooring,
            "walls": walls,
            "doors_and_windows": doors_windows,
            "exterior_materials": exterior_mat,
            "interior_materials": interior_mat,
            "architectural_style": arch_style,
            "construction_details": construction or (spec.construction_details if spec else {}),
            "developer_contact": merged_specs.get("developer_contact", "Tajlandia Agency Desk")
        },
        "internal_notes": internal_notes,
        "is_private": True
    }
