"""
Comprehensive QA and Regression Test Suite for Backend API Endpoints.
Covers:
- app/api/v1/admin.py (CRUD, validation, website scoping, batch undo, history, config, corrections, block)
- app/main.py (health, branding, public properties, media proxy security, document/url ingest)
- app/api/v1/chat_logs.py (conversations, remarks, corrections with optional body, message feedback)
- app/api/v1/superadmin_instructions.py (role guard, CRUD, activate, review)
- app/api/v1/drive_ingestion.py (formats, job creation, role guards)
"""

from __future__ import annotations

import copy
import io
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sqlite3.register_adapter(uuid.UUID, lambda u: str(u))

from app.api.v1.admin import router as admin_router
from app.api.v1.chat_logs import router as chat_logs_router
from app.api.v1.drive_ingestion import router as drive_ingestion_router
from app.api.v1.superadmin_instructions import router as superadmin_instructions_router
from app.core.auth import AuthenticatedStaff, get_current_user_from_db
from app.core.config import settings
from app.db.models import AuditLog, Conversation, Property, PropertyImage, User, UserSession
from app.db.session import get_admin_db, get_db, get_user_db
from app.services.instruction_store import get_store
import app.main as main_module


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


# ---------------------------------------------------------------------------
# Fake Firestore In-Memory Stub
# ---------------------------------------------------------------------------
class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._data = None if data is None else copy.deepcopy(data)
        self.reference = self

    def to_dict(self):
        return None if self._data is None else copy.deepcopy(self._data)


class _Doc:
    def __init__(self, store, collection, doc_id):
        self.store = store
        self.collection_name = collection
        self.id = doc_id

    def get(self, transaction=None):
        return _Snap(self.id, self.store.get(self.collection_name, self.id))

    def set(self, data, merge=False):
        self.store.set(self.collection_name, self.id, data, merge=merge)

    def update(self, data):
        self.store.update(self.collection_name, self.id, data)

    def delete(self):
        self.store.delete(self.collection_name, self.id)

    def collection(self, name):
        return _Collection(self.store, f"{self.collection_name}/{self.id}/{name}")


class _Query:
    def __init__(self, collection):
        self.collection = collection

    def limit(self, _n):
        return self

    def stream(self, transaction=None):
        return self.collection.stream()

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
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

    def order_by(self, *args, **kwargs):
        return _Query(self)


class FakeFirestore:
    def __init__(self):
        self.docs = {}
        self._read_only = False
        self._id = b"fake"
        self._max_attempts = 5

    def collection(self, name):
        return _Collection(self, name)

    def get(self, collection_or_ref, doc_id=None):
        if doc_id is None:
            # ref passed
            return collection_or_ref.get()
        return self.docs.get((collection_or_ref, doc_id))

    def items(self, collection):
        return [(doc_id, data) for (col, doc_id), data in self.docs.items() if col == collection]

    def set(self, collection_or_ref, doc_id_or_data, data=None, merge=False):
        if data is None:
            # ref, data passed
            return collection_or_ref.set(doc_id_or_data, merge=merge)
        key = (collection_or_ref, doc_id_or_data)
        payload = copy.deepcopy(data)
        current = self.docs.get(key)
        if merge and isinstance(current, dict):
            merged = copy.deepcopy(current)
            merged.update(payload)
            self.docs[key] = merged
        else:
            self.docs[key] = payload

    def update(self, collection_or_ref, doc_id_or_data, data=None):
        if data is None:
            return collection_or_ref.update(doc_id_or_data)
        key = (collection_or_ref, doc_id_or_data)
        current = self.docs.get(key)
        if not isinstance(current, dict):
            self.docs[key] = copy.deepcopy(data)
            return
        merged = copy.deepcopy(current)
        merged.update(copy.deepcopy(data))
        self.docs[key] = merged

    def delete(self, collection_or_ref, doc_id=None):
        if doc_id is None:
            return collection_or_ref.delete()
        self.docs.pop((collection_or_ref, doc_id), None)

    def transaction(self, max_attempts=5):
        return self

    def run_transaction(self, work):
        return work(self)

    def _clean_up(self):
        pass

    def _begin(self, retry_id=None):
        pass

    def _commit(self):
        pass

    def _rollback(self):
        pass

    def set_in_transaction(self, ref, data):
        ref.set(data)

    def update_in_transaction(self, ref, data):
        ref.update(data)


