"""Test helpers for the Firestore-only world: seed staff users, log them in, stage pending actions."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from app.core.tenant import ALL_CANONICAL_WEBSITES
from app.repositories import pending_actions_repo
from app.services.firestore_auth_service import (
    add_or_update_admin_in_firestore,
    update_admin_in_firestore,
)

PASSWORD = "PaTestPass123!"


def make_user(
    client: TestClient,
    role: str,
    prefix: str = "t_",
    allowed_websites: Optional[List[str]] = None,
    password: str = PASSWORD,
    preferences: Optional[Dict[str, Any]] = None,
    email: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an active staff user in the (fake) admin Firestore and log in through the real endpoint."""
    email = email or f"{prefix}{role}_{uuid.uuid4().hex[:6]}@tajlandia.com"
    add_or_update_admin_in_firestore(
        email=email, password=password, role=role, name=role,
        allowed_websites=list(allowed_websites or ALL_CANONICAL_WEBSITES),
    )
    if preferences:
        from app.repositories import users_repo
        rec = users_repo.get_by_email(None, email)
        rec.preferences = dict(preferences)
        users_repo.save_user(None, rec)
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"email": email, "headers": {"Authorization": f"Bearer {resp.json()['access_token']}"}}


def deactivate(email: str) -> None:
    assert update_admin_in_firestore(email=email, is_active=False)


def make_pending(
    owner_email: str,
    tool_name: str = "send_email",
    payload: Optional[Dict[str, Any]] = None,
    minutes: Optional[int] = None,
    conversation_id: Optional[str] = None,
) -> str:
    """Stage a pending action owned by `owner_email`; minutes<0 makes it already expired."""
    row = pending_actions_repo.create(
        None,
        staff_email=owner_email,
        tool_name=tool_name,
        action_payload=payload if payload is not None else {"to": "client@example.com", "subject": "Hello", "body": "Hi"},
        conversation_id=conversation_id,
        expires_in_minutes=minutes if minutes is not None else 2 * 24 * 60,
    )
    return str(row.id)


def pending_status(action_id: str) -> str:
    return pending_actions_repo.get(None, action_id).status


def pending_token(action_id: str) -> str:
    return pending_actions_repo.get(None, action_id).confirmation_token


def seed_user(
    role: str,
    email: str,
    allowed_websites: Optional[List[str]] = None,
    preferences: Optional[Dict[str, Any]] = None,
    password: str = PASSWORD,
) -> str:
    """Create an active staff user without logging in."""
    from app.repositories import users_repo
    add_or_update_admin_in_firestore(
        email=email, password=password, role=role, name=role,
        allowed_websites=list(allowed_websites or ALL_CANONICAL_WEBSITES),
    )
    if preferences is not None:
        rec = users_repo.get_by_email(None, email)
        rec.preferences = dict(preferences)
        users_repo.save_user(None, rec)
    return email


def set_preferences(email: str, preferences: Dict[str, Any]) -> None:
    from app.repositories import users_repo
    rec = users_repo.get_by_email(None, email)
    rec.preferences = dict(preferences)
    users_repo.save_user(None, rec)


def get_preferences(email: str) -> Dict[str, Any]:
    from app.repositories import users_repo
    return dict(users_repo.get_by_email(None, email).preferences or {})


def seed_property(property_id: str, name: str = "Test", websites: Optional[List[str]] = None, **fields: Any) -> str:
    """Write one listing into the (fake) `projects` collection."""
    from app.services import firestore_service as fsvc
    doc = {"name": name, "status": "Available", "isActive": True, "websites": list(websites or ["tajlandia.com"])}
    doc.update(fields)
    fsvc.get_admin_firestore_client().collection("projects").document(property_id).set(doc)
    return property_id


def pending_for(email: str) -> List[Dict[str, Any]]:
    from app.services import firestore_service as fsvc
    return [
        d.to_dict() for d in fsvc.get_admin_firestore_client().collection("pending_actions").stream()
        if (d.to_dict() or {}).get("staff_user_id") == email
    ]


def audit_for_pending(pending_id: str) -> List[Dict[str, Any]]:
    """Audit rows of one pending action, oldest first, as plain dicts."""
    from app.services import firestore_service as fsvc
    rows = [
        d.to_dict() for d in fsvc.get_admin_firestore_client().collection("audit_logs").stream()
        if (d.to_dict() or {}).get("pending_action_id") == pending_id
    ]
    return sorted(rows, key=lambda r: str(r.get("created_at") or ""))


def audit_by_tool(tool: str) -> List[Dict[str, Any]]:
    from app.services import firestore_service as fsvc
    return [
        d.to_dict() for d in fsvc.get_admin_firestore_client().collection("audit_logs").stream()
        if (d.to_dict() or {}).get("tool_name") == tool
    ]
