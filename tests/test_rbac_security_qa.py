"""
Role-Based Access Control (RBAC), Tenant Isolation, IDOR Prevention, and Session Security QA Tests.
Covers:
  - Super Admin vs Admin vs Worker/Regular User boundaries
  - Cross-tenant data isolation and rejection
  - IDOR prevention on user profiles, conversations, and site access
  - Invalid, expired, and revoked token rejection
  - Account status lifecycle (blocked, deactivated, deleted) session instant termination
Hermetic in-memory SQLite test suite with zero live service dependencies.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

sqlite3.register_adapter(uuid.UUID, lambda u: str(u))
sqlite3.register_adapter(list, lambda l: json.dumps(l))


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"

from app.core.config import settings
from app.core.security import hash_password, generate_session_token
from app.core.auth import (
    AuthenticatedStaff,
    get_current_user_from_db,
    require_super_admin,
    require_admin_or_super_admin,
    require_admin_or_employee,
    require_admin_or_employee_with_sites,
    require_admin_or_super_admin_with_sites,
    require_super_admin_with_sites,
    resolve_user_allowed_websites,
    get_role_permissions,
)
from app.db.models import User, UserSession, Property, Conversation, PendingAction, AuditLog
from app.db.session import get_db
from app.api.v1.auth import router as auth_router
from app.api.v1.admin import router as admin_router


def _create_test_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in (User.__table__, UserSession.__table__, Property.__table__, Conversation.__table__, PendingAction.__table__, AuditLog.__table__):
        table.create(bind=engine, checkfirst=True)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


@pytest.fixture
def qa_env(monkeypatch):
    """Hermetic test environment with mocked Firestore and in-memory SQLite DB."""
    db = _create_test_db()
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "get_admin_enable_firestore_sync", lambda: False)
    monkeypatch.setattr("app.services.action_service._fs_sync_enabled", lambda: False)
    monkeypatch.setattr("app.services.firestore_auth_service.get_firestore_client", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.admin_batch_service.get_firestore_client", lambda *a, **kw: None)
    monkeypatch.setattr("app.core.tenant.get_current_env_domain_key", lambda: "tajlandia")
    monkeypatch.setattr("app.core.tenant.get_current_env_domain", lambda: "tajlandia.com")
    monkeypatch.setattr("app.api.v1.auth.get_current_env_domain_key", lambda: "tajlandia")
    monkeypatch.setattr("app.api.v1.auth.get_current_env_domain", lambda: "tajlandia.com")
    monkeypatch.setattr("app.core.auth.get_current_env_domain_key", lambda: "tajlandia")
    monkeypatch.setattr("app.core.auth.get_current_env_domain", lambda: "tajlandia.com")

    from app.services import conversation_service
    conversation_service._IN_SESSION_CONVERSATIONS.clear()
    monkeypatch.setattr(conversation_service, "_firestore_sync_enabled", lambda agent_type: False)

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")

    # Override get_db to provide our in-memory test database
    app.dependency_overrides[get_db] = lambda: (yield db)

    client = TestClient(app)
    yield {"db": db, "client": client, "app": app}
    conversation_service._IN_SESSION_CONVERSATIONS.clear()
    db.close()


def _seed_user(
    db: Session,
    email: str,
    role: str,
    allowed_websites: List[str],
    password: str = "TestPass123!",
    domain: Optional[str] = None,
    status: str = "active",
    is_active: bool = True,
) -> User:
    u = User(
        email=email.lower().strip(),
        name=email.split("@")[0].title(),
        role=role,
        domain=domain or allowed_websites[0],
        password_hash=hash_password(password),
        is_active=is_active,
        status=status,
        allowed_websites=list(allowed_websites),
        preferences={"allowed_websites": list(allowed_websites)},
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _create_session(
    db: Session,
    user: User,
    expires_delta: timedelta = timedelta(hours=24),
    revoked: bool = False,
) -> str:
    token = generate_session_token()
    sess = UserSession(
        user_id=user.id,
        token=token,
        expires_at=datetime.utcnow() + expires_delta,
        created_at=datetime.utcnow(),
        revoked_at=datetime.utcnow() if revoked else None,
    )
    db.add(sess)
    db.commit()
    return token


# =========================================================================
# 1. Super Admin vs Admin vs Worker/Regular User Boundaries
# =========================================================================

def test_worker_cannot_access_user_directory(qa_env):
    """Workers must be strictly forbidden (403) from accessing the staff user directory."""
    db, client = qa_env["db"], qa_env["client"]
    worker = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    token = _create_session(db, worker)

    resp = client.get("/api/v1/auth/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "lacks permission" in resp.json()["detail"].lower() or "access denied" in resp.json()["detail"].lower()


def test_worker_cannot_create_user(qa_env):
    """Workers must be forbidden (403) from creating any accounts."""
    db, client = qa_env["db"], qa_env["client"]
    worker = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    token = _create_session(db, worker)

    resp = client.post(
        "/api/v1/auth/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "email": "newbie@tajlandia.com",
            "password": "Password123!",
            "role": "worker"
        }
    )
    assert resp.status_code == 403


def test_worker_cannot_create_update_or_delete_properties(qa_env):
    """Workers have read-only access to properties and cannot perform write operations."""
    db, client = qa_env["db"], qa_env["client"]
    worker = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    token = _create_session(db, worker)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Selected-Websites": "tajlandia.com"
    }

    # Worker can read properties
    read_resp = client.get("/api/v1/admin/properties", headers=headers)
    assert read_resp.status_code == 200

    # Worker cannot create property
    create_resp = client.post(
        "/api/v1/admin/properties",
        headers=headers,
        json={
            "property_id": "prop-worker-test",
            "project_name_en": "Worker Villa",
            "project_name_pl": "Willa Pracownika",
            "property_type": "Villa",
            "country": "Thailand",
            "city": "Phuket",
            "websites": ["tajlandia.com"]
        }
    )
    assert create_resp.status_code == 403

    # Worker cannot update property
    update_resp = client.put(
        "/api/v1/admin/properties/prop-test-01",
        headers=headers,
        json={"property_name": "Updated by Worker"}
    )
    assert update_resp.status_code == 403

    # Worker cannot delete property
    del_resp = client.delete("/api/v1/admin/properties/prop-test-01", headers=headers)
    assert del_resp.status_code == 403


def test_admin_cannot_block_user(qa_env):
    """Only Super Admins can block accounts. Regular Admins receive 403 Forbidden."""
    db, client = qa_env["db"], qa_env["client"]
    admin = _seed_user(db, "admin@tajlandia.com", "admin", ["tajlandia.com"])
    target_worker = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    token = _create_session(db, admin)

    resp = client.post(
        f"/api/v1/auth/admin/users/{target_worker.id}/block",
        headers={"Authorization": f"Bearer {token}"},
        json={"blocked": True}
    )
    assert resp.status_code == 403
    assert "lacks permission" in resp.json()["detail"].lower()


def test_admin_cannot_create_super_admin_or_admin(qa_env):
    """Admins can only create workers; privilege escalation to admin/super_admin is rejected with 403."""
    db, client = qa_env["db"], qa_env["client"]
    admin = _seed_user(db, "admin@tajlandia.com", "admin", ["tajlandia.com"])
    token = _create_session(db, admin)
    headers = {"Authorization": f"Bearer {token}"}

    # Cannot create super_admin
    sa_resp = client.post(
        "/api/v1/auth/admin/users",
        headers=headers,
        json={"email": "hacked_sa@tajlandia.com", "password": "Pass1234!", "role": "super_admin"}
    )
    assert sa_resp.status_code == 403

    # Cannot create admin
    adm_resp = client.post(
        "/api/v1/auth/admin/users",
        headers=headers,
        json={"email": "hacked_admin@tajlandia.com", "password": "Pass1234!", "role": "admin"}
    )
    assert adm_resp.status_code == 403

    # CAN create worker
    w_resp = client.post(
        "/api/v1/auth/admin/users",
        headers=headers,
        json={"email": "legit_worker@tajlandia.com", "password": "Pass1234!", "role": "worker"}
    )
    assert w_resp.status_code == 201
    assert w_resp.json()["role"] == "worker"


def test_admin_cannot_modify_or_delete_other_admins(qa_env):
    """Admins cannot modify or delete peer admins, and cannot touch super admins."""
    db, client = qa_env["db"], qa_env["client"]
    admin1 = _seed_user(db, "admin1@tajlandia.com", "admin", ["tajlandia.com"])
    admin2 = _seed_user(db, "admin2@tajlandia.com", "admin", ["tajlandia.com"])
    superadmin = _seed_user(db, "boss@tajlandia.com", "super_admin", ["tajlandia.com"])
    token = _create_session(db, admin1)
    headers = {"Authorization": f"Bearer {token}"}

    # Cannot modify peer admin
    mod_peer = client.patch(f"/api/v1/auth/admin/users/{admin2.id}", headers=headers, json={"name": "Hacked Peer"})
    assert mod_peer.status_code == 403

    # Cannot delete peer admin
    del_peer = client.delete(f"/api/v1/auth/admin/users/{admin2.id}", headers=headers)
    assert del_peer.status_code == 403

    # Cannot modify super admin
    mod_sa = client.patch(f"/api/v1/auth/admin/users/{superadmin.id}", headers=headers, json={"name": "Hacked SA"})
    assert mod_sa.status_code == 403

    # Cannot delete super admin
    del_sa = client.delete(f"/api/v1/auth/admin/users/{superadmin.id}", headers=headers)
    assert del_sa.status_code == 403


def test_admin_cannot_promote_to_admin_or_super_admin(qa_env):
    """Regular Admins cannot promote workers to admin or super_admin."""
    db, client = qa_env["db"], qa_env["client"]
    admin = _seed_user(db, "admin@tajlandia.com", "admin", ["tajlandia.com"])
    worker = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    token = _create_session(db, admin)
    headers = {"Authorization": f"Bearer {token}"}

    promote_adm = client.patch(f"/api/v1/auth/admin/users/{worker.id}", headers=headers, json={"role": "admin"})
    assert promote_adm.status_code == 403

    promote_sa = client.patch(f"/api/v1/auth/admin/users/{worker.id}", headers=headers, json={"role": "super_admin"})
    assert promote_sa.status_code == 403


def test_super_admin_cannot_block_or_delete_self(qa_env):
    """A Super Admin cannot block or delete their own active account."""
    db, client = qa_env["db"], qa_env["client"]
    superadmin = _seed_user(db, "boss@tajlandia.com", "super_admin", ["tajlandia.com", "thai.estate", "varsoviaestate.com"])
    token = _create_session(db, superadmin)
    headers = {"Authorization": f"Bearer {token}"}

    # Self-block rejected
    block_self = client.post(f"/api/v1/auth/admin/users/{superadmin.id}/block", headers=headers, json={"blocked": True})
    assert block_self.status_code == 400
    assert "cannot block their own account" in block_self.json()["detail"].lower()

    # Self-delete rejected
    del_self = client.delete(f"/api/v1/auth/admin/users/{superadmin.id}", headers=headers)
    assert del_self.status_code == 400
    assert "cannot delete your own account" in del_self.json()["detail"].lower()


# =========================================================================
# 2. Cross-Tenant Data Isolation and Rejection
# =========================================================================

def test_user_cannot_select_unauthorized_websites_in_headers(qa_env):
    """Passing an unauthorized site in X-Selected-Websites must be rejected with 403."""
    db, client = qa_env["db"], qa_env["client"]
    admin_taj = _seed_user(db, "admin@tajlandia.com", "admin", ["tajlandia.com"])
    token = _create_session(db, admin_taj)

    # Attempt to access varsoviaestate.com
    resp = client.get(
        "/api/v1/admin/properties",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Selected-Websites": "varsoviaestate.com"
        }
    )
    assert resp.status_code == 403
    assert "access denied" in resp.json()["detail"].lower()
    assert "authorized websites are" in resp.json()["detail"].lower()


def test_user_cannot_select_unauthorized_websites_in_query_params(qa_env):
    """Passing an unauthorized site via query parameter must be rejected with 403."""
    db, client = qa_env["db"], qa_env["client"]
    worker = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    token = _create_session(db, worker)

    resp = client.get(
        "/api/v1/admin/properties?selected_websites=thai.estate",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert "access denied" in resp.json()["detail"].lower()


def test_admin_cannot_delegate_websites_outside_scope(qa_env):
    """An admin authorized only for tajlandia.com cannot create a user with access to varsoviaestate.com."""
    db, client = qa_env["db"], qa_env["client"]
    admin_taj = _seed_user(db, "admin@tajlandia.com", "admin", ["tajlandia.com"])
    token = _create_session(db, admin_taj)

    resp = client.post(
        "/api/v1/auth/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "email": "w2@tajlandia.com",
            "password": "Password123!",
            "role": "worker",
            "websites": ["tajlandia.com", "varsoviaestate.com"]
        }
    )
    assert resp.status_code == 403
    assert "outside your permissions" in resp.json()["detail"].lower()


def test_admin_cannot_manage_users_with_out_of_scope_websites(qa_env):
    """An admin cannot update or delete users who belong to websites outside the admin's scope."""
    db, client = qa_env["db"], qa_env["client"]
    admin_taj = _seed_user(db, "admin@tajlandia.com", "admin", ["tajlandia.com"])
    worker_varsovia = _seed_user(db, "w_vars@varsoviaestate.com", "worker", ["varsoviaestate.com"])
    token = _create_session(db, admin_taj)
    headers = {"Authorization": f"Bearer {token}"}

    # Patch out-of-scope worker
    patch_resp = client.patch(
        f"/api/v1/auth/admin/users/{worker_varsovia.id}",
        headers=headers,
        json={"name": "New Name"}
    )
    assert patch_resp.status_code == 403

    # Delete out-of-scope worker
    del_resp = client.delete(
        f"/api/v1/auth/admin/users/{worker_varsovia.id}",
        headers=headers
    )
    assert del_resp.status_code == 403


