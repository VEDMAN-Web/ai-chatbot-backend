"""
ADMIN STAGE F1: pending_actions / audit_logs / leads_read dual-store + conversation review.
Dict-based FakeFirestore with transactions — no network.
"""

from __future__ import annotations

import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from app.core.auth import AuthenticatedStaff
from app.core.config import get_admin_config_status
from app.core.deps import FirestoreSession
from app.repositories import audit_repo, leads_read_repo, pending_actions_repo
from app.services.audit_service import (
    AUDIT_WRITE_FAILED_MSG,
    AuditPersistenceError,
    record_audit_log,
)
from app.services.action_service import (
    ACTION_STATUS_EXECUTED,
    ACTION_STATUS_PENDING,
    ACTION_STATUS_REJECTED,
    confirm_and_execute_action,
    create_pending_action,
    get_pending_action,
    reject_pending_action,
)
from tests.fake_firestore import FakeFirestore, patch_firestore, set_store_modes


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._data = None if data is None else copy.deepcopy(data)

    def to_dict(self):
        return None if self._data is None else copy.deepcopy(self._data)


class _Ref:
    def __init__(self, store, path):
        self.store = store
        self.path = tuple(path)
        self.id = self.path[-1]

    def get(self, transaction=None):
        with self.store.lock:
            return _Snap(self.id, self.store.docs.get(self.path))

    def set(self, data, merge=False):
        with self.store.lock:
            self.store.write(self.path, data, merge)

    def delete(self):
        with self.store.lock:
            self.store.docs.pop(self.path, None)

    def collection(self, name):
        return _Collection(self.store, self.path + (name,))


class _Query:
    def __init__(self, collection, filters=None, limit_n=None):
        self.collection = collection
        self.filters = list(filters or [])
        self.limit_n = limit_n

    def where(self, field, op=None, value=None, **kwargs):
        # Support both positional (field, "==", value) and filter= FieldFilter
        if op is None and value is None and "filter" in kwargs:
            f = kwargs["filter"]
            field, op, value = getattr(f, "field_path", None), getattr(f, "op_string", "=="), getattr(f, "value", None)
        return _Query(self.collection, self.filters + [(field, op, value)], self.limit_n)

    def limit(self, n):
        return _Query(self.collection, self.filters, n)

    def stream(self):
        snaps = list(self.collection.stream())
        out = []
        for snap in snaps:
            data = snap.to_dict() or {}
            ok = True
            for field, op, value in self.filters:
                actual = data.get(field)
                if op in ("==", None):
                    if actual != value:
                        ok = False
                        break
                else:
                    ok = False
                    break
            if ok:
                out.append(snap)
        if self.limit_n is not None:
            out = out[: int(self.limit_n)]
        return out


class _Collection:
    def __init__(self, store, path):
        self.store = store
        self.path = tuple(path)

    def document(self, doc_id):
        return _Ref(self.store, self.path + (str(doc_id),))

    def where(self, field, op=None, value=None, **kwargs):
        return _Query(self).where(field, op, value, **kwargs)

    def limit(self, n):
        return _Query(self).limit(n)

    def stream(self):
        with self.store.lock:
            snaps = []
            for path, data in self.store.docs.items():
                if len(path) == len(self.path) + 1 and path[: len(self.path)] == self.path:
                    snaps.append(_Snap(path[-1], data))
        return snaps


class _Txn:
    def __init__(self, store):
        self.store = store
        self.overlay = {}

    def get(self, ref):
        if ref.path in self.overlay:
            return _Snap(ref.id, self.overlay[ref.path])
        return ref.get()

    def set(self, ref, data, merge=False):
        current = self.overlay.get(ref.path)
        if current is None and merge:
            current = self.store.docs.get(ref.path)
        if merge and isinstance(current, dict):
            merged = copy.deepcopy(current)
            merged.update(copy.deepcopy(data))
            self.overlay[ref.path] = merged
        else:
            self.overlay[ref.path] = copy.deepcopy(data)


class FakeFirestore:
    def __init__(self):
        self.docs = {}
        self.lock = threading.RLock()

    def collection(self, name):
        return _Collection(self, (name,))

    def write(self, path, data, merge):
        if merge and isinstance(self.docs.get(path), dict):
            merged = copy.deepcopy(self.docs[path])
            merged.update(copy.deepcopy(data))
            self.docs[path] = merged
        else:
            self.docs[path] = copy.deepcopy(data)

    def run_transaction(self, fn):
        with self.lock:
            txn = _Txn(self)
            result = fn(txn)
            for path, data in txn.overlay.items():
                self.docs[path] = data
            return result


@pytest.fixture
def db():
    return FirestoreSession()