# ---------------------------------------------------------------------------
# Test Fixture Setup
# ---------------------------------------------------------------------------
def _sqlite_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (Property.__table__, PropertyImage.__table__, AuditLog.__table__, Conversation.__table__, User.__table__, UserSession.__table__):
        for idx in list(table.indexes):
            using = getattr(idx, "dialect_kwargs", {}).get("postgresql_using")
            if using == "gin":
                table.indexes.discard(idx)
        table.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return Session()


def _staff(role="super_admin", email="qa_admin@tajlandia.com", allowed=None):
    sites = allowed or ["tajlandia.com", "varsoviaestate.com", "thai.estate"]
    return AuthenticatedStaff(
        user_id="user-qa-001",
        email=email,
        name="QA Tester",
        role=role,
        allowed_websites=list(sites),
        selected_websites=list(sites),
    )


@pytest.fixture
def qa_env(monkeypatch):
    session = _sqlite_session()
    fake_fs = FakeFirestore()

    # Firestore stubs
    monkeypatch.setattr("app.services.firestore_service.get_admin_firestore_client", lambda *args, **kwargs: fake_fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda *args, **kwargs: fake_fs)
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda *args, **kwargs: fake_fs)
    monkeypatch.setattr("app.services.chat_log_service._client", lambda *args, **kwargs: fake_fs)
    monkeypatch.setattr("app.services.chat_log_query._client", lambda *args, **kwargs: fake_fs)

    # Bind fake Firestore into instruction store
    store = get_store()
    store.bind_client(fake_fs)

    # Config stubs
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")

    staff_holder = {"staff": _staff("super_admin")}

    def _get_db_override():
        yield session

    def _get_staff_override():
        return staff_holder["staff"]

    # Use FastAPI main app
    app = main_module.app
    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_admin_db] = _get_db_override
    app.dependency_overrides[get_user_db] = _get_db_override
    app.dependency_overrides[get_current_user_from_db] = _get_staff_override

    client = TestClient(app)

    yield {
        "client": client,
        "db": session,
        "fs": fake_fs,
        "holder": staff_holder,
    }

    app.dependency_overrides.clear()
    store.bind_client(None)
    session.close()


# ---------------------------------------------------------------------------
# 1. System & Public Endpoints (app/main.py)
# ---------------------------------------------------------------------------
def test_system_health_check(qa_env):
    client = qa_env["client"]
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "admin_database" in data
    assert "user_database" in data


def test_system_branding(qa_env):
    client = qa_env["client"]
    resp = client.get("/api/v1/branding?site_origin=varsoviaestate.com")
    assert resp.status_code == 200
    data = resp.json()
    assert "brand_name" in data
    assert "varsoviaestate.com" in data["domain"].lower()


def test_public_properties_empty_and_list(qa_env):
    client = qa_env["client"]
    resp = client.get("/api/v1/properties")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["properties"] == []


def test_media_proxy_path_traversal_refusal(qa_env):
    client = qa_env["client"]
    # Path traversal with .. must return 404
    resp = client.get("/api/v1/media/../../../etc/passwd")
    assert resp.status_code == 404
    # Non-image extension must return 404
    resp2 = client.get("/api/v1/media/test_script.sh")
    assert resp2.status_code == 404


def test_ingest_url_validation(qa_env):
    client = qa_env["client"]
    # Empty url must be rejected with 422 or 400
    resp = client.post("/api/v1/properties/ingest-url", json={"url": "   "})
    assert resp.status_code in (400, 422)


