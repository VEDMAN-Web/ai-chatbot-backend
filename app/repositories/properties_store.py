"""Property rows backed by Firestore (the only datastore).

Rows are simple objects (property_id, property_name, province, ...) read from the `projects`
collection, so callers that used to receive ORM rows keep working.
"""
from __future__ import annotations


def _firestore_rows(target: str = "admin"):
    """Active listings from Firestore as simple row objects (property_id, property_name, province, ...)."""
    from types import SimpleNamespace

    from app.repositories import _fs_utils as fs
    from app.services.admin_property_fields import coerce_price_storage, localize_name, read_firestore_canonical

    client = fs.user_fs_client() if target == "user" else fs.admin_fs_client()
    keys = [
        "property_name", "province", "property_type", "status", "price_numeric_min", "price_display", "websites",
        "bedrooms_min", "bedrooms_max", "bedrooms_display",
        "developer_name", "area_display", "area_sqm_min", "area_sqm_max", "bathrooms",
    ]
    rows = []
    for snap in fs.stream_collection(client, "projects"):
        doc = fs.doc_to_dict(snap)
        if doc.get("isActive") is False:
            continue
        canon = read_firestore_canonical(doc, keys)
        if str(canon.get("status") or "").lower() == "archived":
            continue
        name = localize_name(canon.get("property_name")) or localize_name(doc.get("name")) or localize_name(doc.get("title"))
        if not name:
            continue
        price = canon.get("price_numeric_min")
        if price is not None:
            price = coerce_price_storage(price)
        rows.append(SimpleNamespace(
            property_id=str(doc.get("id") or getattr(snap, "id", "") or ""),
            property_name=str(name),
            province=canon.get("province"),
            property_type=canon.get("property_type") or doc.get("propertyType"),
            status=canon.get("status"),
            price_numeric_min=price,
            price_display=canon.get("price_display"),
            websites=canon.get("websites") or [],
            bedrooms_min=canon.get("bedrooms_min"),
            bedrooms_max=canon.get("bedrooms_max"),
            bedrooms_display=canon.get("bedrooms_display"),
            developer_name=canon.get("developer_name") or doc.get("developerName") or doc.get("developer"),
            area_display=canon.get("area_display"),
            area_sqm_min=canon.get("area_sqm_min"),
            area_sqm_max=canon.get("area_sqm_max"),
            bathrooms=canon.get("bathrooms"),
            is_public=doc.get("isActive", True),
            updated_at=doc.get("updatedAt") or doc.get("updated_at"),
        ))
    return rows


def list_rows(db=None):
    """All active property rows from the admin Firestore project."""
    return _firestore_rows()


def public_rows(db=None):
    """Active property rows from the user (public) Firestore project."""
    return _firestore_rows("user")
