"""Bug 6: a staff member must not be able to lock themselves (or the platform) out through user management.

Rules enforced on the backend, whatever the UI sends:
  - nobody can deactivate / block / demote / delete their own account through the directory API;
  - the last active super admin can never be demoted, disabled or deleted;
  - a regular admin can never edit a super admin or grant super_admin (or admin).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.repositories import users_repo
from tests import fs_users

client = TestClient(app)
PREFIX = "lockout_"
PASSWORD = "LockoutPass123!"
USERS = "/api/v1/auth/admin/users"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    yield


def _make(role: str):
    made = fs_users.make_user(client, role, prefix=PREFIX, password=PASSWORD)
    rec = users_repo.get_by_email(None, made["email"])
    return {"email": made["email"], "id": str(rec.id), "headers": made["headers"]}


def _reload(person):
    return users_repo.get_by_email(None, person["email"])


# ---- self-lockout of a super admin (even when another super admin exists) ------------------------

@pytest.mark.parametrize("body", [
    {"status": "deactivated"},
    {"status": "blocked"},
    {"is_active": False},
])
def test_super_admin_cannot_disable_own_account(body):
    me = _make("super_admin")
    _make("super_admin")  # another active super admin exists, so this is purely the self rule
    resp = client.patch(f"{USERS}/{me['id']}", headers=me["headers"], json=body)
    assert resp.status_code == 400, resp.text
    assert "own account" in resp.json()["detail"].lower()
    rec = _reload(me)
    assert rec.is_active is True and (rec.status or "active") == "active"


@pytest.mark.parametrize("new_role", ["admin", "worker", "employee"])
def test_super_admin_cannot_demote_self(new_role):
    me = _make("super_admin")
    _make("super_admin")
    resp = client.put(f"{USERS}/{me['id']}", headers=me["headers"], json={"role": new_role})
    assert resp.status_code == 400, resp.text
    assert _reload(me).role == "super_admin"


def test_super_admin_cannot_delete_or_block_self():
    me = _make("super_admin")
    _make("super_admin")
    assert client.delete(f"{USERS}/{me['id']}", headers=me["headers"]).status_code == 400
    assert client.post(f"{USERS}/{me['id']}/block", headers=me["headers"], json={"blocked": True}).status_code == 400
    rec = _reload(me)
    assert rec is not None and rec.is_active and rec.role == "super_admin"


def test_super_admin_can_still_edit_own_profile():
    """The guard must not over-block: name changes and re-sending the same role/status are fine."""
    me = _make("super_admin")
    resp = client.patch(f"{USERS}/{me['id']}", headers=me["headers"],
                        json={"name": "Renamed Boss", "role": " Super_Admin ", "status": "active", "is_active": True})
    assert resp.status_code == 200, resp.text
    rec = _reload(me)
    assert rec.name == "Renamed Boss" and rec.role == "super_admin" and rec.is_active


# ---- the last active super admin ----------------------------------------------------------------

def test_super_admin_can_manage_another_super_admin_when_one_remains():
    me = _make("super_admin")
    other = _make("super_admin")
    resp = client.patch(f"{USERS}/{other['id']}", headers=me["headers"], json={"status": "deactivated"})
    assert resp.status_code == 200, resp.text
    assert _reload(other).is_active is False


def test_last_active_super_admin_guard_refuses_every_removal(monkeypatch):
    """If the target is the only active super admin, demote / disable / delete / block are refused (409)."""
    from fastapi import HTTPException
    from app.api.v1 import auth as auth_api

    only = _make("super_admin")
    # Make every other super admin in the store inactive so `only` is the last one.
    for u in users_repo.list_users(None, roles=["super_admin"]):
        if str(u.id) != only["id"]:
            u.is_active = False
            u.status = "deactivated"
            users_repo.save_user(None, u)
    target = _reload(only)
    for action in ("demote or disable", "delete", "block"):
        with pytest.raises(HTTPException) as exc:
            auth_api._require_other_active_super_admin(None, target, action)
        assert exc.value.status_code == 409
        assert "last active super admin" in exc.value.detail


def test_last_active_super_admin_guard_fails_closed_when_store_errors(monkeypatch):
    """A store error while counting super admins must not silently allow removing one."""
    me = _make("super_admin")
    other = _make("super_admin")
    real_list = users_repo.list_users

    def broken(db, **kw):
        if kw.get("roles") == ["super_admin"]:
            raise RuntimeError("firestore down")
        return real_list(db, **kw)

    monkeypatch.setattr(users_repo, "list_users", broken)
    resp = client.patch(f"{USERS}/{other['id']}", headers=me["headers"], json={"status": "deactivated"})
    assert resp.status_code in (409, 503), resp.text
    monkeypatch.setattr(users_repo, "list_users", real_list)
    assert _reload(other).is_active is True


# ---- a regular admin and super admins -----------------------------------------------------------

def test_admin_cannot_edit_or_delete_a_super_admin():
    admin = _make("admin")
    boss = _make("super_admin")
    for body in ({"name": "pwned"}, {"status": "deactivated"}, {"role": "worker"}):
        resp = client.patch(f"{USERS}/{boss['id']}", headers=admin["headers"], json=body)
        assert resp.status_code == 403, resp.text
    assert client.delete(f"{USERS}/{boss['id']}", headers=admin["headers"]).status_code == 403
    assert client.post(f"{USERS}/{boss['id']}/block", headers=admin["headers"], json={"blocked": True}).status_code == 403
    rec = _reload(boss)
    assert rec.role == "super_admin" and rec.is_active and rec.name != "pwned"


def test_admin_cannot_grant_super_admin_to_anyone_including_self():
    admin = _make("admin")
    worker = _make("worker")
    assert client.patch(f"{USERS}/{worker['id']}", headers=admin["headers"], json={"role": "super_admin"}).status_code == 403
    assert client.patch(f"{USERS}/{admin['id']}", headers=admin["headers"], json={"role": "super_admin"}).status_code in (400, 403)
    resp = client.post(USERS, headers=admin["headers"], json={
        "email": f"{PREFIX}new_{uuid.uuid4().hex[:6]}@tajlandia.com", "password": "NewAccount123!", "role": "super_admin"})
    assert resp.status_code == 403
    assert _reload(worker).role == "worker" and _reload(admin).role == "admin"


def test_admin_cannot_disable_or_demote_self():
    admin = _make("admin")
    assert client.patch(f"{USERS}/{admin['id']}", headers=admin["headers"], json={"status": "deactivated"}).status_code == 400
    assert client.patch(f"{USERS}/{admin['id']}", headers=admin["headers"], json={"role": "worker"}).status_code == 400
    rec = _reload(admin)
    assert rec.role == "admin" and rec.is_active
