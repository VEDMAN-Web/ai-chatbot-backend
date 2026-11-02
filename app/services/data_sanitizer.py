"""
Data sanitizer and null fallback utility.

Provides strict null-safety sanitization for records before database persistence.
Null values are safely replaced with typed default fallbacks:
- Strings / text -> ""
- Numbers (ints/floats) -> 0 or 0.0
- Booleans -> False
- Arrays / lists -> []
- Dictionaries / JSON objects -> {}
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Set, Union


# Known field types for property documents across the system
STRING_FIELDS: Set[str] = {
    "name",
    "title",
    "property_name",
    "project_name",
    "propertyType",
    "property_type",
    "status",
    "city",
    "cityId",
    "province",
    "district",
    "subdistrict",
    "area",
    "address",
    "location",
    "priceDisplay",
    "price_display",
    "currency",
    "description",
    "shortDescription",
    "coverImage",
    "cover_image",
    "developer_name",
    "developerName",
    "developer",
    "ownership_type",
    "siteOrigin",
    "site_origin",
    "slug",
    "detail_page_url",
    "bedrooms_display",
    "bedroomsDisplay",
    "area_display",
    "price_note",
}

NUMERIC_FIELDS: Set[str] = {
    "priceFrom",
    "priceTo",
    "price_numeric_min",
    "price_numeric_max",
    "bedrooms",
    "bedrooms_min",
    "bedrooms_max",
    "bedroomsMin",
    "bedroomsMax",
    "bathrooms",
    "areaFrom",
    "areaTo",
    "area_sqm_min",
    "area_sqm_max",
    "usable_area_sqm",
    "land_area_sqm",
    "latitude",
    "longitude",
    "expectedROI",
    "rentalYield",
}

BOOLEAN_FIELDS: Set[str] = {
    "isActive",
    "is_public",
    "has_pool",
    "hasPool",
    "beach_access",
    "sea_view",
}

LIST_FIELDS: Set[str] = {
    "images",
    "image_urls",
    "amenities",
    "tags",
    "websites",
    "domains",
    "allowed_websites",
    "highlights",
    "features",
    "facilities",
    "layouts",
}

DICT_FIELDS: Set[str] = {
    "raw_metadata",
    "metadata",
    "preferences",
}


def sanitize_null_value(val: Any, field_name: Optional[str] = None) -> Any:
    """
    Sanitizes a single value if it is None/null based on field name or value structure.
    If the value is not None, recursively cleans nested dicts/lists.
    """
    if val is None:
        if field_name:
            fn = field_name
            if fn in STRING_FIELDS:
                return ""
            if fn in NUMERIC_FIELDS:
                return 0
            if fn in BOOLEAN_FIELDS:
                return False
            if fn in LIST_FIELDS:
                return []
            if fn in DICT_FIELDS:
                return {}
            # Suffix heuristics
            fn_lower = fn.lower()
            if any(s in fn_lower for s in ("price", "amount", "count", "num", "min", "max", "rate", "area")):
                return 0
            if any(s in fn_lower for s in ("is_", "has_")) or fn_lower.startswith(("is", "has")):
                return False
            if any(s in fn_lower for s in ("list", "images", "websites", "urls", "tags")):
                return []
            if any(s in fn_lower for s in ("dict", "map", "meta", "data")):
                return {}
            # Default fallback for unclassified null fields
            return ""
        return ""

    if isinstance(val, dict):
        return {k: sanitize_null_value(v, k) for k, v in val.items()}

    if isinstance(val, list):
        return [sanitize_null_value(item) for item in val]

    return val


def sanitize_db_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Performs full null-safety sanitization on a dictionary before it is written to the database.
    Replaces all None/null values with typed zero/empty defaults.
    """
    if not isinstance(record, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in record.items():
        cleaned[k] = sanitize_null_value(v, field_name=k)
    return cleaned