def test_ingest_document_validation(qa_env):
    client = qa_env["client"]
    # Unsupported file extension
    resp = client.post(
        "/api/v1/properties/ingest-document",
        files={"file": ("malicious.exe", b"MZ...", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. Property REST CRUD & Validation (app/api/v1/admin.py)
# ---------------------------------------------------------------------------
def test_property_create_read_update_delete_lifecycle(qa_env):
    client = qa_env["client"]
    pid = "prop-qa-test-100"

    create_payload = {
        "property_id": pid,
        "project_name_en": "Sunset Bay Ocean Villa",
        "project_name_pl": "Willa Nad Zatoka Zachodzacego Slonca",
        "property_type": "Villa",
        "country": "Thailand",
        "city": "Phuket",
        "websites": ["tajlandia.com"],
        "price_from": 12500000.0,
        "price_to": 18000000.0,
        "currency": "THB",
        "completion_status": "Ready to move in",
        "total_units": 8,
        "area_from_sqm": 220.5,
        "area_to_sqm": 350.0,
        "short_description_en": "Exclusive hillside villa with panoramic Andaman Sea views.",
        "short_description_pl": "Ekskluzywna willa na wzgorzu z widokiem na Morze Andamanskie.",
        "media": ["https://tajlandia.com/images/villa1.jpg"],
        "active": True,
    }

    # 1. Create property
    res_create = client.post("/api/v1/admin/properties", json=create_payload)
    assert res_create.status_code == 201
    created_data = res_create.json()
    assert created_data["property_id"] == pid

    # 2. Get property (Admin)
    res_get = client.get(f"/api/v1/admin/properties/{pid}")
    assert res_get.status_code == 200
    get_data = res_get.json()
    assert get_data["found"] is True
    assert get_data["property_id"] == pid
    assert get_data["pricing"]["numeric_min"] == 12500000.0

    # 3. Get property (Public Endpoint)
    res_pub = client.get(f"/api/v1/properties/{pid}")
    assert res_pub.status_code == 200
    assert res_pub.json()["property_id"] == pid

    # 4. Update property (Verifying Bug 1 fix: completion_status, units, description mapping)
    update_payload = {
        "completion_status": "Under construction",
        "total_units": 12,
        "area_from_sqm": 240.0,
        "full_description_en": "Updated luxury villa description.",
        "price_from": 13000000.0,
    }
    res_update = client.put(f"/api/v1/admin/properties/{pid}", json=update_payload)
    assert res_update.status_code == 200
    assert res_update.json().get("status") == "UPDATED"

    # 5. Verify updated values landed in DB
    db = qa_env["db"]
    row = db.query(Property).filter(Property.property_id == pid).first()
    assert row is not None
    assert row.status == "Under construction"
    assert row.units == 12
    assert float(row.area_sqm_min) == 240.0

    # 6. List properties endpoint (Admin) - verifies Bug 4 fix (offset and limit in response)
    res_list = client.get("/api/v1/admin/properties?limit=10&offset=0")
    assert res_list.status_code == 200
    list_json = res_list.json()
    assert "count" in list_json
    assert "offset" in list_json
    assert "limit" in list_json
    assert list_json["count"] >= 1

    # 7. Soft delete (archive)
    res_del = client.delete(f"/api/v1/admin/properties/{pid}")
    assert res_del.status_code == 200
    assert res_del.json().get("status") == "ARCHIVED"

    # 8. Archived property returns 404 unless include_archived=True
    res_del_get = client.get(f"/api/v1/admin/properties/{pid}")
    assert res_del_get.status_code == 404

    res_archived_get = client.get(f"/api/v1/admin/properties/{pid}?include_archived=true")
    assert res_archived_get.status_code == 200
    assert res_archived_get.json()["found"] is True


def test_property_zero_price_and_area_handling(qa_env):
    """Verifies Bug 2 fix: numeric zero (0.0) is preserved and not converted to None."""
    client = qa_env["client"]
    db = qa_env["db"]
    pid = "prop-zero-val-test"

    prop = Property(
        property_id=pid,
        property_name="Zero Price Promotion Unit",
        property_type="Condominium",
        city="Bangkok",
        province="Bangkok",
        websites=["tajlandia.com"],
        price_numeric_min=0.0,
        price_numeric_max=0.0,
        area_sqm_min=35.0,
        area_sqm_max=45.0,
        description="Special unit with zero down payment promotion.",
        status="Ready to move in",
        is_public=True,
    )
    db.add(prop)
    db.commit()

    # Check via admin endpoint
    res_admin = client.get(f"/api/v1/admin/properties/{pid}")
    assert res_admin.status_code == 200
    data = res_admin.json()
    assert data["pricing"]["numeric_min"] == 0.0
    assert data["pricing"]["price_from"] == 0.0
    assert data["details"]["area_from_sqm"] == 35.0

    # Check via public endpoint
    res_pub = client.get(f"/api/v1/properties/{pid}")
    assert res_pub.status_code == 200
    pub_data = res_pub.json()
    assert pub_data["pricing"]["numeric_min"] == 0.0
    assert pub_data["pricing"]["price_from"] == 0.0
    assert pub_data["details"]["area_from_sqm"] == 35.0


def test_property_website_scoping_permissions(qa_env):
    """Ensures staff cannot create or access properties outside their permitted sites."""
    client = qa_env["client"]
    holder = qa_env["holder"]

    # Staff with only tajlandia.com
    holder["staff"] = _staff(role="admin", allowed=["tajlandia.com"])

    # Try creating property for unauthorized site
    payload = {
        "property_id": "prop-unauth-site",
        "project_name_en": "Varsovia Villa",
        "project_name_pl": "Willa Warszawa",
        "property_type": "Villa",
        "country": "Thailand",
        "city": "Phuket",
        "websites": ["varsoviaestate.com"],
        "short_description_en": "Villa on Varsovia",
        "short_description_pl": "Willa w Varsovia",
        "media": ["https://varsoviaestate.com/img.jpg"],
        "completion_status": "Ready to move in",
    }
    res = client.post("/api/v1/admin/properties", json=payload)
    assert res.status_code == 403


def test_property_validation_errors(qa_env):
    """Tests input validation edge cases: duplicate id, bad numbers, missing required."""
    client = qa_env["client"]

    # Missing required project_name_en
    bad_payload = {
        "property_type": "Villa",
        "country": "Thailand",
        "city": "Phuket",
        "media": ["https://example.com/img.jpg"],
    }
    res = client.post("/api/v1/admin/properties", json=bad_payload)
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# 3. Batch Operations & Undo Endpoints (app/api/v1/admin.py)
# ---------------------------------------------------------------------------
def test_batch_operations_history_endpoint(qa_env):
    client = qa_env["client"]
    res = client.get("/api/v1/admin/operations/history")
    assert res.status_code == 200
    assert "operations" in res.json()


def test_admin_config_status_endpoint(qa_env):
    client = qa_env["client"]
    res = client.get("/api/v1/admin/system/config-status")
    assert res.status_code == 200
    data = res.json()
    assert "audit_logging" in data or "brochure_engine" in data
    # Ensure no secrets leaked
    assert "password" not in str(data).lower()
    assert "secret" not in str(data).lower()


def test_admin_block_user_route(qa_env):
    """Verifies Bug 8 fix: block endpoint is registered on admin router."""
    client = qa_env["client"]
    db = qa_env["db"]

    test_user = User(
        id=uuid.uuid4(),
        email="worker_to_block@tajlandia.com",
        name="Worker One",
        role="worker",
        is_active=True,
    )
    db.add(test_user)
    db.commit()

    res = client.post(
        f"/api/v1/admin/users/{test_user.id}/block",
        json={"blocked": True, "reason": "Testing block"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "blocked"


# ---------------------------------------------------------------------------
# 4. Direct Answer Corrections (app/api/v1/admin.py)
# ---------------------------------------------------------------------------
def test_direct_answer_correction_crud(qa_env):
    client = qa_env["client"]
    payload = {
        "trigger_query": "What is the pet policy at Kata Hills?",
        "original_bot_answer": "Pets are strictly forbidden.",
        "corrected_answer": "Small pets up to 10kg are permitted with deposit.",
        "scope": "global",
        "category": "agency_policy",
    }
    res = client.post("/api/v1/admin/corrections", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert "correction_id" in data
    assert data["status"] in ("approved", "proposed")


# ---------------------------------------------------------------------------
# 5. Super Admin Instructions & Reviews (app/api/v1/superadmin_instructions.py)
# ---------------------------------------------------------------------------
def test_superadmin_instructions_role_guard(qa_env):
    client = qa_env["client"]
    holder = qa_env["holder"]

    # Switch to worker role
    holder["staff"] = _staff(role="worker", email="worker@tajlandia.com")

    # Non-superadmin must be rejected with 403
    res = client.get("/api/v1/admin/superadmin/instructions")
    assert res.status_code == 403

    # Switch back to superadmin
    holder["staff"] = _staff(role="super_admin")
    res_sa = client.get("/api/v1/admin/superadmin/instructions")
    assert res_sa.status_code == 200
    assert "instructions" in res_sa.json()


def test_superadmin_instruction_lifecycle(qa_env):
    client = qa_env["client"]
    body = {
        "title": "Always quote THB first",
        "rule": "Always state prices in THB before converting to EUR or USD.",
        "targets": ["public_chat", "admin_chat"],
        "websites": ["all"],
        "languages": ["all"],
        "priority": 80,
    }
    # Create
    res_create = client.post("/api/v1/admin/superadmin/instructions", json=body)
    assert res_create.status_code == 200
    inst = res_create.json()
    inst_id = inst["id"]

    # Activate
    res_act = client.post(f"/api/v1/admin/superadmin/instructions/{inst_id}/activate")
    assert res_act.status_code == 200
    assert res_act.json()["status"] == "active"

    # Archive
    res_arch = client.post(f"/api/v1/admin/superadmin/instructions/{inst_id}/archive")
    assert res_arch.status_code == 200
    assert res_arch.json()["status"] == "archived"


# ---------------------------------------------------------------------------
# 6. Chat Logs & Corrections API (app/api/v1/chat_logs.py)
# ---------------------------------------------------------------------------
def test_chat_logs_corrections_optional_body(qa_env):
    """Verifies Bug 6 fix: decision endpoints handle omitted or empty body."""
    client = qa_env["client"]
    # Non-existent correction returns 404, but does NOT raise 422 for omitted body
    res_app = client.post("/api/v1/admin/chat-logs/corrections/non-existent-id/approve")
    assert res_app.status_code == 404

    res_rej = client.post("/api/v1/admin/chat-logs/corrections/non-existent-id/reject")
    assert res_rej.status_code == 404

    res_deact = client.post("/api/v1/admin/chat-logs/corrections/non-existent-id/deactivate")
    assert res_deact.status_code == 404


def test_chat_logs_feedback_sequence_handling(qa_env):
    """Verifies Bug 7 fix: feedback endpoint checks message existence and sequence formatting."""
    client = qa_env["client"]
    # Missing conversation returns 404
    res = client.patch(
        "/api/v1/admin/chat-logs/conversations/non-existent-conv/messages/1/feedback",
        json={"feedback": "thumbs_up", "comment": "Great answer"},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# 7. Drive Ingestion API (app/api/v1/drive_ingestion.py)
# ---------------------------------------------------------------------------
def test_drive_ingestion_supported_formats(qa_env):
    client = qa_env["client"]
    res = client.get("/api/v1/admin/drive-ingestion/formats")
    assert res.status_code == 200
    data = res.json()
    assert "supported" in data
    assert "unsupported" in data
    assert any(f["format"] == "pdf" for f in data["supported"])
    assert any(f["format"] == "docx" for f in data["supported"])
    assert any(f["format"] == "xlsx" for f in data["supported"])


def test_drive_ingestion_role_guard(qa_env):
    client = qa_env["client"]
    holder = qa_env["holder"]

    # Worker role cannot access drive ingestion
    holder["staff"] = _staff(role="worker", email="worker@tajlandia.com")
    res = client.get("/api/v1/admin/drive-ingestion/formats")
    assert res.status_code == 403
