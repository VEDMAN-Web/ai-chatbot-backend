"""Staff authentication and registration access rules (header, dev-token, shared-secret, register)."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings, DEFAULT_INTERNAL_API_SECRET
from app.repositories import users_repo
from tests import fs_users

client = TestClient(app)
PREFIX = "sec_test_"
PASSWORD = "SecTestPass123!"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    yield


def _make_user(role: str) -> str:
    return fs_users.make_user(client, role, prefix=PREFIX, password=PASSWORD)["email"]


def _token(email: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _new_account(role: str = "worker", **extra) -> dict:
    return {"email": f"{PREFIX}new_{uuid.uuid4().hex[:6]}@tajlandia.com", "password": "NewAccount123!", "name": "New", "role": role, **extra}


# ---- authentication: only a real session token authenticates staff -------------------------------

def test_header_only_login_is_rejected():
    email = _make_user("super_admin")
    resp = client.get("/api/v1/auth/me", headers={"X-Staff-Email": email})
    assert resp.status_code == 401


def test_dev_token_is_rejected():
    email = _make_user("super_admin")
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer dev-token-{email}"})
    assert resp.status_code == 401


def test_shared_internal_secret_is_not_a_login(monkeypatch):
    _make_user("super_admin")
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", DEFAULT_INTERNAL_API_SECRET)
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {DEFAULT_INTERNAL_API_SECRET}"})
    assert resp.status_code == 401
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", "a-long-random-service-secret-value")
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer a-long-random-service-secret-value"})
    assert resp.status_code == 401


def test_real_session_token_still_works():
    email = _make_user("worker")
    resp = client.get("/api/v1/auth/me", headers=_token(email))
    assert resp.status_code == 200
    assert resp.json()["email"] == email


# ---- registration access rule --------------------------------------------------------------------

def test_register_without_credentials_is_forbidden():
    _make_user("super_admin")  # at least one account exists, so first-run onboarding does not apply
    body = _new_account("super_admin")
    resp = client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 403
    assert users_repo.get_by_email(None, body["email"]) is None


def test_register_with_worker_session_is_forbidden():
    headers = _token(_make_user("worker"))
    resp = client.post("/api/v1/auth/register", headers=headers, json=_new_account("worker"))
    assert resp.status_code == 403


def test_register_with_admin_session_is_forbidden():
    headers = _token(_make_user("admin"))
    resp = client.post("/api/v1/auth/register", headers=headers, json=_new_account("worker"))
    assert resp.status_code == 403


def test_register_with_super_admin_session_creates_the_requested_role():
    headers = _token(_make_user("super_admin"))
    body = _new_account("worker")
    resp = client.post("/api/v1/auth/register", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "worker"


def test_register_existing_email_is_a_conflict_and_changes_nothing():
    headers = _token(_make_user("super_admin"))
    victim = _make_user("super_admin")
    resp = client.post("/api/v1/auth/register", headers=headers, json={
        "email": victim, "password": "TakeOver12345!", "name": "x", "role": "worker"})
    assert resp.status_code == 409
    assert users_repo.get_by_email(None, victim).role == "super_admin"
    assert client.post("/api/v1/auth/login", json={"email": victim, "password": PASSWORD}).status_code == 200


def test_register_unknown_role_is_rejected_not_promoted():
    headers = _token(_make_user("super_admin"))
    resp = client.post("/api/v1/auth/register", headers=headers, json=_new_account("root"))
    assert resp.status_code == 400


def test_register_with_valid_admin_secret(monkeypatch):
    _make_user("super_admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "configured-admin-secret-1")
    ok = client.post("/api/v1/auth/register", json=_new_account("worker", admin_secret="configured-admin-secret-1"))
    assert ok.status_code == 201, ok.text
    bad = client.post("/api/v1/auth/register", json=_new_account("worker", admin_secret="wrong-secret"))
    assert bad.status_code == 403


def test_default_internal_secret_never_authorizes_register(monkeypatch):
    _make_user("super_admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", DEFAULT_INTERNAL_API_SECRET)
    resp = client.post("/api/v1/auth/register", json=_new_account("worker", admin_secret=DEFAULT_INTERNAL_API_SECRET))
    assert resp.status_code == 403


def test_strong_internal_secret_authorizes_register(monkeypatch):
    _make_user("super_admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", "a-long-random-service-secret-value")
    resp = client.post("/api/v1/auth/register", json=_new_account("worker", admin_secret="a-long-random-service-secret-value"))
    assert resp.status_code == 201, resp.text
