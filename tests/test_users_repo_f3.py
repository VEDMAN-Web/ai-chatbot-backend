"""
Stage F3: staff users + sessions live in Firestore (admin_authentication / admin_sessions).
Also covers the auth hardening that shipped with it (open /register, dev-token bypass, plain-text passwords).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.main import app
from app.repositories import users_repo
from tests.fake_firestore import FakeFirestore, docs_in_collection, patch_firestore


@pytest.fixture
def fs(monkeypatch):
    fake = patch_firestore(monkeypatch, FakeFirestore())
    monkeypatch.setattr(settings, "STORE_USERS", "firestore")
    monkeypatch.setattr(settings, "STORE_AUDIT", "firestore", raising=False)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr(settings, "ENV", "test", raising=False)
    monkeypatch.setattr(settings, "ALLOW_INSECURE_DEV_AUTH", False)
    return fake


@pytest.fixture
def client(fs):
    return TestClient(app, raise_server_exceptions=False)


def _make_user(email="boss@tajlandia.com", role="super_admin", password="Secret#123", **extra):
    fields = dict(
        email=email,
        name=email.split("@")[0],
        role=role,
        domain="tajlandia.com",
        allowed_websites=["tajlandia.com"],
        password_hash=hash_password(password),
        is_active=True,
        status="active",
        preferences={},
    )
    fields.update(extra)
    return users_repo.create_user(None, **fields)


def _login(client, email="boss@tajlandia.com", password="Secret#123"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

def test_user_crud_and_filters(fs):
    a = _make_user("a@tajlandia.com", role="admin")
    _make_user("w1@tajlandia.com", role="worker")
    _make_user("w2@tajlandia.com", role="worker", is_active=False, status="blocked")

    assert users_repo.get_by_email(None, "A@Tajlandia.com").id == a.id
    assert users_repo.get_by_id(None, a.id).email == "a@tajlandia.com"
    assert users_repo.get_by_id(None, "a@tajlandia.com").id == a.id

    assert {u.email for u in users_repo.list_users(None, roles=["worker"])} == {"w1@tajlandia.com", "w2@tajlandia.com"}
    assert [u.email for u in users_repo.list_users(None, is_active=False)] == ["w2@tajlandia.com"]
    assert [u.email for u in users_repo.list_users(None, status="blocked")] == ["w2@tajlandia.com"]
    assert [u.email for u in users_repo.list_users(None, search="W1")] == ["w1@tajlandia.com"]

    with pytest.raises(ValueError):
        _make_user("a@tajlandia.com")

    a.name = "Renamed"
    users_repo.save_user(None, a)
    assert users_repo.get_by_email(None, "a@tajlandia.com").name == "Renamed"
    assert users_repo.any_user_exists(None)


def test_sessions_store_only_a_token_hash(fs):
    user = _make_user()
    token = "tok-abcdefghijklmnopqrstuvwxyz"
    users_repo.create_session(None, user.id, token, datetime.utcnow() + timedelta(hours=1))

    docs = docs_in_collection(fs, "admin_sessions")
    assert len(docs) == 1
    assert token not in str(docs[0])
    assert docs[0]["tokenHash"] == users_repo.hash_token(token)

    sess = users_repo.get_session_by_token(None, token)
    assert sess and sess.user_id == user.id and sess.revoked_at is None
    assert users_repo.count_active_sessions(None, user.id) == 1

    assert users_repo.revoke_user_sessions(None, user.id) == 1
    assert users_repo.get_session_by_token(None, token).revoked_at is not None
    assert users_repo.count_active_sessions(None, user.id) == 0

    users_repo.delete_user(None, user)
    assert users_repo.get_by_email(None, user.email) is None
    assert docs_in_collection(fs, "admin_sessions") == []


def test_firestore_mode_without_client_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "STORE_USERS", "firestore")
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        users_repo.get_by_email(None, "x@tajlandia.com")


def test_login_returns_503_when_store_is_down(monkeypatch):
    monkeypatch.setattr(settings, "STORE_USERS", "firestore")
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda *a, **k: None)
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/auth/login", json={"email": "x@tajlandia.com", "password": "nope-nope"}
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Login, sessions, block
# ---------------------------------------------------------------------------

def test_login_me_logout_flow(client, fs):
    _make_user()
    bad = _login(client, password="wrong-password")
    assert bad.status_code == 401

    ok = _login(client)
    assert ok.status_code == 200, ok.text
    token = ok.json()["access_token"]

    me = client.get("/api/v1/auth/me", headers=_bearer(token))
    assert me.status_code == 200 and me.json()["email"] == "boss@tajlandia.com"

    out = client.post("/api/v1/auth/logout", headers=_bearer(token))
    assert out.status_code == 200
    assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401


def test_blocked_user_cannot_login_and_live_sessions_die(client, fs):
    _make_user()
    _make_user("worker@tajlandia.com", role="worker", password="Worker#123")
    boss = _login(client).json()["access_token"]
    worker = _login(client, "worker@tajlandia.com", "Worker#123").json()["access_token"]
    assert client.get("/api/v1/auth/me", headers=_bearer(worker)).status_code == 200

    wid = users_repo.get_by_email(None, "worker@tajlandia.com").id
    r = client.post(f"/api/v1/auth/admin/users/{wid}/block", json={"blocked": True, "reason": "test"}, headers=_bearer(boss))
    assert r.status_code == 200, r.text

    assert client.get("/api/v1/auth/me", headers=_bearer(worker)).status_code in (401, 403)
    assert _login(client, "worker@tajlandia.com", "Worker#123").status_code == 403

    r = client.post(f"/api/v1/auth/admin/users/{wid}/block", json={"blocked": False}, headers=_bearer(boss))
    assert r.status_code == 200
    assert _login(client, "worker@tajlandia.com", "Worker#123").status_code == 200


def test_legacy_plaintext_password_is_upgraded_once(client, fs):
    fs.collection("admin_authentication").document("old@tajlandia.com").set({
        "email": "old@tajlandia.com", "password": "ConsolePass1", "role": "admin",
        "isActive": True, "domain": "tajlandia.com", "allowed_websites": ["tajlandia.com"],
    })
    assert _login(client, "old@tajlandia.com", "WrongPass99").status_code == 401
    assert _login(client, "old@tajlandia.com", "ConsolePass1").status_code == 200

    doc = fs.collection("admin_authentication").document("old@tajlandia.com").get().to_dict()
    assert "password" not in doc
    assert verify_password("ConsolePass1", doc["passwordHash"])
    # A stored hash is never accepted as the password itself.
    assert _login(client, "old@tajlandia.com", doc["passwordHash"]).status_code == 401


def test_admin_created_user_never_stores_plaintext(client, fs):
    _make_user()
    token = _login(client).json()["access_token"]
    r = client.post(
        "/api/v1/auth/admin/users",
        json={"email": "new@tajlandia.com", "password": "Brand#New1", "role": "worker", "name": "New"},
        headers=_bearer(token),
    )
    assert r.status_code == 201, r.text
    doc = fs.collection("admin_authentication").document("new@tajlandia.com").get().to_dict()
    assert "password" not in doc and doc["passwordHash"]
    assert _login(client, "new@tajlandia.com", "Brand#New1").status_code == 200


# ---------------------------------------------------------------------------
# /auth/register must not be open
# ---------------------------------------------------------------------------

def test_register_requires_authorisation_once_users_exist(client, fs):
    _make_user()
    r = client.post("/api/v1/auth/register", json={"email": "evil@x.com", "password": "hacked99", "role": "super_admin"})
    assert r.status_code == 403
    assert users_repo.get_by_email(None, "evil@x.com") is None


def test_register_cannot_take_over_existing_account(client, fs):
    _make_user("victim@tajlandia.com", role="worker", password="RealPass#1")
    boss = _make_user()
    token = _login(client).json()["access_token"]
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "victim@tajlandia.com", "password": "hacked99", "role": "super_admin"},
        headers=_bearer(token),
    )
    assert r.status_code == 409
    victim = users_repo.get_by_email(None, "victim@tajlandia.com")
    assert victim.role == "worker" and verify_password("RealPass#1", victim.password_hash)
    assert boss is not None


def test_register_first_account_then_closed(client, fs):
    r = client.post("/api/v1/auth/register", json={"email": "first@tajlandia.com", "password": "First#Pass1", "role": "super_admin"})
    assert r.status_code == 201, r.text
    r2 = client.post("/api/v1/auth/register", json={"email": "second@tajlandia.com", "password": "Second#Pass1", "role": "worker"})
    assert r2.status_code == 403


def test_register_worker_session_is_refused_and_unknown_role_rejected(client, fs):
    _make_user()
    _make_user("w@tajlandia.com", role="worker", password="Worker#123")
    wt = _login(client, "w@tajlandia.com", "Worker#123").json()["access_token"]
    r = client.post("/api/v1/auth/register", json={"email": "z@x.com", "password": "Zzzz#1234", "role": "worker"}, headers=_bearer(wt))
    assert r.status_code == 403

    bt = _login(client).json()["access_token"]
    r = client.post("/api/v1/auth/register", json={"email": "z@x.com", "password": "Zzzz#1234", "role": "overlord"}, headers=_bearer(bt))
    assert r.status_code == 400


def test_register_with_strong_admin_secret(client, fs, monkeypatch):
    _make_user()
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", "a-long-random-internal-secret-value")
    bad = client.post("/api/v1/auth/register", json={"email": "s@x.com", "password": "Passw0rd#", "role": "worker", "adminSecret": "guess"})
    assert bad.status_code == 403
    ok = client.post("/api/v1/auth/register", json={"email": "s@x.com", "password": "Passw0rd#", "role": "worker", "adminSecret": "a-long-random-internal-secret-value"})
    assert ok.status_code == 201, ok.text


def test_default_internal_secret_is_not_accepted(client, fs, monkeypatch):
    _make_user()
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", "tajlandia-internal-secret-token")
    r = client.post("/api/v1/auth/register", json={
        "email": "s@x.com", "password": "Passw0rd#", "role": "worker", "adminSecret": "tajlandia-internal-secret-token",
    })
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# dev-token / X-Staff-Email bypass
# ---------------------------------------------------------------------------

def test_staff_email_header_and_dev_token_are_refused_by_default(client, fs):
    _make_user()
    assert client.get("/api/v1/auth/me", headers={"X-Staff-Email": "boss@tajlandia.com"}).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_bearer("dev-token-boss@tajlandia.com")).status_code == 401


def test_dev_auth_flag_never_works_in_production(client, fs, monkeypatch):
    _make_user()
    monkeypatch.setattr(settings, "ALLOW_INSECURE_DEV_AUTH", True)
    assert client.get("/api/v1/auth/me", headers={"X-Staff-Email": "boss@tajlandia.com"}).status_code == 200
    monkeypatch.setattr(settings, "ENV", "production")
    assert client.get("/api/v1/auth/me", headers={"X-Staff-Email": "boss@tajlandia.com"}).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_bearer("dev-token-boss@tajlandia.com")).status_code == 401