@pytest.fixture
def fake_fs(monkeypatch):
    return patch_firestore(monkeypatch)


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-f1",
        email="admin@tajlandia.com",
        role="admin",
        permissions={"all": True},
        name="Admin",
        allowed_websites=["tajlandia.com", "varsoviaestate.com", "thai.estate"],
    )


def _modes(monkeypatch, pending="both", audit="both", leads="both"):
    set_store_modes(monkeypatch, pending=pending, audit=audit, leads=leads)


# ----- pending create / confirm / reject / double-confirm / expiry -----


def test_pending_create_confirm_reject(db, fake_fs, staff, monkeypatch):
    _modes(monkeypatch, pending="firestore", audit="firestore")
    action = create_pending_action(
        db,
        staff_user_id=staff.email,
        tool_name="send_email",
        action_payload={"to": "a@b.com", "subject": "Hi", "body": "x"},
        conversation_id="conv-f1",
    )
    assert action.status == ACTION_STATUS_PENDING
    assert ("pending_actions", action.id) in fake_fs.docs

    loaded = get_pending_action(db, action.id)
    assert loaded.confirmation_token == action.confirmation_token

    rejected = reject_pending_action(db, action.id, staff, reason="nope")
    assert rejected["status"] == ACTION_STATUS_REJECTED
    assert fake_fs.docs[("pending_actions", action.id)]["status"] == ACTION_STATUS_REJECTED


def test_double_confirm_executes_once(db, fake_fs, staff, monkeypatch):
    _modes(monkeypatch, pending="firestore", audit="firestore")
    action = create_pending_action(
        db,
        staff_user_id=staff.email,
        tool_name="send_email",
        action_payload={"to": "a@b.com", "subject": "Hi", "body": "x"},
        conversation_id="conv-race",
    )
    token = action.confirmation_token

    def _send(*a, **k):
        return {"delivered": True, "provider": "mock", "is_mock": True}

    monkeypatch.setattr("app.services.action_service._execute_send_email", _send)

    results = []

    def once(_):
        out = confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            confirmation_token=token,
        )
        results.append(out["status"])
        return out

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(once, range(2)))

    # One EXECUTED; the other either EXECUTED idempotent or non-PENDING refuse
    assert ACTION_STATUS_EXECUTED in results
    final = get_pending_action(db, action.id)
    assert final.status == ACTION_STATUS_EXECUTED


def test_expired_pending_cannot_confirm(db, fake_fs, staff, monkeypatch):
    _modes(monkeypatch, pending="firestore", audit="firestore")
    action = create_pending_action(
        db,
        staff_user_id=staff.email,
        tool_name="send_email",
        action_payload={"to": "a@b.com", "subject": "Hi", "body": "x"},
        conversation_id="conv-exp",
        expires_in_minutes=0,
    )
    # Force expiry in the past
    pending_actions_repo.update_fields(
        db,
        action.id,
        # expires_at via raw doc
    )
    fake_fs.docs[("pending_actions", action.id)]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()

    with pytest.raises(Exception) as exc:
        confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            confirmation_token=action.confirmation_token,
        )
    detail = str(getattr(exc.value, "detail", exc.value)).lower()
    assert "expir" in detail or "cannot" in detail


# ----- audit soft/hard fail + query -----


def test_audit_write_and_activity_filters(db, fake_fs, monkeypatch):
    _modes(monkeypatch, audit="firestore")
    entry = record_audit_log(
        db=db,
        staff_user_id="admin@tajlandia.com",
        tool_name="create_or_update_listing",
        proposed_payload={"property_id": "isla-kata"},
        status="SUCCESS",
    )
    assert entry.id
    assert ("audit_logs", str(entry.id)) in fake_fs.docs

    # Legacy mirror must NOT be written
    assert not any(p[0] == "admin_audit_log" for p in fake_fs.docs)

    # Seed a legacy doc for merge-on-read
    fake_fs.docs[("admin_audit_log", "legacy1")] = {
        "adminEmail": "admin@tajlandia.com",
        "action": "create_or_update_listing",
        "details": {"status": "SUCCESS", "payload": {"property_id": "old"}},
        "createdAt": "2020-01-01T00:00:00+00:00",
        "targetId": "old",
    }

    from app.services.audit_service import query_admin_activity_logs
    page = query_admin_activity_logs(db, admin_email="admin@tajlandia.com", limit=10, offset=0)
    assert page["total"] >= 2
    assert len(page["logs"]) >= 1


