"""
GET /api/v1/auth/admin/users and GET /api/v1/admin/users returned 500.

Causes fixed:
  * the `status` query parameter shadowed fastapi.status, so the domain 403 crashed into a 500;
  * list/details blocked super admins whose account domain differs from the deployment domain (create, update,
    block and delete already exempt super admins);
  * a malformed user id reached the database as a string (500 on Postgres) instead of 404.

Isolated: SQLite in memory and dependency overrides. No configured database is used.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import AuthenticatedStaff, require_admin_or_super_admin
from app.db.models import User, UserSession
from app.db.session import get_db
from app.main import app

URLS = ("/api/v1/auth/admin/users", "/api/v1/admin/users")


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    User.__table__.create(engine)
    UserSession.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    # the deployment runs as varsoviaestate.com
    monkeypatch.setattr("app.api.v1.auth.get_current_env_domain_key", lambda: "varsovia")
    rows = [
        dict(email="sa@tajlandia.com", role="super_admin", domain="tajlandia.com"),
        dict(email="admin@varsoviaestate.com", role="admin", domain="varsoviaestate.com"),
        dict(email="worker@varsoviaestate.com", role="worker", domain="varsoviaestate.com", status="blocked", is_active=False),
        dict(email="odd@varsoviaestate.com", role="worker", domain="varsoviaestate.com", preferences=None),
        dict(email="g@varsoviaestate.com", role="worker", domain="varsoviaestate.com",
             preferences={"google_workspace": {"connected": True, "refresh_token": "x", "connected_at": "2026-10-30T10:00:00"}}),
    ]
    ids = {}
    for r in rows:
        r.setdefault("is_active", True)
        user = User(id=uuid.uuid4(), name=r["email"].split("@")[0], created_at=datetime.utcnow(), **r)
        db.add(user)
        ids[r["email"]] = str(user.id)
    db.commit()
    who = {"staff": None}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin_or_super_admin] = lambda: who["staff"]
    client = TestClient(app, raise_server_exceptions=False)
    yield client, who, ids
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(require_admin_or_super_admin, None)
    db.close()


def _staff(email, role, domain):
    return AuthenticatedStaff(user_id="x", email=email, role=role, domain=domain,
                              allowed_websites=["tajlandia.com", "varsoviaestate.com", "thai.estate"])


@pytest.mark.parametrize("url", URLS)
def test_super_admin_from_another_domain_lists_users(env, url):
    client, who, _ = env
    who["staff"] = _staff("sa@tajlandia.com", "super_admin", "tajlandia.com")
    r = client.get(url)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 5


@pytest.mark.parametrize("url", URLS)
def test_admin_from_another_domain_gets_403_not_500(env, url):
    client, who, _ = env
    who["staff"] = _staff("other@tajlandia.com", "admin", "tajlandia.com")
    r = client.get(url)
    assert r.status_code == 403
    assert "different domain" in r.json()["detail"]


@pytest.mark.parametrize("url", URLS)
def test_admin_of_this_domain_lists_and_status_filter_still_works(env, url):
    client, who, _ = env
    who["staff"] = _staff("admin@varsoviaestate.com", "admin", "varsoviaestate.com")
    r = client.get(url)
    assert r.status_code == 200, r.text
    emails = {u["email"] for u in r.json()}
    assert "sa@tajlandia.com" not in emails                      # admins never see super admins
    blocked = client.get(url, params={"status": "blocked"})
    assert blocked.status_code == 200
    assert [u["email"] for u in blocked.json()] == ["worker@varsoviaestate.com"]
    connected = next(u for u in r.json() if u["email"] == "g@varsoviaestate.com")
    assert connected["google_auth"]["connected"] is True


def test_user_details_super_admin_other_domain_and_malformed_id(env):
    client, who, ids = env
    who["staff"] = _staff("sa@tajlandia.com", "super_admin", "tajlandia.com")
    ok = client.get(f"/api/v1/auth/admin/users/{ids['admin@varsoviaestate.com']}")
    assert ok.status_code == 200, ok.text
    assert client.get("/api/v1/auth/admin/users/not-a-uuid").status_code == 404
    assert client.get(f"/api/v1/auth/admin/users/{uuid.uuid4()}").status_code == 404
    who["staff"] = _staff("other@tajlandia.com", "admin", "tajlandia.com")
    assert client.get(f"/api/v1/auth/admin/users/{ids['admin@varsoviaestate.com']}").status_code == 403