def test_super_admin_can_inspect_users_across_all_tenant_domains(qa_env):
    """Super Admin has universal access and can retrieve user details regardless of tenant domain."""
    db, client = qa_env["db"], qa_env["client"]
    superadmin = _seed_user(
        db, "boss@tajlandia.com", "super_admin",
        ["tajlandia.com", "thai.estate", "varsoviaestate.com"],
        domain="tajlandia.com"
    )
    worker_thai = _seed_user(db, "thai_staff@thai.estate", "worker", ["thai.estate"], domain="thai.estate")
    worker_varsovia = _seed_user(db, "vars_staff@varsoviaestate.com", "worker", ["varsoviaestate.com"], domain="varsoviaestate.com")
    token = _create_session(db, superadmin)
    headers = {"Authorization": f"Bearer {token}"}

    # Super admin inspects thai.estate worker
    r_thai = client.get(f"/api/v1/auth/admin/users/{worker_thai.id}", headers=headers)
    assert r_thai.status_code == 200
    assert r_thai.json()["email"] == "thai_staff@thai.estate"

    # Super admin inspects varsoviaestate.com worker
    r_vars = client.get(f"/api/v1/auth/admin/users/{worker_varsovia.id}", headers=headers)
    assert r_vars.status_code == 200
    assert r_vars.json()["email"] == "vars_staff@varsoviaestate.com"


