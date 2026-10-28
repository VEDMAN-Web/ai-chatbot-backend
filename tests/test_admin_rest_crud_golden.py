"""
Admin REST CRUD golden tests.

Fake in-memory Firestore plus in-memory SQLite. No live Postgres or Firestore.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.admin import router as admin_router
from app.core.auth import AuthenticatedStaff, get_current_user_from_db
from app.core.config import settings
from app.db.models import AuditLog, Property
from app.db.session import get_db
from app.services.action_service import validate_listing_values, validate_property_data
from app.services.admin_batch_service import (
    OLD_SNAPSHOT_MESSAGE,
    BatchOperationError,
    execute_batch_property_operation,
)
from app.services.cache_service import search_cache
from app.services.firestore_service import get_firestore_client


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._data = None if data is None else copy.deepcopy(data)
        self.reference = self

    def to_dict(self):
        if self._data is None:
            return None
        return copy.deepcopy(self._data)

    def delete(self):
        return None


class _Doc:
    def __init__(self, store, collection, doc_id):
        self.store = store
        self.collection_name = collection
        self.id = doc_id

    def get(self):
        return _Snap(self.id, self.store.get(self.collection_name, self.id))

    def set(self, data, merge=False):
        self.store.set(self.collection_name, self.id, data, merge=merge)

    def update(self, data):
        self.store.update(self.collection_name, self.id, data)

    def delete(self):
        self.store.delete(self.collection_name, self.id)


class _Query:
    def __init__(self, collection):
        self.collection = collection

    def limit(self, _n):
        return self

    def stream(self):
        return self.collection.stream()

    def where(self, *args, **kwargs):
        return self


class _Collection:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def document(self, doc_id=None):
        if not doc_id:
            doc_id = uuid.uuid4().hex
        return _Doc(self.store, self.name, str(doc_id))

    def stream(self):
        for doc_id, data in self.store.items(self.name):
            yield _Snap(doc_id, data)

    def limit(self, _n):
        return _Query(self)

    def where(self, *args, **kwargs):
        return _Query(self)


class FakeFirestore:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Collection(self, name)

    def get(self, collection, doc_id):
        return self.docs.get((collection, doc_id))

    def items(self, collection):
        return [(doc_id, data) for (col, doc_id), data in self.docs.items() if col == collection]

    def set(self, collection, doc_id, data, merge=False):
        key = (collection, doc_id)
        payload = copy.deepcopy(data)
        current = self.docs.get(key)
        if merge and isinstance(current, dict):
            merged = copy.deepcopy(current)
            merged.update(payload)
            self.docs[key] = merged
        else:
            self.docs[key] = payload

    def update(self, collection, doc_id, data):
        key = (collection, doc_id)
        current = self.docs.get(key)
        if not isinstance(current, dict):
            raise RuntimeError(f"missing document {doc_id}")
        merged = copy.deepcopy(current)
        merged.update(copy.deepcopy(data))
        self.docs[key] = merged

    def delete(self, collection, doc_id):
        self.docs.pop((collection, doc_id), None)


def _sqlite_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (Property.__table__, AuditLog.__table__):
        for idx in list(table.indexes):
            using = getattr(idx, "dialect_kwargs", {}).get("postgresql_using")
            if using == "gin":
                table.indexes.discard(idx)
        table.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return Session()


def _staff(role="super_admin", allowed=None):
    sites = allowed or ["tajlandia.com", "varsoviaestate.com", "thai.estate"]
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role=role,
        allowed_websites=list(sites),
        selected_websites=list(sites),
    )


@pytest.fixture
def world(monkeypatch):
    session = _sqlite_session()
    admin_fs = FakeFirestore()
    user_fs = FakeFirestore()
    monkeypatch.setattr(
        "app.services.firestore_service.get_admin_firestore_client",
        lambda: admin_fs,
    )
    monkeypatch.setattr(
        "app.services.firestore_service.get_user_firestore_client",
        lambda: user_fs,
    )
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")

    def _db():
        yield session

    holder = {"staff": _staff()}

    def _user():
        return holder["staff"]

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_from_db] = _user
    client = TestClient(app)
    yield {
        "client": client,
        "db": session,
        "admin_fs": admin_fs,
        "user_fs": user_fs,
        "holder": holder,
    }
    app.dependency_overrides.clear()
    session.close()


def _minimal_body(**extra):
    body = {
        "property_name": "Minimal Villa",
        "property_type": "Villa",
        "province": "Phuket",
        "price_numeric_min": 2500000,
        "status": "Available",
    }
    body.update(extra)
    return body


def _fs_doc(admin_fs, property_id):
    return admin_fs.get("projects", property_id)


def _sql_row(db, property_id):
    return db.query(Property).filter(Property.property_id == property_id).first()


def test_minimal_and_full_create_land_in_both_stores(world):
    created = world["client"].post("/api/v1/admin/properties", json=_minimal_body())
    assert created.status_code == 201, created.text
    payload = created.json()
    pid = payload["property_id"]
    assert payload["sql"] is True
    assert payload["firestore"] is True
    assert payload["stores"] == {"sql": True, "firestore": True}
    assert "Postgres" in payload["message"] and "Firestore" in payload["message"]
    assert payload["operation_id"]
    assert payload["audit"] != "disabled"

    row = _sql_row(world["db"], pid)
    doc = _fs_doc(world["admin_fs"], pid)
    assert row is not None and row.property_name == "Minimal Villa"
    assert float(row.price_numeric_min) == 2500000
    assert doc["name"] == "Minimal Villa"
    assert doc["priceFrom"] == 2500000
    assert doc["isActive"] is True
    assert doc["propertyType"] == "Villa"

    full_id = "full-laguna-villa"
    full = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(
            property_id=full_id,
            property_name="Laguna Full Villa",
            project_name="Laguna",
            district="Thalang",
            address="12 Lagoon Road",
            price_display="from 4.50M THB",
            price_numeric_max=6000000,
            bedrooms=3,
            bathrooms=4,
            area_sqm_min=180,
            area_sqm_max=240,
            description="Pool villa",
        ),
    )
    assert full.status_code == 201, full.text
    full_row = _sql_row(world["db"], full_id)
    full_doc = _fs_doc(world["admin_fs"], full_id)
    assert full_row.project_name == "Laguna"
    assert full_row.district == "Thalang"
    assert full_row.bedrooms_min == 3
    assert float(full_row.price_numeric_max) == 6000000
    assert full_row.description == "Pool villa"
    assert full_doc["name"] == "Laguna Full Villa"
    assert full_doc["priceTo"] == 6000000


def test_failed_validation_writes_nothing(world):
    before_sql = world["db"].query(Property).count()
    before_fs = len(world["admin_fs"].items("projects"))
    res = world["client"].post(
        "/api/v1/admin/properties",
        json={"property_name": "No Price", "property_type": "Villa", "province": "Phuket", "status": "Available"},
    )
    assert res.status_code == 422
    assert "price" in res.json()["detail"]["fields"]
    assert world["db"].query(Property).count() == before_sql
    assert len(world["admin_fs"].items("projects")) == before_fs
    assert world["admin_fs"].items("admin_batch_operations") == []


def test_duplicate_create_is_409_and_leaves_original(world):
    first = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="dup-villa", price_numeric_min=1110000),
    )
    assert first.status_code == 201, first.text
    again = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="dup-villa", property_name="Dup Villa", price_numeric_min=999),
    )
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"].lower()
    row = _sql_row(world["db"], "dup-villa")
    doc = _fs_doc(world["admin_fs"], "dup-villa")
    assert float(row.price_numeric_min) == 1110000
    assert doc["priceFrom"] == 1110000
    assert doc["name"] == "Minimal Villa"


def test_firestore_failure_rolls_back_sql(world, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(
        "app.services.firestore_service.save_or_update_property_firestore",
        _boom,
    )
    res = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="rollback-villa"),
    )
    assert res.status_code == 502
    body = res.json()["detail"]
    assert body["sql"] is False
    assert body["firestore"] is False
    assert body["failed"] == "firestore"
    assert _sql_row(world["db"], "rollback-villa") is None
    assert _fs_doc(world["admin_fs"], "rollback-villa") is None


def test_update_then_undo_restores_firestore_bytes(world):
    original = {
        "name": "Byte Villa",
        "title": "Byte Villa",
        "propertyType": "Villa",
        "province": "Phuket",
        "priceFrom": 39000000,
        "priceTo": 42000000,
        "isActive": True,
        "createdAt": "2024-01-02T03:04:05",
        "status": "Available",
        "websites": ["tajlandia.com"],
        "customNote": "keep-me",
    }
    world["admin_fs"].set("projects", "byte-villa", original)
    world["db"].add(Property(
        property_id="byte-villa",
        property_name="Byte Villa",
        property_type="Villa",
        province="Phuket",
        price_numeric_min=39000000,
        price_numeric_max=42000000,
        status="Available",
        websites=["tajlandia.com"],
        is_public=True,
    ))
    world["db"].commit()

    updated = world["client"].put(
        "/api/v1/admin/properties/byte-villa",
        json={"price_numeric_min": 1000000},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["sql"] is True
    assert updated.json()["firestore"] is True
    assert _fs_doc(world["admin_fs"], "byte-villa")["priceFrom"] == 1000000
    assert _fs_doc(world["admin_fs"], "byte-villa")["createdAt"] == "2024-01-02T03:04:05"

    undone = world["client"].post(
        f"/api/v1/admin/operations/{updated.json()['operation_id']}/undo"
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["restored_ok"] is True
    assert undone.json()["differing_fields"] == []
    assert _fs_doc(world["admin_fs"], "byte-villa") == original
    assert float(_sql_row(world["db"], "byte-villa").price_numeric_min) == 39000000


def test_soft_delete_undo_restores_price_and_active(world):
    original = {
        "name": "Soft Villa",
        "title": "Soft Villa",
        "priceFrom": 8800000,
        "priceTo": 9000000,
        "isActive": True,
        "createdAt": "2023-05-01T00:00:00",
        "status": "Available",
        "propertyType": "Villa",
        "province": "Phuket",
        "websites": ["tajlandia.com"],
    }
    world["admin_fs"].set("projects", "soft-villa", original)
    world["db"].add(Property(
        property_id="soft-villa",
        property_name="Soft Villa",
        property_type="Villa",
        province="Phuket",
        price_numeric_min=8800000,
        status="Available",
        websites=["tajlandia.com"],
        is_public=True,
    ))
    world["db"].commit()
    deleted = world["client"].delete("/api/v1/admin/properties/soft-villa")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "ARCHIVED"
    assert deleted.json()["firestore"] is True
    assert deleted.json()["sql"] is True
    assert _fs_doc(world["admin_fs"], "soft-villa")["isActive"] is False
    assert _fs_doc(world["admin_fs"], "soft-villa")["priceFrom"] == 8800000

    undone = world["client"].post(
        f"/api/v1/admin/operations/{deleted.json()['operation_id']}/undo"
    )
    assert undone.status_code == 200, undone.text
    restored = _fs_doc(world["admin_fs"], "soft-villa")
    assert restored == original
    assert restored["priceFrom"] == 8800000
    assert restored["isActive"] is True
    row = _sql_row(world["db"], "soft-villa")
    assert row.is_public is True
    assert float(row.price_numeric_min) == 8800000
    assert row.status == "Available"


def test_create_undo_removes_both_stores_and_second_undo_is_refused(world):
    created = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="undo-create"),
    )
    assert created.status_code == 201, created.text
    op = created.json()["operation_id"]
    undone = world["client"].post(f"/api/v1/admin/operations/{op}/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["restored_ok"] is True
    assert _sql_row(world["db"], "undo-create") is None
    assert _fs_doc(world["admin_fs"], "undo-create") is None

    again = world["client"].post(f"/api/v1/admin/operations/{op}/undo")
    assert again.status_code == 400
    assert "already" in again.json()["detail"].lower()


def test_old_format_snapshot_is_refused(world):
    world["admin_fs"].set(
        "admin_batch_operations",
        "crud_oldcard",
        {
            "operation_id": "crud_oldcard",
            "action_type": "update",
            "status": "APPLIED",
            "expire_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "previous_snapshot": {
                "old-card": {
                    "property_id": "old-card",
                    "name": "Card Only",
                    "price": "Price on request",
                    "bedrooms": "1-4 Beds",
                    "province": "Thailand",
                }
            },
            "created_ids": [],
        },
    )
    res = world["client"].post("/api/v1/admin/operations/crud_oldcard/undo")
    assert res.status_code == 400
    assert res.json()["detail"] == OLD_SNAPSHOT_MESSAGE
    assert _fs_doc(world["admin_fs"], "old-card") is None


def test_admin_client_is_used_when_user_client_differs(world):
    created = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="client-split"),
    )
    assert created.status_code == 201, created.text
    op = created.json()["operation_id"]
    assert world["admin_fs"].get("admin_batch_operations", op) is not None
    world["user_fs"].set(
        "admin_batch_operations",
        op,
        {
            "operation_id": op,
            "action_type": "update",
            "status": "APPLIED",
            "expire_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "previous_snapshot": {"client-split": {"price": "Price on request"}},
        },
    )
    undone = world["client"].post(f"/api/v1/admin/operations/{op}/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["restored_ok"] is True
    assert _fs_doc(world["admin_fs"], "client-split") is None
    assert get_firestore_client(target="admin") is world["admin_fs"]
    assert get_firestore_client(target="user") is world["user_fs"]


def test_expired_undo_is_refused(world):
    created = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="expired-villa"),
    )
    op = created.json()["operation_id"]
    stored = world["admin_fs"].get("admin_batch_operations", op)
    stored["expire_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    stored["created_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    world["admin_fs"].set("admin_batch_operations", op, stored)
    res = world["client"].post(f"/api/v1/admin/operations/{op}/undo")
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()
    assert _sql_row(world["db"], "expired-villa") is not None


def test_firestore_only_update_does_not_invent_sql(world):
    world["admin_fs"].set(
        "projects",
        "fs-only",
        {
            "name": "Firestore Only",
            "title": "Firestore Only",
            "propertyType": "Villa",
            "province": "Phuket",
            "priceFrom": 5000000,
            "isActive": True,
            "status": "Available",
            "websites": ["tajlandia.com"],
        },
    )
    res = world["client"].put(
        "/api/v1/admin/properties/fs-only",
        json={"price_numeric_min": 6400000},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sql"] is False
    assert body["firestore"] is True
    assert _sql_row(world["db"], "fs-only") is None
    assert _fs_doc(world["admin_fs"], "fs-only")["name"] == "Firestore Only"
    assert _fs_doc(world["admin_fs"], "fs-only")["priceFrom"] == 6400000


def test_sql_only_update_writes_complete_firestore_doc(world):
    world["db"].add(Property(
        property_id="sql-only",
        property_name="SQL Only Villa",
        property_type="Villa",
        province="Phuket",
        price_numeric_min=3000000,
        status="Available",
        websites=["tajlandia.com"],
        is_public=True,
    ))
    world["db"].commit()
    res = world["client"].put(
        "/api/v1/admin/properties/sql-only",
        json={"price_numeric_min": 3100000},
    )
    assert res.status_code == 200, res.text
    assert res.json()["sql"] is True
    assert res.json()["firestore"] is True
    doc = _fs_doc(world["admin_fs"], "sql-only")
    assert doc["name"] == "SQL Only Villa"
    assert doc["title"] == "SQL Only Villa"
    assert doc["propertyType"] == "Villa"
    assert doc["province"] == "Phuket"
    assert doc["priceFrom"] == 3100000
    assert doc["isActive"] is True
    assert doc["websites"] == ["tajlandia.com"]


def test_sql_only_update_skips_firestore_when_sync_disabled(world, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", False)
    world["db"].add(Property(
        property_id="sql-nosync",
        property_name="No Sync Villa",
        property_type="Villa",
        province="Phuket",
        price_numeric_min=2000000,
        status="Available",
        websites=["tajlandia.com"],
        is_public=True,
    ))
    world["db"].commit()
    res = world["client"].put(
        "/api/v1/admin/properties/sql-nosync",
        json={"status": "Reserved"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["sql"] is True
    assert res.json()["firestore"] is False
    assert _fs_doc(world["admin_fs"], "sql-nosync") is None
    assert _sql_row(world["db"], "sql-nosync").status == "Reserved"


def test_permanent_delete_is_refused_by_default(world):
    world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="keep-villa"))
    res = world["client"].delete(
        "/api/v1/admin/properties/keep-villa?hard_delete=true&confirm_property_id=keep-villa"
    )
    assert res.status_code == 403, res.text
    assert _sql_row(world["db"], "keep-villa") is not None
    assert _fs_doc(world["admin_fs"], "keep-villa") is not None


def test_hard_delete_requires_typed_confirmation(world, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PERMANENT_DELETE", True)
    world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="hard-villa"))
    missing = world["client"].delete("/api/v1/admin/properties/hard-villa?hard_delete=true")
    assert missing.status_code == 400
    assert "confirm_property_id" in missing.json()["detail"]
    assert _sql_row(world["db"], "hard-villa") is not None
    assert _fs_doc(world["admin_fs"], "hard-villa") is not None

    wrong = world["client"].delete(
        "/api/v1/admin/properties/hard-villa?hard_delete=true&confirm_property_id=other"
    )
    assert wrong.status_code == 400
    gone = world["client"].delete(
        "/api/v1/admin/properties/hard-villa?hard_delete=true&confirm_property_id=hard-villa"
    )
    assert gone.status_code == 200, gone.text
    assert gone.json()["sql"] is True
    assert gone.json()["firestore"] is True
    assert _sql_row(world["db"], "hard-villa") is None
    assert _fs_doc(world["admin_fs"], "hard-villa") is None


def test_wrong_website_and_worker_are_forbidden(world):
    world["admin_fs"].set(
        "projects",
        "other-site",
        {
            "name": "Other Site",
            "title": "Other Site",
            "priceFrom": 1000,
            "isActive": True,
            "websites": ["varsoviaestate.com"],
            "status": "Available",
        },
    )
    denied = world["client"].put(
        "/api/v1/admin/properties/other-site",
        json={"price_numeric_min": 2000000},
        headers={"X-Selected-Websites": "tajlandia.com"},
    )
    assert denied.status_code == 403
    assert _fs_doc(world["admin_fs"], "other-site")["priceFrom"] == 1000

    world["holder"]["staff"] = _staff(role="worker")
    worker = world["client"].post("/api/v1/admin/properties", json=_minimal_body())
    assert worker.status_code == 403


def test_validation_rejects_bad_numbers_and_ids(world):
    cases = [
        (_minimal_body(price_numeric_min=-5), "price_numeric_min"),
        (_minimal_body(price_numeric_min=0), "price_numeric_min"),
        (_minimal_body(price_numeric_min=10, price_numeric_max=5), "price_numeric_max"),
        (_minimal_body(bedrooms_min=4, bedrooms_max=2), "bedrooms_max"),
        (_minimal_body(area_sqm_min=0), "area_sqm_min"),
        (_minimal_body(property_id="Bad_ID"), "property_id"),
        (_minimal_body(property_id="UPPER"), "property_id"),
        (_minimal_body(status="not-a-status"), "status"),
        (_minimal_body(property_type="castle"), "property_type"),
    ]
    for body, field in cases:
        res = world["client"].post("/api/v1/admin/properties", json=body)
        assert res.status_code == 422, res.text
        assert field in res.json()["detail"]["fields"]
    assert world["db"].query(Property).count() == 0

    ok, missing, msg = validate_property_data(
        {
            "mode": "update",
            "property_id": "p1",
            "is_update_only": True,
            "fields_to_update": {"price_numeric_min": -1, "bathrooms": 80},
        },
        existing_property=Property(property_id="p1", property_name="X"),
    )
    assert ok is False
    assert "price_numeric_min" in missing
    assert "bathrooms" in missing
    assert "greater than 0" in msg

    batch_errors = validate_listing_values(
        {"price_numeric_min": 0, "bedrooms_min": 5, "bedrooms_max": 1},
        is_create=False,
    )
    assert "price_numeric_min" in batch_errors
    assert "bedrooms_max" in batch_errors


def test_batch_path_rejects_negative_price_before_write():
    staff = _staff()
    fake = FakeFirestore()
    fake.set("projects", "batch-villa", {"name": "Batch", "priceFrom": 10, "isActive": True})

    def _client(target="user", *args, **kwargs):
        return fake

    from app.services import admin_batch_service
    original = admin_batch_service.get_firestore_client
    admin_batch_service.get_firestore_client = _client
    try:
        with pytest.raises(BatchOperationError) as exc:
            execute_batch_property_operation(
                action_type="update",
                records=[{"property_id": "batch-villa", "priceFrom": -5}],
                staff_user=staff,
                source_filename="bad.csv",
            )
    finally:
        admin_batch_service.get_firestore_client = original
    assert "price_numeric_min" in str(exc.value)
    assert fake.get("projects", "batch-villa")["priceFrom"] == 10


def test_audit_entries_and_disabled_flag(world, monkeypatch):
    created = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="audit-villa"),
    )
    assert created.status_code == 201, created.text
    logs = world["db"].query(AuditLog).all()
    assert len(logs) == 1
    log = logs[0]
    assert log.tool_name == "admin_rest_create"
    args = log.tool_arguments or {}
    assert args.get("actor") == "admin@tajlandia.com"
    assert args.get("property_id") == "audit-villa"
    assert args.get("operation_id") == created.json()["operation_id"]
    blob = str(log.proposed_payload)
    assert "address" not in blob
    assert "description" not in blob

    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", False)
    muted = world["client"].put(
        "/api/v1/admin/properties/audit-villa",
        json={"price_numeric_min": 2600000},
    )
    assert muted.status_code == 200, muted.text
    assert muted.json()["audit"] == "disabled"
    assert world["db"].query(AuditLog).count() == 1


def test_search_cache_invalidated_on_create_and_undo(world):
    search_cache.set("golden-key", [{"property_id": "stale"}])
    version = search_cache._version
    created = world["client"].post(
        "/api/v1/admin/properties",
        json=_minimal_body(property_id="cache-villa"),
    )
    assert created.status_code == 201, created.text
    assert search_cache._cache == {}
    assert search_cache._version == version + 1
    search_cache.set("golden-key", [{"property_id": "stale"}])
    undone = world["client"].post(
        f"/api/v1/admin/operations/{created.json()['operation_id']}/undo"
    )
    assert undone.status_code == 200, undone.text
    assert search_cache._cache == {}


def test_pirak_villa_consistency_lists_differences(world):
    world["admin_fs"].set(
        "projects",
        "pirak-villa",
        {
            "name": "Pirak Villa",
            "title": "Pirak Villa",
            "propertyType": "Villa",
            "province": "Phuket",
            "priceFrom": 39990000,
            "isActive": True,
            "status": "Available",
            "websites": ["tajlandia.com"],
        },
    )
    world["db"].add(Property(
        property_id="pirak-villa",
        property_name="Pirak Villa",
        property_type="Villa",
        province="Phuket",
        price_numeric_min=35000000,
        status="Under construction",
        websites=["tajlandia.com"],
        is_public=False,
    ))
    world["db"].commit()
    res = world["client"].get("/api/v1/admin/properties/pirak-villa/consistency")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["consistent"] is False
    fields = {item["field"]: item for item in body["differences"]}
    assert fields["price"]["firestore"] == 39990000
    assert fields["price"]["sql"] == 35000000
    assert fields["status"]["firestore"] == "Available"
    assert fields["status"]["sql"] == "Under construction"
    assert fields["isActive"]["firestore"] is True
    assert fields["isActive"]["sql"] is False
    assert "name" not in fields
    assert "websites" not in fields


def test_overwrite_flag_is_rejected(world):
    res = world["client"].post(
        "/api/v1/admin/properties?overwrite=true",
        json=_minimal_body(property_id="no-overwrite"),
    )
    assert res.status_code == 400
    assert "overwrite" in res.json()["detail"]
    assert _sql_row(world["db"], "no-overwrite") is None
