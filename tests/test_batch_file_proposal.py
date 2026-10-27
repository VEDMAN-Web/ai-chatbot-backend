"""POST /admin/properties/batch-file stages the change; nothing is written until the uploader confirms."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.core.security import hash_password
from app.core.tenant import ALL_CANONICAL_WEBSITES
from app.db.models import AuditLog, PendingAction, Property, User
from app.db.session import SessionLocal

client = TestClient(app)
PREFIX = "bf_test_"
PASSWORD = "BfTestPass123!"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    for target in ("app.services.firestore_auth_service.get_firestore_client",
                   "app.services.firestore_service.get_firestore_client",
                   "app.services.admin_batch_service.get_firestore_client"):
        monkeypatch.setattr(target, lambda *a, **kw: None)
    yield
    db = SessionLocal()
    try:
        db.query(PendingAction).filter(PendingAction.staff_user_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.staff_user_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
        db.query(Property).filter(Property.property_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
        for user in db.query(User).filter(User.email.like(f"{PREFIX}%")).all():
            db.delete(user)
        db.commit()
    finally:
        db.close()


def _user(role: str) -> dict:
    email = f"{PREFIX}{role}_{uuid.uuid4().hex[:6]}@tajlandia.com"
    db = SessionLocal()
    try:
        db.add(User(email=email, name=role, role=role, password_hash=hash_password(PASSWORD),
                    is_active=True, allowed_websites=list(ALL_CANONICAL_WEBSITES)))
        db.commit()
    finally:
        db.close()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"email": email, "headers": {"Authorization": f"Bearer {token}"}}


def _property(suffix: str) -> str:
    pid = f"{PREFIX}{suffix}"
    db = SessionLocal()
    try:
        db.add(Property(property_id=pid, property_name=f"Test {suffix}", status="Available", is_public=True))
        db.commit()
    finally:
        db.close()
    return pid


def _csv(*rows: str) -> bytes:
    return ("property_id,name,status\n" + "\n".join(rows) + "\n").encode()


def _post(headers, content: bytes, action="update", name="batch.csv"):
    return client.post("/api/v1/admin/properties/batch-file", headers=headers,
                       files={"file": (name, content, "text/csv")}, data={"action": action})


@pytest.fixture
def writes(monkeypatch):
    """Any real bulk write is a test failure on the propose step; confirm swaps in a recorder."""
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"operation_id": "batch_fake", "status": "SUCCESS", "affected_count": len(kwargs["records"])}

    monkeypatch.setattr("app.services.admin_batch_service.execute_batch_property_operation", fake)
    return calls


def test_upload_is_staged_not_applied(writes):
    admin = _user("admin")
    pid = _property("one")
    resp = _post(admin["headers"], _csv(f"{pid},Test one,Sold"))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "PROPOSED" and body["requires_confirmation"] is True
    assert writes == []
    db = SessionLocal()
    try:
        row = db.query(PendingAction).filter(PendingAction.id == uuid.UUID(body["pending_action_id"])).first()
        assert row.status == "PENDING" and row.staff_user_id == admin["email"]
        assert row.tool_name == "execute_batch_property_action"
    finally:
        db.close()


def test_only_the_uploader_can_confirm_and_only_previewed_rows_apply(writes):
    admin, other = _user("admin"), _user("super_admin")
    pid = _property("two")
    pending_id = _post(admin["headers"], _csv(f"{pid},Test two,Sold")).json()["pending_action_id"]
    assert client.post(f"/api/v1/admin/actions/{pending_id}/confirm", headers=other["headers"]).status_code == 403
    assert writes == []
    done = client.post(f"/api/v1/admin/actions/{pending_id}/confirm", headers=admin["headers"])
    assert done.status_code == 200, done.text
    assert len(writes) == 1 and writes[0]["confirmed_by_staff"] is True
    assert [r["property_id"] for r in writes[0]["records"]] == [pid]


def test_workers_cannot_upload(writes):
    assert _post(_user("worker")["headers"], _csv("x,y,z")).status_code == 403


def test_invalid_action_empty_and_oversized_files_are_refused(writes):
    admin = _user("admin")
    assert _post(admin["headers"], _csv("a,b,c"), action="purge").status_code == 400
    assert _post(admin["headers"], b"").status_code == 400
    assert _post(admin["headers"], b"x" * (5 * 1024 * 1024 + 10)).status_code == 413


def test_row_cap(writes):
    admin = _user("admin")
    rows = [f"{PREFIX}r{i},Row {i},Sold" for i in range(501)]
    resp = _post(admin["headers"], _csv(*rows))
    assert resp.status_code == 400 and "limited to 500" in resp.json()["detail"]
    assert writes == []
