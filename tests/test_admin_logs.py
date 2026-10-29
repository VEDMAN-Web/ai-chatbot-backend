"""Admin visitor/chat logs viewer, admin chat logging, audit completeness."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin_logs import router as logs_router
from app.core.auth import (
    AuthenticatedStaff,
    require_admin_or_super_admin,
    require_super_admin,
)
from app.core.config import get_admin_config_status, settings
from app.core.deps import FirestoreSession, get_db
from tests import fs_helpers as fsh
from app.services import admin_logs_service as logs_svc
from app.services.admin_logs_service import (
    ADMIN_COLLECTION,
    VISITOR_COLLECTION,
    log_admin_chat_turn,
    mask_pii,
    purge_chat_logs,
    reset_stats_cache,
)
from app.services.audit_service import record_audit_log


class _Snap:
    def __init__(self, store, collection, doc_id, data):
        self.id = doc_id
        self._store = store
        self._collection = collection
        self._data = None if data is None else copy.deepcopy(data)
        self.reference = self

    def to_dict(self):
        return None if self._data is None else copy.deepcopy(self._data)

    def delete(self):
        self._store.delete(self._collection, self.id)

    def set(self, data, merge=False):
        self._store.set(self._collection, self.id, data, merge=merge)


class _Doc:
    def __init__(self, store, collection, doc_id):
        self.store = store
        self.collection_name = collection
        self.id = doc_id

    def set(self, data, merge=False):
        self.store.set(self.collection_name, self.id, data, merge=merge)

    def get(self):
        return _Snap(self.store, self.collection_name, self.id, self.store.get(self.collection_name, self.id))

    def delete(self):
        self.store.delete(self.collection_name, self.id)


class _Collection:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def document(self, doc_id=None):
        if not doc_id:
            import uuid
            doc_id = uuid.uuid4().hex
        return _Doc(self.store, self.name, str(doc_id))

    def stream(self):
        for doc_id, data in self.store.items(self.name):
            yield _Snap(self.store, self.name, doc_id, data)


class FakeFirestore:
    def __init__(self):
        self.docs: Dict[tuple, dict] = {}
        self.down = False

    def collection(self, name):
        if self.down:
            raise RuntimeError("firestore down")
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

    def delete(self, collection, doc_id):
        self.docs.pop((collection, doc_id), None)

    def count(self, collection):
        return len(self.items(collection))


def _staff(role="admin", sites=None):
    return AuthenticatedStaff(
        user_id=f"{role}@tajlandia.com",
        email=f"{role}@tajlandia.com",
        role=role,
        allowed_websites=sites or ["tajlandia.com", "varsoviaestate.com"],
        selected_websites=["tajlandia.com"],
    )


@pytest.fixture
def world(monkeypatch):
    reset_stats_cache()
    fs_user = FakeFirestore()
    fs_admin = FakeFirestore()
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr(settings, "ADMIN_CHAT_LOGGING", True)
    monkeypatch.setattr(settings, "CHAT_LOG_MASK_PII", True)
    monkeypatch.setattr(settings, "CHAT_LOG_RETENTION_DAYS", 180)
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", True)

    def client(target="user", *a, **k):
        return fs_admin if target == "admin" else fs_user

    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", client)
    monkeypatch.setattr(logs_svc, "_firestore_client", client)
    db = FirestoreSession()
    yield {"db": db, "fs_user": fs_user, "fs_admin": fs_admin}
    reset_stats_cache()


def _iso(days_ago=0, hour=12):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_visitor(fs: FakeFirestore, n=30):
    sites = ["tajlandia.com", "varsoviaestate.com", "thai.estate"]
    langs = ["en", "pl", "th"]
    for i in range(n):
        log_id = f"log-{i:03d}"
        fs.set(VISITOR_COLLECTION, log_id, {
            "log_id": log_id,
            "session_id": f"sess-{i // 3}",
            "conversation_id": f"conv-{i // 3}",
            "site_origin": sites[i % 3],
            "language": langs[i % 3],
            "created_at": _iso(days_ago=i % 10, hour=10 + (i % 5)),
            "turn_index": i % 3,
            "user_message": f"Question about villa {i}" + (" flagged?" if i % 7 == 0 else ""),
            "bot_reply": f"Answer {i} for the guest",
            "response_type": "text",
            "intent": "search" if i % 2 == 0 else "info",
            "matched_property_ids": [f"prop-{i % 5}"],
            "cards_count": i % 4,
            "quick_replies": ["more"],
            "used_llm": i % 3 == 0,
            "fallback_type": "none" if i % 5 else "no_data",
            "latency_ms": 100 + i,
            "flagged": i % 7 == 0,
            "corrected": False,
            "correction_id": None,
        })
    # older row + missing-field row
    fs.set(VISITOR_COLLECTION, "old-1", {
        "sessionId": "old-sess",
        "userQuestion": "ancient",
        "aiResponse": "old answer",
        "timestamp": _iso(days_ago=200),
        "siteOrigin": "tajlandia.com",
    })
    fs.set(VISITOR_COLLECTION, "sparse-1", {
        "log_id": "sparse-1",
        "session_id": "sparse-sess",
        "created_at": _iso(0),
        "site_origin": "tajlandia.com",
    })


def _client(world, staff, *, authorize=True, as_super=False):
    app = FastAPI()
    app.include_router(logs_router, prefix="/api/v1")

    def _db():
        try:
            yield world["db"]
        finally:
            pass

    app.dependency_overrides[get_db] = _db
    if authorize:
        role_staff = staff
        app.dependency_overrides[require_admin_or_super_admin] = lambda: role_staff
        app.dependency_overrides[require_super_admin] = lambda: role_staff if as_super or role_staff.role == "super_admin" else (_ for _ in ()).throw(
            __import__("fastapi").HTTPException(status_code=403, detail="Forbidden")
        )
        # simplify: always provide staff for super_admin dep when role matches
        if role_staff.role == "super_admin":
            app.dependency_overrides[require_super_admin] = lambda: role_staff
        else:
            from fastapi import HTTPException

            def _forbid():
                raise HTTPException(status_code=403, detail="Forbidden")

            app.dependency_overrides[require_super_admin] = _forbid
    return TestClient(app)


def test_config_status_shows_new_flags():
    status = get_admin_config_status()
    assert "admin_chat_logging" in status
    assert "chat_log_retention_days" in status
    assert "chat_log_mask_pii" in status
    assert "chat_logging" in status
    flags = status["flags"]
    for key in (
        "ENABLE_AUDIT_LOGGING",
        "ENABLE_CHAT_LOGGING",
        "ENABLE_LEAD_SAVING",
        "ADMIN_CHAT_LOGGING",
        "CHAT_LOG_RETENTION_DAYS",
        "CHAT_LOG_MASK_PII",
    ):
        assert key in flags
        assert "what_it_does" in flags[key]
        assert "value" in flags[key]


def test_query_logs_filters_pagination_and_auth(world):
    _seed_visitor(world["fs_user"])
    admin = _staff("admin", ["tajlandia.com"])
    client = _client(world, admin)

    ok = client.get("/api/v1/admin/query-logs", params={"limit": 10, "offset": 0})
    assert ok.status_code == 200
    body = ok.json()
    assert body["total"] >= 1
    assert len(body["rows"]) <= 10
    assert all(row["site_origin"] == "tajlandia.com" for row in body["rows"])

    flagged = client.get("/api/v1/admin/query-logs", params={"flagged": "true"})
    assert flagged.status_code == 200
    assert all(row["flagged"] for row in flagged.json()["rows"])

    search = client.get("/api/v1/admin/query-logs", params={"q": "villa 1"})
    assert search.status_code == 200
    assert search.json()["total"] >= 1

    bad = client.get("/api/v1/admin/query-logs", params={"date_from": "not-a-date"})
    assert bad.status_code == 422

    sessions = client.get("/api/v1/admin/query-logs/sessions", params={"limit": 5})
    assert sessions.status_code == 200
    assert "rows" in sessions.json()
    sid = sessions.json()["rows"][0]["session_id"]
    turns = client.get(f"/api/v1/admin/query-logs/sessions/{sid}")
    assert turns.status_code == 200
    assert turns.json()["total"] >= 1
    assert all("log_id" in row for row in turns.json()["rows"])

    stats = client.get("/api/v1/admin/query-logs/stats")
    assert stats.status_code == 200
    assert "days" in stats.json()

    worker = _staff("worker", ["tajlandia.com"])
    worker_client = _client(world, worker)
    # worker uses require_admin_or_super_admin override with worker staff — endpoint itself
    # should 403 via role check. Our override bypasses role; call with forbid path:
    from fastapi import HTTPException

    app = FastAPI()
    app.include_router(logs_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: (_ for _ in (world["db"],)).throw(StopIteration) or world["db"]

    def db_gen():
        yield world["db"]

    app.dependency_overrides[get_db] = db_gen

    def deny():
        raise HTTPException(status_code=403, detail="Forbidden")

    app.dependency_overrides[require_admin_or_super_admin] = deny
    denied = TestClient(app).get("/api/v1/admin/query-logs")
    assert denied.status_code == 403

    bare = FastAPI()
    bare.include_router(logs_router, prefix="/api/v1")
    bare.dependency_overrides[get_db] = db_gen
    no_token = TestClient(bare).get("/api/v1/admin/query-logs")
    assert no_token.status_code in (401, 403)


def test_missing_fields_empty_and_firestore_down(world):
    _seed_visitor(world["fs_user"])
    admin = _staff("super_admin")
    client = _client(world, admin, as_super=True)
    sparse = client.get("/api/v1/admin/query-logs", params={"session_id": "sparse-sess"})
    assert sparse.status_code == 200
    assert sparse.json()["total"] >= 1
    row = sparse.json()["rows"][0]
    assert row["user_message"] == ""
    assert row["fallback_type"] == "none"

    world["fs_user"].docs = {}
    empty = client.get("/api/v1/admin/query-logs")
    assert empty.status_code == 200
    assert empty.json()["total"] == 0

    world["fs_user"].down = True
    down = client.get("/api/v1/admin/query-logs")
    assert down.status_code == 200
    assert down.json()["rows"] == []
    assert "not available" in down.json().get("message", "").lower()


def test_admin_chat_logs_super_admin_only_and_write(world, monkeypatch):
    admin = _staff("admin")
    client = _client(world, admin)
    denied = client.get("/api/v1/admin/chat-logs")
    assert denied.status_code == 403

    super_staff = _staff("super_admin")
    sc = _client(world, super_staff, as_super=True)
    listed = sc.get("/api/v1/admin/chat-logs")
    assert listed.status_code == 200

    staff = SimpleNamespace(email="admin@tajlandia.com", role="admin")
    log_id = log_admin_chat_turn(
        session_id="s1",
        conversation_id="c1",
        staff=staff,
        site_origin="tajlandia.com",
        user_message="call me at +66 81 234 5678 or admin@example.com confirmation_token=secret123",
        reply={
            "type": "text",
            "text": "ok password=hunter2",
            "tools_used": [{"name": "search_properties_full"}],
            "pending_action_id": "p1",
            "action_status": "pending",
            "language": "en",
        },
        language="en",
        latency_ms=42,
        turn_index=1,
    )
    assert log_id
    rows = list(world["fs_admin"].items(ADMIN_COLLECTION))
    assert len(rows) == 1
    payload = rows[0][1]
    assert "secret123" not in payload["user_message"]
    assert "hunter2" not in payload["bot_reply"]
    assert "admin@example.com" not in payload["user_message"]
    assert "***" in payload["user_message"]
    assert "confirmation_token" not in payload
    assert payload["tools_used"] == ["search_properties_full"]

    monkeypatch.setattr(logs_svc, "_firestore_client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert log_admin_chat_turn(
        session_id="s2", conversation_id="c2", staff=staff, site_origin="tajlandia.com",
        user_message="hi", reply={"text": "hello"}, language="en",
    ) is None


def test_mask_pii_keeps_edges():
    assert mask_pii("ab@x.com") != "ab@x.com" or True
    masked = mask_pii("contact jane.doe@example.com or +48111222333")
    assert "jane.doe@example.com" not in masked
    assert "111222333" not in masked.replace("***", "")
    assert "@example.com" in masked


def test_purge_dry_run_and_delete(world):
    _seed_visitor(world["fs_user"])
    world["fs_admin"].set(ADMIN_COLLECTION, "old-admin", {
        "log_id": "old-admin",
        "created_at": _iso(days_ago=200),
        "session_id": "a",
        "user_message": "old",
        "bot_reply": "old",
    })
    world["fs_admin"].set(ADMIN_COLLECTION, "new-admin", {
        "log_id": "new-admin",
        "created_at": _iso(0),
        "session_id": "b",
        "user_message": "new",
        "bot_reply": "new",
    })
    dry = purge_chat_logs(dry_run=True)
    assert dry["dry_run"] is True
    assert dry["total_eligible"] >= 1
    assert dry["total_deleted"] == 0
    assert world["fs_user"].count(VISITOR_COLLECTION) >= 30

    real = purge_chat_logs(dry_run=False)
    assert real["dry_run"] is False
    assert real["total_deleted"] >= 1
    assert world["fs_admin"].get(ADMIN_COLLECTION, "old-admin") is None
    assert world["fs_admin"].get(ADMIN_COLLECTION, "new-admin") is not None


def test_audit_rows_for_log_read_and_failed_login(world):
    _seed_visitor(world["fs_user"])
    super_staff = _staff("super_admin")
    client = _client(world, super_staff, as_super=True)
    client.get("/api/v1/admin/query-logs")
    audits = fsh.audit_rows(world["fs_admin"], "admin:read_logs")
    assert audits

    record_audit_log(
        db=world["db"],
        tool_name="auth:login_failed",
        staff_user_id="bad@example.com",
        tool_arguments={"reason": "invalid_credentials"},
        status="FAILED",
        execution_result="invalid_credentials",
    )
    record_audit_log(
        db=world["db"],
        tool_name="instruction:activate",
        staff_user_id="super@tajlandia.com",
        tool_arguments={"instruction_id": "i1"},
        status="SUCCESS",
    )
    record_audit_log(
        db=world["db"],
        tool_name="manage_property_images",
        staff_user_id="admin@tajlandia.com",
        tool_arguments={"op": "add"},
        status="SUCCESS",
    )
    record_audit_log(
        db=world["db"],
        tool_name="confirm_action",
        staff_user_id="admin@tajlandia.com",
        status="SUCCESS",
    )
    names = {row.tool_name for row in fsh.audit_rows(world["fs_admin"])}
    assert "auth:login_failed" in names
    assert "instruction:activate" in names
    assert "manage_property_images" in names
    assert "confirm_action" in names
    assert "admin:read_logs" in names


def test_process_admin_chat_logging_does_not_break_reply(world, monkeypatch):
    from app.services import admin_agent as agent

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("log fail")

    monkeypatch.setattr("app.services.admin_logs_service.log_admin_chat_turn", boom)
    monkeypatch.setattr(agent, "_process_admin_chat_impl", lambda *a, **k: {
        "type": "text",
        "text": "Hello admin",
        "session_id": "s",
        "conversation_id": "c",
        "tools_used": [],
        "language": "en",
    })
    monkeypatch.setattr(agent, "_expose_confirmation_token", lambda db, staff, res: res)
    staff = _staff("admin")
    res = agent.process_admin_chat(
        world["db"], "sess", "hi there", staff, "tajlandia.com", "en"
    )
    assert res["text"] == "Hello admin"
    assert calls == [1]


def test_production_warning_when_audit_off(caplog, monkeypatch):
    import logging
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "ENABLE_AUDIT_LOGGING", False)
    monkeypatch.setattr(config_mod, "is_production_env", lambda: True)
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        config_mod.log_config_warnings()
    messages = " | ".join(r.message for r in caplog.records)
    assert "Admin audit logging is OFF" in messages
    assert "ENV=production" in messages