# =========================================================================
# 3. IDOR Prevention on User Profiles, Conversations, and Pending Actions
# =========================================================================

def test_admin_chat_history_is_owner_only_idor_prevention(qa_env):
    """Staff member cannot view or hijack another staff member's admin conversation history."""
    db, client = qa_env["db"], qa_env["client"]
    user_a = _seed_user(db, "staff_a@tajlandia.com", "worker", ["tajlandia.com"])
    user_b = _seed_user(db, "staff_b@tajlandia.com", "worker", ["tajlandia.com"])
    token_a = _create_session(db, user_a)
    token_b = _create_session(db, user_b)

    # User A initiates a conversation
    session_id = f"admin-sess-{uuid.uuid4().hex[:8]}"
    conv = Conversation(
        session_id=session_id,
        agent_type="admin",
        preferences={"owner_user_id": str(user_a.id)},
        message_history=[{"role": "user", "content": "Confidential financial query"}]
    )
    db.add(conv)
    db.commit()

    # User A claims and accesses conversation
    res_a = client.get(f"/api/v1/admin/chat/history/{session_id}", headers={"Authorization": f"Bearer {token_a}"})
    assert res_a.status_code == 200

    # User B attempting to view User A's history is rejected with 404 (IDOR prevented)
    res_b = client.get(f"/api/v1/admin/chat/history/{session_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert res_b.status_code == 404
    assert "not found" in res_b.json()["detail"].lower()


def test_pending_action_inspection_idor_prevention(qa_env):
    """A worker cannot inspect another staff member's staged pending action."""
    db, client = qa_env["db"], qa_env["client"]
    user_a = _seed_user(db, "staff_a@tajlandia.com", "worker", ["tajlandia.com"])
    user_b = _seed_user(db, "staff_b@tajlandia.com", "worker", ["tajlandia.com"])
    token_b = _create_session(db, user_b)

    act = PendingAction(
        id=uuid.uuid4(),
        staff_user_id=str(user_a.id),
        tool_name="update_listing",
        action_payload={"price": "20,000,000 THB"},
        status="PENDING",
        confirmation_token=generate_session_token(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
        created_at=datetime.utcnow()
    )
    db.add(act)
    db.commit()

    # Worker B tries to inspect Worker A's pending action -> 403 Forbidden
    resp = client.get(f"/api/v1/admin/actions/{act.id}", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 403
    assert "only view actions you proposed" in resp.json()["detail"].lower()


def test_pending_action_confirm_idor_prevention(qa_env):
    """Only the staff member who proposed an action can confirm it."""
    db, client = qa_env["db"], qa_env["client"]
    proposer = _seed_user(db, "proposer@tajlandia.com", "admin", ["tajlandia.com"])
    attacker = _seed_user(db, "attacker@tajlandia.com", "admin", ["tajlandia.com"])
    token_attacker = _create_session(db, attacker)

    act = PendingAction(
        id=uuid.uuid4(),
        staff_user_id=str(proposer.id),
        tool_name="update_listing",
        action_payload={"price": "10,000,000 THB"},
        status="PENDING",
        confirmation_token="secret-token-1234",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        created_at=datetime.utcnow()
    )
    db.add(act)
    db.commit()

    # Attacker tries to confirm proposer's action -> 403 Forbidden
    resp = client.post(
        f"/api/v1/admin/actions/{act.id}/confirm",
        headers={"Authorization": f"Bearer {token_attacker}"},
        json={"confirmation_token": "secret-token-1234"}
    )
    assert resp.status_code == 403
    assert "only the staff member who proposed" in resp.json()["detail"].lower()


# =========================================================================
# 4. Invalid, Expired, and Blocked Token Rejection
# =========================================================================

def test_missing_or_invalid_bearer_token_is_rejected(qa_env):
    """Requests without token, with malformed token, or fake token must return 401."""
    client = qa_env["client"]

    # Missing Authorization header
    assert client.get("/api/v1/auth/me").status_code == 401

    # Malformed Bearer header
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer"}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401


def test_expired_token_is_rejected(qa_env):
    """Expired session tokens return 401 with an explanatory detail message."""
    db, client = qa_env["db"], qa_env["client"]
    user = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    expired_token = _create_session(db, user, expires_delta=timedelta(seconds=-10))

    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_revoked_token_is_rejected(qa_env):
    """Revoked session tokens return 401 with an explanatory detail message."""
    db, client = qa_env["db"], qa_env["client"]
    user = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"])
    revoked_token = _create_session(db, user, revoked=True)

    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {revoked_token}"})
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"].lower()


def test_blocked_account_sessions_are_instantly_terminated(qa_env):
    """When an account is blocked, any active session token is instantly revoked and returns 403."""
    db, client = qa_env["db"], qa_env["client"]
    user = _seed_user(db, "worker@tajlandia.com", "worker", ["tajlandia.com"], status="active")
    active_token = _create_session(db, user)

    # Verify session works
    ok_resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {active_token}"})
    assert ok_resp.status_code == 200

    # User is blocked
    user.status = "blocked"
    user.is_active = False
    db.commit()

    # Calling with the token must now be 403 Forbidden and the session terminated in DB
    denied = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {active_token}"})
    assert denied.status_code == 403
    assert "blocked" in denied.json()["detail"].lower()

    # Verify in DB that the session row was revoked
    sess = db.query(UserSession).filter(UserSession.token == active_token).first()
    assert sess.revoked_at is not None


def test_deactivated_account_sessions_are_instantly_terminated(qa_env):
    """When an account is deactivated, any active session token is immediately denied with 403."""
    db, client = qa_env["db"], qa_env["client"]
    user = _seed_user(db, "deact@tajlandia.com", "worker", ["tajlandia.com"], status="active")
    active_token = _create_session(db, user)

    # User is deactivated
    user.status = "deactivated"
    user.is_active = False
    db.commit()

    denied = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {active_token}"})
    assert denied.status_code == 403
    assert "deactivated" in denied.json()["detail"].lower()


def test_deleted_account_sessions_are_instantly_terminated(qa_env):
    """When an account is deleted, active sessions are revoked and cannot make calls."""
    db, client = qa_env["db"], qa_env["client"]
    user = _seed_user(db, "del@tajlandia.com", "worker", ["tajlandia.com"], status="active")
    active_token = _create_session(db, user)

    user.status = "deleted"
    user.is_active = False
    db.commit()

    denied = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {active_token}"})
    assert denied.status_code == 403
