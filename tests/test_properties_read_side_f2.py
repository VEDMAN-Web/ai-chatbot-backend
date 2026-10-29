"""Stage F2 (read side): with STORE_PROPERTIES_WRITE=firestore, lookups use Firestore and ignore stale SQL rows."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.models import Property
from app.repositories import properties_store
from tests.fake_firestore import FakeFirestore, patch_firestore


@pytest.fixture
def fs(monkeypatch):
    fake = patch_firestore(monkeypatch, FakeFirestore())
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "firestore")
    fake.collection("projects").document("villa-1").set({
        "name": "Sea Villa", "province": "Phuket", "propertyType": "Villa",
        "priceFrom": 5000000, "isActive": True, "status": "Available", "websites": ["tajlandia.com"],
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


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Property.__table__.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    session.add(Property(property_id="stale-1", property_name="Stale SQL Villa", province="Phuket",
                         property_type="Villa", price_numeric_min=111, websites=["tajlandia.com"], is_public=True))
    session.commit()
    yield session
    session.close()


def test_list_rows_reads_active_firestore_listings_not_sql(fs, db):
    rows = properties_store.list_rows(db)
    ids = sorted(r.property_id for r in rows)
    assert ids == ["condo-1", "villa-1"]  # inactive skipped, stale SQL row ignored
    villa = next(r for r in rows if r.property_id == "villa-1")
    assert villa.property_name == "Sea Villa"
    assert villa.province == "Phuket"
    assert float(villa.price_numeric_min) == 5000000


def test_list_rows_uses_sql_in_default_mode(fs, db, monkeypatch):
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "both")
    assert [r.property_id for r in properties_store.list_rows(db)] == ["stale-1"]


def test_sql_db_helper(db, monkeypatch):
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "firestore")
    assert properties_store.sql_db(db) is None
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "both")
    assert properties_store.sql_db(db) is db


def test_chat_quality_name_lookup_uses_firestore(fs, db):
    from app.services.admin_chat_quality import _closest_property_names, _name_matches

    assert [r.property_id for r in _name_matches(db, "sea villa")] == ["villa-1"]
    assert [r.property_id for r in _name_matches(db, "Condo")] == ["condo-1"]
    assert _name_matches(db, "Stale SQL Villa") == []
    assert "Sea Villa" in _closest_property_names(db, "Sea Vila")


def test_brochure_postgres_lookup_is_skipped(fs, db):
    from app.services.brochure_data import _load_postgres_property

    assert _load_postgres_property(db, "stale-1") is None


def test_admin_scope_target_ignores_sql(fs, db):
    from app.api.v1.admin import _property_scope_target

    raw, prop = _property_scope_target("stale-1", db)
    assert prop is None


def test_bulk_price_change_finds_firestore_listings(fs, db, monkeypatch):
    from types import SimpleNamespace

    from app.core.auth import AuthenticatedStaff
    from app.services.admin_turn_rules import try_rule_based_write

    monkeypatch.setattr(settings, "STORE_PENDING_ACTIONS", "firestore")
    monkeypatch.setattr(settings, "STORE_AUDIT", "firestore")
    staff = AuthenticatedStaff(
        user_id="u1", email="boss@tajlandia.com", role="super_admin",
        allowed_websites=["tajlandia.com"], selected_websites=["tajlandia.com"],
    )
    conv = SimpleNamespace(id="conv-1")
    out = try_rule_based_write(db, conv, "increase all Phuket villas by 10%", staff, "en")
    assert out is not None
    text = out["text"] if isinstance(out, dict) and "text" in out else str(out)
    assert "Sea Villa" in text
    assert "Stale SQL Villa" not in text
    assert "Old Villa" not in text
