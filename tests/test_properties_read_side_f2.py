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


def test_brochure_postgres_lookup_is_skipped(fs, db):
    from app.services.brochure_data import _load_postgres_property

    assert _load_postgres_property(db, "stale-1") is None


def test_admin_scope_target_ignores_sql(fs, db):
    from app.api.v1.admin import _property_scope_target

    raw, prop = _property_scope_target("stale-1", db)
    assert prop is None
