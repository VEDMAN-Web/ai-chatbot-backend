"""Remaining SQL reads: public rows follow the public store mode; bedroom overlay follows the property store mode."""
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
    fake.collection("projects").document("villa-1").set({
        "name": "Sea Villa", "province": "Phuket", "type": "Villa", "priceFrom": 5000000,
        "isActive": True, "bedrooms_min": 3, "bedrooms_max": 4, "bedrooms_display": "3-4",
    })
    return fake


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Property.__table__.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    session.add(Property(property_id="villa-1", property_name="Stale SQL Name", bedrooms_min=1, bedrooms_max=1,
                         bedrooms_display="1", websites=["tajlandia.com"], is_public=True))
    session.commit()
    yield session
    session.close()


def test_rows_carry_bedrooms(fs, monkeypatch):
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "firestore")
    row = properties_store.list_rows(None)[0]
    assert (row.bedrooms_min, row.bedrooms_max, row.bedrooms_display) == (3, 4, "3-4")


def test_public_rows_firestore_when_sql_not_allowed(fs, db, monkeypatch):
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "firestore")
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "firestore", raising=False)
    rows = properties_store.public_rows(db)
    assert [r.property_name for r in rows] == ["Sea Villa"]


def test_public_rows_sql_when_mode_is_sql(fs, db, monkeypatch):
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "both")
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "sql", raising=False)
    try:
        import app.services.property_index  # noqa: F401
    except Exception:
        pytest.skip("branch has no property_index; mode falls back to the write mode")
    assert [r.property_name for r in properties_store.public_rows(db)] == ["Stale SQL Name"]


def test_overlay_missing_bedrooms_uses_firestore(fs, db, monkeypatch):
    conv = pytest.importorskip("app.services.conversation_controls")
    if not hasattr(conv, "overlay_missing_bedrooms"):
        pytest.skip("branch has no bedroom overlay")
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "firestore")
    cards = [{"property_id": "villa-1", "name": "Sea Villa"}]
    out = conv.overlay_missing_bedrooms(db, cards)
    assert out[0]["bedrooms_min"] == 3 and out[0]["bedrooms_max"] == 4
    assert out[0]["bedrooms_display"] == "3-4"


def test_overlay_missing_bedrooms_still_sql_in_default_mode(fs, db, monkeypatch):
    conv = pytest.importorskip("app.services.conversation_controls")
    if not hasattr(conv, "overlay_missing_bedrooms"):
        pytest.skip("branch has no bedroom overlay")
    monkeypatch.setattr(settings, "STORE_PROPERTIES_WRITE", "both")
    out = conv.overlay_missing_bedrooms(db, [{"property_id": "villa-1"}])
    assert out[0]["bedrooms_min"] == 1
