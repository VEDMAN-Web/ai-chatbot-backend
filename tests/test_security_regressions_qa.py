"""
Security regressions found in the QA audit (each test reproduces the original attack and asserts it now fails).
In-memory SQLite + fake Firestore; no live services.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import AuthenticatedStaff, get_current_user_from_db
from app.core.config import settings
from app.core.security import hash_password
from app.db.models import AuditLog, User, UserSession
from app.db.session import get_db
from test_admin_rest_crud_golden import FakeFirestore  # tests/ is on sys.path under pytest


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in (User.__table__, UserSession.__table__, AuditLog.__table__):
        table.create(bind=engine, checkfirst=True)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _staff(role="worker", user_id="worker-1"):
    sites = ["tajlandia.com"]
    return AuthenticatedStaff(user_id=user_id, email=f"{user_id}@tajlandia.com", role=role,
                              allowed_websites=sites, selected_websites=sites)


# ----------------------------------------------------------------------------- password reset (C1)


@pytest.fixture
def auth_world(monkeypatch):
    from app.api.v1.auth import router as auth_router

    db = _session()
    fs = FakeFirestore()
    monkeypatch.setattr("app.services.firestore_auth_service.get_firestore_client", lambda: fs)
    sent = []
    monkeypatch.setattr(
        "app.services.google_services.google_manager.send_email",
        lambda **kw: sent.append(kw) or {"success": True, "is_mock": False},
    )
    boss = User(email="boss@tajlandia.com", name="Boss", role="super_admin", password_hash=hash_password("Original#Pass1"),
                is_active=True, status="active", allowed_websites=["tajlandia.com"], domain="tajlandia.com")
    db.add(boss)
    db.commit()
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: (yield db)
    yield {"client": TestClient(app), "db": db, "sent": sent, "fs": fs}
    db.close()


def _token_from_email(sent):
    body = sent[-1]["body"]
    return next(line.strip() for line in body.splitlines() if line.strip().startswith("reset"))


def test_forgot_password_never_returns_the_token_and_does_not_enumerate(auth_world):
    c = auth_world["client"]
    known = c.post("/api/v1/auth/forgot-password", json={"email": "boss@tajlandia.com"})
    unknown = c.post("/api/v1/auth/forgot-password", json={"email": "nobody@tajlandia.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json()["reset_token"] is None and unknown.json()["reset_token"] is None
    assert known.json()["message"] == unknown.json()["message"]
    assert len(auth_world["sent"]) == 1 and auth_world["sent"][0]["to"] == "boss@tajlandia.com"


def test_reset_token_is_single_use(auth_world):
    c = auth_world["client"]
    c.post("/api/v1/auth/forgot-password", json={"email": "boss@tajlandia.com"})
    token = _token_from_email(auth_world["sent"])
    first = c.post("/api/v1/auth/reset-password",
                   json={"email": "boss@tajlandia.com", "reset_token": token, "new_password": "Brand#New#Pass2"})
    assert first.status_code == 200, first.text
    again = c.post("/api/v1/auth/reset-password",
                   json={"email": "boss@tajlandia.com", "reset_token": token, "new_password": "Attacker#Pass3"})
    assert again.status_code == 400


def test_old_unbound_token_format_is_rejected(auth_world):
    import hashlib, hmac, time, urllib.parse
    secret = settings.INTERNAL_API_SECRET or settings.SECRET_KEY or "tajlandia-reset-secret"
    payload = f"reset:boss@tajlandia.com:{int(time.time()) + 600}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    res = auth_world["client"].post("/api/v1/auth/reset-password", json={
        "email": "boss@tajlandia.com", "reset_token": urllib.parse.quote(f"{payload}:{sig}"), "new_password": "Whatever#123"})
    assert res.status_code == 400


# ----------------------------------------------------------------------------- login & user management (H2, M1, M2)


def _add_user(db, email, role, sites, password="Strong#Pass1", status="active"):
    u = User(email=email, name=email, role=role, password_hash=hash_password(password), is_active=status == "active",
             status=status, allowed_websites=list(sites), domain=sites[0])
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _as_staff(app_client_world, user):
    from app.api.v1.auth import router  # noqa: F401  (router already mounted)
    from app.core.auth import resolve_user_allowed_websites
    sites = resolve_user_allowed_websites(user, user.role)
    return AuthenticatedStaff(user_id=str(user.id), email=user.email, role=user.role, domain=user.domain,
                              allowed_websites=sites, selected_websites=sites)


def test_login_cannot_rehome_an_admin_into_another_tenant(auth_world):
    db, c = auth_world["db"], auth_world["client"]
    admin = _add_user(db, "taj-admin@tajlandia.com", "admin", ["tajlandia.com"])
    res = c.post("/api/v1/auth/login", json={"email": admin.email, "password": "Strong#Pass1", "domain": "varsoviaestate.com"})
    assert res.status_code == 403
    ok = c.post("/api/v1/auth/login", json={"email": admin.email, "password": "Strong#Pass1", "domain": "tajlandia.com"})
    assert ok.status_code == 200, ok.text
    db.refresh(admin)
    assert admin.domain == "tajlandia.com"


def test_wrong_password_does_not_reveal_blocked_accounts_and_deactivated_cannot_login(auth_world):
    db, c = auth_world["db"], auth_world["client"]
    _add_user(db, "blocked@tajlandia.com", "worker", ["tajlandia.com"], status="blocked")
    _add_user(db, "gone@tajlandia.com", "worker", ["tajlandia.com"], status="deactivated")
    assert c.post("/api/v1/auth/login", json={"email": "blocked@tajlandia.com", "password": "wrong"}).status_code == 401
    assert c.post("/api/v1/auth/login", json={"email": "gone@tajlandia.com", "password": "Strong#Pass1"}).status_code == 403


def _mgmt_client(auth_world, actor):
    from app.api.v1.auth import router as auth_router
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: (yield auth_world["db"])
    app.dependency_overrides[get_current_user_from_db] = lambda: _as_staff(auth_world, actor)
    return TestClient(app)


def test_last_super_admin_cannot_be_demoted_blocked_or_deleted(auth_world):
    db = auth_world["db"]
    boss = db.query(User).filter(User.email == "boss@tajlandia.com").first()
    c = _mgmt_client(auth_world, boss)
    assert c.patch(f"/api/v1/auth/admin/users/{boss.id}", json={"role": "worker"}).status_code == 409
    db.refresh(boss)
    assert boss.role == "super_admin"
    second = _add_user(db, "boss2@tajlandia.com", "super_admin", ["tajlandia.com"])
    assert c.patch(f"/api/v1/auth/admin/users/{second.id}", json={"status": "deactivated"}).status_code == 200
    assert c.post(f"/api/v1/auth/admin/users/{second.id}/block", json={"blocked": True}).status_code in (200, 404)


def test_user_status_must_be_known(auth_world):
    db = auth_world["db"]
    boss = db.query(User).filter(User.email == "boss@tajlandia.com").first()
    w = _add_user(db, "w1@tajlandia.com", "worker", ["tajlandia.com"])
    c = _mgmt_client(auth_world, boss)
    assert c.patch(f"/api/v1/auth/admin/users/{w.id}", json={"status": "whatever_string"}).status_code == 422


def test_user_delete_is_soft_and_revokes_access(auth_world):
    db = auth_world["db"]
    boss = db.query(User).filter(User.email == "boss@tajlandia.com").first()
    w = _add_user(db, "leaver@tajlandia.com", "worker", ["tajlandia.com"])
    c = _mgmt_client(auth_world, boss)
    assert c.delete(f"/api/v1/auth/admin/users/{w.id}").status_code == 200
    db.expire_all()
    row = db.query(User).filter(User.email == "leaver@tajlandia.com").first()
    assert row is not None and row.status == "deleted" and row.is_active is False
    assert row.preferences.get("deleted_by") == "boss@tajlandia.com"
    assert all(u["email"] != "leaver@tajlandia.com" for u in c.get("/api/v1/auth/admin/users").json())
    assert auth_world["client"].post("/api/v1/auth/login", json={"email": "leaver@tajlandia.com", "password": "Strong#Pass1"}).status_code == 403
    assert c.delete(f"/api/v1/auth/admin/users/{w.id}").status_code == 409


def test_admin_cannot_manage_users_with_websites_outside_their_scope(auth_world):
    db = auth_world["db"]
    taj_admin = _add_user(db, "a-taj@tajlandia.com", "admin", ["tajlandia.com"])
    vars_worker = _add_user(db, "w-vars@varsoviaestate.com", "worker", ["varsoviaestate.com"])
    c = _mgmt_client(auth_world, taj_admin)
    assert c.patch(f"/api/v1/auth/admin/users/{vars_worker.id}", json={"name": "x"}).status_code == 403
    assert c.delete(f"/api/v1/auth/admin/users/{vars_worker.id}").status_code == 403
    listing = c.get("/api/v1/auth/admin/users")
    # 403 when this server instance is configured for another site; otherwise the out-of-scope worker is hidden
    assert listing.status_code == 403 or all(u["email"] != vars_worker.email for u in listing.json())


# ----------------------------------------------------------------------------- Firestore auth (C2, H4)


def test_password_update_never_creates_a_super_admin_doc(monkeypatch):
    from app.services import firestore_auth_service as fas

    fs = FakeFirestore()
    monkeypatch.setattr(fas, "get_firestore_client", lambda: fs)
    assert fas.update_admin_password_in_firestore("worker@tajlandia.com", "NewPass#123") is False
    assert fs.items(fas.ADMIN_AUTH_COLLECTION) == []


def test_role_less_firestore_doc_is_least_privilege_and_cannot_overwrite_postgres_role(monkeypatch):
    from app.services import firestore_auth_service as fas

    fs = FakeFirestore()
    fs.set(fas.ADMIN_AUTH_COLLECTION, "w@tajlandia.com", {"email": "w@tajlandia.com", "passwordHash": hash_password("Pw#12345")})
    monkeypatch.setattr(fas, "get_firestore_client", lambda: fs)
    verified = fas.verify_firestore_admin_credentials("w@tajlandia.com", "Pw#12345")
    assert verified["role"] == "worker"

    db = _session()
    db.add(User(email="w@tajlandia.com", role="worker", domain="tajlandia.com", is_active=True, status="active"))
    db.commit()
    user = fas.sync_firestore_admin_to_local_db(db, {**verified, "role": "super_admin", "domain": "varsoviaestate.com"})
    assert user.role == "worker"
    assert user.domain == "tajlandia.com"


def test_stored_hash_is_not_accepted_as_a_password(monkeypatch):
    from app.services import firestore_auth_service as fas

    fs = FakeFirestore()
    stored = hash_password("Real#Secret9")
    fs.set(fas.ADMIN_AUTH_COLLECTION, "a@tajlandia.com", {"email": "a@tajlandia.com", "passwordHash": stored, "role": "admin"})
    monkeypatch.setattr(fas, "get_firestore_client", lambda: fs)
    assert fas.verify_firestore_admin_credentials("a@tajlandia.com", stored) is None
    assert fas.verify_firestore_admin_credentials("a@tajlandia.com", "Real#Secret9") is not None


def test_new_admin_docs_do_not_store_plaintext_passwords(monkeypatch):
    from app.services import firestore_auth_service as fas

    fs = FakeFirestore()
    monkeypatch.setattr(fas, "get_firestore_client", lambda: fs)
    fas.add_or_update_admin_in_firestore("n@tajlandia.com", "Plain#Text9", role="worker")
    doc = fs.get(fas.ADMIN_AUTH_COLLECTION, "n@tajlandia.com")
    # (the fake keeps Firestore's DELETE_FIELD sentinel instead of removing the key; real Firestore removes it)
    assert doc.get("password") != "Plain#Text9" and doc["passwordHash"] != "Plain#Text9"
    assert "Plain#Text9" not in repr(doc)


# ----------------------------------------------------------------------------- public file routes (C3, H1)


@pytest.fixture
def public_app(tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setattr(settings, "REPORT_STORAGE_DIR", str(tmp_path / "reports"))
    media = tmp_path / "reports" / "media" / "properties"
    media.mkdir(parents=True)
    (media / "villa.webp").write_bytes(b"RIFFxxxxWEBP")
    (tmp_path / "reports" / "tajlandia_lead_report_20261101_101500.xlsx").write_bytes(b"PII")
    (tmp_path / "reports" / "brochure_villa_t1_abc123.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "secret_outside.txt").write_text("TOP SECRET")
    monkeypatch.setattr("app.services.cloud_media_storage.cloud_storage._get_access_token", lambda: None, raising=False)
    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/api/v1/media/..%2F..%2Fsecret_outside.txt",
    "/media/%2E%2E/%2E%2E/secret_outside.txt",
    "/api/v1/media/..%5C..%5Csecret_outside.txt",
    "/api/v1/media/properties/..%2F..%2F..%2Fsecret_outside.txt",
])
def test_media_route_cannot_escape_media_folder(public_app, path, monkeypatch):
    monkeypatch.setattr("httpx.Client.get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network")))
    res = public_app.get(path)
    assert res.status_code == 404
    assert b"TOP SECRET" not in res.content


def test_media_route_still_serves_images(public_app):
    res = public_app.get("/api/v1/media/properties/villa.webp")
    assert res.status_code == 200 and res.headers["content-type"] == "image/webp"


def test_public_brochure_route_does_not_serve_lead_reports(public_app):
    assert public_app.get("/api/v1/chat/brochure/file/tajlandia_lead_report_20261101_101500.xlsx").status_code == 404
    assert public_app.get("/api/v1/chat/brochure/file/..%5C..%5Csecret_outside.txt").status_code == 404
    ok = public_app.get("/api/v1/chat/brochure/file/brochure_villa_t1_abc123.pdf")
    assert ok.status_code == 200 and ok.content.startswith(b"%PDF")


# ----------------------------------------------------------------------------- ingest endpoints (C4)


def test_ingest_endpoints_require_authentication():
    from app.main import app

    c = TestClient(app)
    doc = c.post("/api/v1/properties/ingest-document", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")},
                 data={"property_id": "victim-id"})
    url = c.post("/api/v1/properties/ingest-url", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert doc.status_code == 401 and url.status_code == 401


def test_ingest_endpoints_refuse_workers():
    from app.main import app

    app.dependency_overrides[get_current_user_from_db] = lambda: _staff("worker")
    try:
        c = TestClient(app)
        res = c.post("/api/v1/properties/ingest-url", json={"url": "https://example.com"})
        assert res.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_ingest_url_blocks_private_addresses():
    from app.services.document_ingestion_pipeline import process_and_ingest_property_url

    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8000/", "http://localhost/admin"):
        with pytest.raises(ValueError):
            process_and_ingest_property_url(url, save_to_database=False)


# ----------------------------------------------------------------------------- admin chat ownership (H3), logs (M3)


def test_admin_chat_history_is_owner_only(monkeypatch):
    from app.api.v1.admin import router as admin_router
    from app.services import conversation_service

    conversation_service._IN_SESSION_CONVERSATIONS.clear()
    monkeypatch.setattr(conversation_service, "_firestore_sync_enabled", lambda agent_type: False)
    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")
    holder = {"staff": _staff("super_admin", "boss-1")}
    app.dependency_overrides[get_current_user_from_db] = lambda: holder["staff"]
    app.dependency_overrides[get_db] = lambda: (yield None)
    c = TestClient(app)
    assert c.get("/api/v1/admin/chat/history/boss-session").status_code == 200  # owner claims it
    holder["staff"] = _staff("worker", "worker-9")
    assert c.get("/api/v1/admin/chat/history/boss-session").status_code == 404
    holder["staff"] = _staff("super_admin", "boss-1")
    assert c.get("/api/v1/admin/chat/history/boss-session").status_code == 200
    conversation_service._IN_SESSION_CONVERSATIONS.clear()


def test_workers_cannot_read_staff_activity_logs():
    from app.api.v1.admin import router as admin_router

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user_from_db] = lambda: _staff("worker")
    app.dependency_overrides[get_db] = lambda: (yield None)
    c = TestClient(app)
    assert c.get("/api/v1/admin/activity-logs").status_code == 403
    assert c.get("/api/v1/admin/data-history").status_code == 403
