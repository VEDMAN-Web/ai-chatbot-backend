"""Audit trail end to end: who/what/when is recorded, and no credentials or message content reach the stored rows."""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.core.security import hash_password
from app.core.tenant import ALL_CANONICAL_WEBSITES
from app.db.models import AuditLog, PendingAction, User
from app.db.session import SessionLocal
from app.services.action_service import create_pending_action
from app.services.audit_service import sanitize_payload, scrub_text

client = TestClient(app)
PREFIX = "audit_test_"
PASSWORD = "AuditTestPass123!"

BODY_TEXT = "Dear Anna Kowalska, your passport number is X1234567 and your IBAN is PL61109010140000071219812874."
BEARER = "ya29.LEAKEDACCESSTOKEN0123456789"
URL_TOKEN = "LEAKEDURLTOKENVALUE"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr("app.services.firestore_auth_service.get_firestore_client", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda *a, **kw: None)
    yield
    db = SessionLocal()
    try:
        db.query(AuditLog).filter(AuditLog.staff_user_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.tool_name == "auth:register", AuditLog.proposed_payload["email"].astext.like(f"{PREFIX}%")).delete(synchronize_session=False)
        db.query(PendingAction).filter(PendingAction.staff_user_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
        for user in db.query(User).filter(User.email.like(f"{PREFIX}%")).all():
            db.delete(user)
        db.commit()
    finally:
        db.close()


def _user(role="admin") -> dict:
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


def _stage_email(owner_email: str) -> str:
    db = SessionLocal()
    try:
        action = create_pending_action(
            db=db, staff_user_id=owner_email, tool_name="send_email",
            action_payload={"to": "anna@example.com", "subject": "Your offer", "body": BODY_TEXT,
                            "html_body": f"<p>{BODY_TEXT}</p>", "api_key": "sk-live-PLANTEDAPIKEY12345"},
        )
        return str(action.id)
    finally:
        db.close()


def _audit_rows(pending_id: str):
    db = SessionLocal()
    try:
        return [
            {"status": r.status, "tool": r.tool_name, "staff": r.staff_user_id, "confirmed": r.confirmed_by_staff,
             "created_at": r.created_at, "payload": r.proposed_payload, "args": r.tool_arguments,
             "result": r.execution_result, "error": r.error_message}
            for r in db.query(AuditLog).filter(AuditLog.pending_action_id == pending_id).order_by(AuditLog.created_at).all()
        ]
    finally:
        db.close()


def _dump(rows) -> str:
    return json.dumps(rows, default=str, ensure_ascii=False)


# ---- the sanitizer itself ----------------------------------------------------------------------

def test_credentials_in_keys_and_in_text_are_removed():
    out = sanitize_payload({
        "api_key": "sk-live-AAAAAAAAAAAAAAAA", "confirmation_token": "act_0123456789abcdef0123456789abcdef",
        "nested": [{"refresh_token": "1//0gREFRESHVALUE"}],
        "note": f"Authorization: Bearer {BEARER}", "url": f"https://x.test/cb?access_token={URL_TOKEN}&ok=1",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturepart", "cookie": "sid=abc",
    })
    text = json.dumps(out)
    for leaked in ("sk-live-AAAA", "act_0123", "1//0gREFRESH", BEARER, URL_TOKEN, "eyJhbGci", "sid=abc"):
        assert leaked not in text, leaked
    assert "ok=1" in text  # harmless query values survive


def test_message_content_is_not_logged_but_its_size_is():
    out = sanitize_payload({"to": "anna@example.com", "subject": "Offer", "body": BODY_TEXT, "html_body": "<p>x</p>"})
    assert out["to"] == "anna@example.com" and out["subject"] == "Offer"
    assert BODY_TEXT not in json.dumps(out) and f"{len(BODY_TEXT)} characters" in out["body"]


def test_usage_metrics_are_kept():
    assert sanitize_payload({"total_tokens": 10, "prompt_tokens": 4}) == {"total_tokens": 10, "prompt_tokens": 4}


def test_scrub_text_leaves_ordinary_text_alone():
    sentence = "Property PHK-001 updated: price 8,500,000 THB; status Available."
    assert scrub_text(sentence) == sentence and scrub_text(None) is None


# ---- a confirmed action ------------------------------------------------------------------------

def test_a_confirmed_email_is_audited_with_who_what_when_and_no_secrets_or_content(monkeypatch):
    owner = _user("admin")
    monkeypatch.setattr(
        "app.services.action_service._execute_send_email",
        lambda payload, staff_user_email=None, db=None: {
            "delivered": True, "success": True, "completed": True, "is_mock": False, "provider": "fake",
            "to": payload["to"], "subject": payload["subject"], "message_id": "m-1",
            "provider_debug": f"sent with Authorization: Bearer {BEARER}", "access_token": "LEAKEDRESULTTOKEN"},
    )
    pending_id = _stage_email(owner["email"])
    resp = client.post(f"/api/v1/admin/actions/{pending_id}/confirm", headers=owner["headers"])
    assert resp.status_code == 200, resp.text

    rows = _audit_rows(pending_id)
    assert [r["status"] for r in rows] == ["PENDING", "SUCCESS"]
    done = rows[-1]
    # who, what, when
    assert done["staff"] == owner["email"] and done["tool"] == "send_email" and done["confirmed"] is True
    assert abs(datetime.utcnow() - done["created_at"].replace(tzinfo=None)) < timedelta(minutes=5)
    assert done["payload"]["to"] == "anna@example.com" and done["payload"]["subject"] == "Your offer"
    # nothing sensitive
    stored = _dump(rows)
    for leaked in ("Dear Anna", "X1234567", "PL61109010140000071219812874", "PLANTEDAPIKEY", BEARER, "LEAKEDRESULTTOKEN"):
        assert leaked not in stored, leaked
    assert resp.json()["audit_log_id"]


def test_a_failed_action_keeps_the_failure_but_not_the_credentials(monkeypatch):
    owner = _user("admin")

    def boom(payload, staff_user_email=None, db=None):
        raise RuntimeError(f"Gmail rejected Authorization: Bearer {BEARER} at https://api.test/send?access_token={URL_TOKEN}")

    monkeypatch.setattr("app.services.action_service._execute_send_email", boom)
    pending_id = _stage_email(owner["email"])
    resp = client.post(f"/api/v1/admin/actions/{pending_id}/confirm", headers=owner["headers"])
    assert resp.status_code == 500
    assert BEARER not in resp.text and URL_TOKEN not in resp.text      # nothing returned to the caller

    rows = _audit_rows(pending_id)
    assert rows[-1]["status"] == "FAILED" and rows[-1]["staff"] == owner["email"]
    assert "Gmail rejected" in rows[-1]["error"]
    assert BEARER not in _dump(rows) and URL_TOKEN not in _dump(rows)
    db = SessionLocal()
    try:
        stored_error = db.query(PendingAction).filter(PendingAction.id == uuid.UUID(pending_id)).first().error_message
        assert "Gmail rejected" in stored_error and BEARER not in stored_error and URL_TOKEN not in stored_error
    finally:
        db.close()


def test_a_rejected_action_is_audited_as_not_confirmed(monkeypatch):
    owner = _user("admin")
    pending_id = _stage_email(owner["email"])
    assert client.post(f"/api/v1/admin/actions/{pending_id}/reject", headers=owner["headers"], json={"reason": "wrong client"}).status_code == 200
    last = _audit_rows(pending_id)[-1]
    assert last["status"] == "REJECTED" and last["confirmed"] is False and last["staff"] == owner["email"]


# ---- account creation --------------------------------------------------------------------------

def test_registration_audit_names_the_actor_and_never_stores_the_password():
    boss = _user("super_admin")
    new_email = f"{PREFIX}new_{uuid.uuid4().hex[:6]}@tajlandia.com"
    secret_pw = "Un1queNewAccountPw!42"
    resp = client.post("/api/v1/auth/register", headers=boss["headers"],
                       json={"email": new_email, "password": secret_pw, "name": "New", "role": "worker"})
    assert resp.status_code == 201, resp.text
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.tool_name == "auth:register", AuditLog.proposed_payload["email"].astext == new_email).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.staff_user_id == boss["email"]                      # the caller, not the new account
        stored = json.dumps([row.tool_arguments, row.proposed_payload, row.execution_result], default=str)
        assert secret_pw not in stored and new_email in stored
    finally:
        db.close()