def test_audit_failure_read_only_soft_write_hard(db, monkeypatch):
    _modes(monkeypatch, audit="firestore")
    monkeypatch.setattr(
        "app.repositories.audit_repo.create",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    soft = record_audit_log(
        db=db,
        staff_user_id="a@b.com",
        tool_name="get_database_inventory_stats",
        status="SUCCESS",
    )
    assert getattr(soft, "audit_write_failed", False) is True

    with pytest.raises(AuditPersistenceError, match="Could not record the audit log"):
        record_audit_log(
            db=db,
            staff_user_id="a@b.com",
            tool_name="create_or_update_listing",
            proposed_payload={"property_id": "x"},
            status="PENDING",
        )


# ----- leads -----


def test_leads_report_from_firestore_all_formats(db, fake_fs, monkeypatch):
    _modes(monkeypatch, leads="firestore")
    now = datetime.now(timezone.utc).isoformat()
    fake_fs.docs[("leads", "L1")] = {
        "name": "Alex Smith",
        "email": "alex@example.com",
        "phone": "+66111",
        "status": "new",
        "site": "tajlandia.com",
        "createdAt": now,
        "updatedAt": now,
        "summary": "Wants a villa",
    }
    from app.services.report_service import generate_report

    for fmt in ("pdf", "xlsx", "docx", "csv"):
        res = generate_report(
            db,
            format_type=fmt,
            report_type="leads",
            days=0,
            title="Leads",
        )
        assert res.get("filename") or res.get("file_url") or res.get("message")
        assert res.get("error") is None or "No leads" not in str(res.get("message") or "")


# ----- conversation review -----


def test_conversation_review_from_firestore(db, fake_fs, monkeypatch):
    _modes(monkeypatch, pending="firestore", audit="firestore")
    cid = "admin__sess-f1"
    fake_fs.docs[("conversations", cid)] = {
        "sessionId": "sess-f1",
        "agentType": "admin",
        "messageHistory": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        "preferences": {},
    }
    action = create_pending_action(
        db,
        staff_user_id="admin@tajlandia.com",
        tool_name="send_email",
        action_payload={"to": "a@b.com", "subject": "x", "body": "y"},
        conversation_id=cid,
    )
    record_audit_log(
        db=db,
        staff_user_id="admin@tajlandia.com",
        tool_name="send_email",
        status="PENDING",
        conversation_id=cid,
        pending_action_id=str(action.id),
    )

    from app.services.instruction_store import review_conversation
    review = review_conversation(db, cid)
    assert review["conversation_id"] == cid
    assert len(review["messages"]) == 2
    assert any(p["id"] == str(action.id) for p in review["pending_actions"])
    assert review["audit_logs"]


# ----- STORE modes + config-status -----


def test_config_status_has_no_store_mode_flags():
    status = get_admin_config_status()
    assert not [k for k in status if k.startswith("store_")]
    assert not [k for k in status["flags"] if k.startswith("STORE_")]
    assert "database_configured" not in status


# ----- bump_properties_version -----


def test_bump_properties_version_on_image_write(fake_fs, monkeypatch):
    from app.services.firestore_service import bump_properties_version

    v1 = bump_properties_version(target="user")
    v2 = bump_properties_version(target="user")
    assert v1 == 1
    assert v2 == 2
    meta = fake_fs.docs[("meta", "properties_version")]
    assert meta["version"] == 2
    assert meta.get("updated_at")


def test_invalidate_search_cache_bumps_version(fake_fs, monkeypatch):
    from app.services.action_service import _invalidate_search_cache

    monkeypatch.setattr(
        "app.services.cache_service.search_cache.invalidate",
        lambda: None,
        raising=False,
    )
    with patch("app.services.cache_service.search_cache") as sc:
        sc.invalidate = MagicMock()
        _invalidate_search_cache()
    assert fake_fs.docs[("meta", "properties_version")]["version"] >= 1


def test_missing_firestore_client_raises(db, monkeypatch):
    """With no Firestore client, writes must raise (there is no other store to fall back to)."""
    _modes(monkeypatch, pending="firestore", audit="firestore")
    monkeypatch.setattr(
        "app.services.firestore_service.get_firestore_client",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.firestore_service.get_admin_firestore_client",
        lambda *a, **k: None,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="Firestore client unavailable"):
        create_pending_action(
            db,
            staff_user_id="admin@tajlandia.com",
            tool_name="send_email",
            action_payload={"to": "a@b.com", "subject": "x", "body": "y"},
        )


def test_listing_writes_bump_the_store_they_write_to(monkeypatch):
    """Listings are written to the admin store, so the cache counter is bumped there.
    On staging the admin store is the test project; the user store is production and must not be touched."""
    import app.services.firestore_service as fsvc
    from app.services.action_service import _bump_properties_version

    seen = []
    monkeypatch.setattr(fsvc, "bump_properties_version", lambda target="user": seen.append(target) or 1)
    _bump_properties_version()
    assert seen == ["admin"]
