"""Property reads come from Firestore: rows, name lookups, bulk price changes and the bedroom overlay."""
from __future__ import annotations

import pytest

from app.repositories import properties_store
from tests.fake_firestore import FakeFirestore, patch_firestore


@pytest.fixture
def fs(monkeypatch):
    fake = patch_firestore(monkeypatch, FakeFirestore())
    fake.collection("projects").document("villa-1").set({
        "name": "Sea Villa", "province": "Phuket", "propertyType": "Villa",
        "priceFrom": 5000000, "isActive": True, "status": "Available", "websites": ["tajlandia.com"],
        "bedrooms_min": 3, "bedrooms_max": 4, "bedrooms_display": "3-4",
    })
    fake.collection("projects").document("condo-1").set({
        "name": "City Condo", "province": "Bangkok", "propertyType": "Condo",
        "priceFrom": 3000000, "isActive": True, "status": "Available",
    })
    fake.collection("projects").document("old-1").set({
        "name": "Old Villa", "province": "Phuket", "propertyType": "Villa",
        "priceFrom": 1000000, "isActive": False,
    })
    return fake


def test_list_rows_reads_active_firestore_listings(fs, db):
    rows = properties_store.list_rows(db)
    assert sorted(r.property_id for r in rows) == ["condo-1", "villa-1"]  # inactive skipped
    villa = next(r for r in rows if r.property_id == "villa-1")
    assert villa.property_name == "Sea Villa"
    assert villa.province == "Phuket"
    assert float(villa.price_numeric_min) == 5000000
    assert (villa.bedrooms_min, villa.bedrooms_max, villa.bedrooms_display) == (3, 4, "3-4")


def test_public_rows_read_the_user_project(fs, db):
    assert sorted(r.property_name for r in properties_store.public_rows(db)) == ["City Condo", "Sea Villa"]


def test_chat_quality_name_lookup_uses_firestore(fs, db):
    from app.services.admin_chat_quality import _closest_property_names, _name_matches

    assert [r.property_id for r in _name_matches(db, "sea villa")] == ["villa-1"]
    assert [r.property_id for r in _name_matches(db, "Condo")] == ["condo-1"]
    assert "Sea Villa" in _closest_property_names(db, "Sea Vila")


def test_overlay_missing_bedrooms_uses_firestore(fs, db):
    from app.services.admin_list_display import overlay_missing_bedrooms

    out = overlay_missing_bedrooms(db, [{"property_id": "villa-1", "name": "Sea Villa"}])
    assert out[0]["bedrooms_min"] == 3 and out[0]["bedrooms_max"] == 4
    assert out[0]["bedrooms_display"] == "3-4"


def test_bulk_price_change_finds_firestore_listings(fs, db):
    from types import SimpleNamespace

    from app.core.auth import AuthenticatedStaff
    from app.services.admin_turn_rules import try_rule_based_write

    staff = AuthenticatedStaff(
        user_id="u1", email="boss@tajlandia.com", role="super_admin",
        allowed_websites=["tajlandia.com"], selected_websites=["tajlandia.com"],
    )
    conv = SimpleNamespace(id="conv-1")
    out = try_rule_based_write(db, conv, "increase all Phuket villas by 10%", staff, "en")
    assert out is not None
    text = out["text"] if isinstance(out, dict) and "text" in out else str(out)
    assert "Sea Villa" in text
    assert "Old Villa" not in text
