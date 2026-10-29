"""STORE_PROPERTIES_WRITE: where property listings are written (Stage F2).

  both      (default) write Postgres and Firestore, as before
  postgres  legacy SQL-first behaviour (same code path as 'both')
  firestore Firestore only. No SQL reads, writes, snapshots or restores for properties.

In firestore mode the SQL session is treated as absent for property work, so a stale SQL row can
never be read back, snapshotted or "restored" over the Firestore document.
"""
from __future__ import annotations

from app.repositories.store_mode import store_mode


def mode() -> str:
    return store_mode("STORE_PROPERTIES_WRITE", default="both")


def sql_enabled() -> bool:
    return mode() != "firestore"


def sql_db(db):
    """The SQL session when SQL is in use for properties, else None (callers already handle None)."""
    return db if sql_enabled() else None


def _firestore_rows():
    """Active listings from Firestore as simple row objects (property_id, property_name, province, ...)."""
    from types import SimpleNamespace

    from app.repositories import _fs_utils as fs
    from app.services.admin_property_fields import coerce_price_storage, localize_name, read_firestore_canonical

    client = fs.admin_fs_client()
    keys = ["property_name", "province", "property_type", "status", "price_numeric_min", "price_display", "websites"]
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
        ))
    return rows


def list_rows(db):
    """All property rows: ORM rows when SQL is in use, otherwise Firestore-backed rows."""
    if sql_enabled():
        from app.db.models import Property

        return db.query(Property).all()
    return _firestore_rows()
