"""Regression test: Chat create must only match an exact listing ID, never substrings.

When an existing listing has ID 'crud-run-k1' and a new listing is proposed with ID 'crud-run-k1b',
the creation must NOT resolve 'crud-run-k1' via substring matching, must NOT switch mode to 'update',
and on Confirm must create the new listing rather than silently updating the existing one.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedStaff
from app.core.config import settings
from app.core.deps import FirestoreSession
from app.main import app
from app.repositories import users_repo
from app.services.admin_agent import _execute_admin_tool
from app.services import firestore_service as fsvc
from tests import fs_users

client = TestClient(app)
TAJ, VAR = "tajlandia.com", "varsoviaestate.com"
PASSWORD = "ExactCreatePass#123"


@pytest.fixture(autouse=True)
def no_sync(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)


@pytest.fixture
def admin_staff():
    email = f"exact_admin_{uuid.uuid4().hex[:6]}@tajlandia.com"
    made = fs_users.make_user(client, "admin", email=email, password=PASSWORD, allowed_websites=[TAJ, VAR])
    uid = str(users_repo.get_by_email(None, email).id)
    return SimpleNamespace(
        id=uid,
        email=email,
        role="admin",
        token=made["headers"]["Authorization"].split(" ", 1)[1],
    )


def _staff(who, selected):
    return AuthenticatedStaff(
        user_id=who.id,
        email=who.email,
        name="Admin",
        role=who.role,
        allowed_websites=[TAJ, VAR],
        selected_websites=list(selected),
    )


def _row(pid):
    snap = fsvc.get_admin_firestore_client().collection("projects").document(pid).get()
    if not snap.exists:
        return None
    raw = snap.to_dict() or {}
    price = raw.get("priceFrom") if raw.get("priceFrom") is not None else raw.get("price_numeric_min")
    return SimpleNamespace(
        websites=list(raw.get("websites") or []),
        status=raw.get("status"),
        name=raw.get("name") or raw.get("property_name"),
        price=price,
        raw=raw,
    )


def test_chat_create_substring_id_does_not_hijack_create(admin_staff):
    """
    Step 1: Create a base listing 'crud-k1' in Firestore.
    Step 2: Propose creating 'crud-k1b' through chat tool.
    Verify:
      - 'crud-k1' is not matched by substring.
      - Proposed action has mode='create' and resolve_source is None.
      - Staged diff has mode='create'.
    Step 3: Confirm the action.
    Verify:
      - 'crud-k1b' is created in Firestore.
      - 'crud-k1' remains intact with its original data.
    """
    prefix = f"sub-{uuid.uuid4().hex[:6]}"
    base_id = f"{prefix}-k1"
    new_id = f"{prefix}-k1b"

    # 1. Insert base listing directly in Firestore
    fs = fsvc.get_admin_firestore_client()
    fs.collection("projects").document(base_id).set({
        "property_id": base_id,
        "name": "Base Property K1",
        "property_type": "Villa",
        "location": "Phuket",
        "priceFrom": 10_000_000,
        "price_numeric_min": 10_000_000,
        "websites": [TAJ],
        "isActive": True,
        "status": "Available",
    })

    assert _row(base_id) is not None
    assert _row(base_id).name == "Base Property K1"

    # 2. Propose creating new listing with ID 'crud-k1b'
    tool_args = {
        "property_id": new_id,
        "mode": "create",
        "fields_to_update": {
            "property_name": "New Property K1b",
            "property_type": "Villa",
            "province": "Phuket",
            "price_numeric_min": 15_000_000,
            "status": "Available",
        },
    }

    result, staged, action_id = _execute_admin_tool(
        db=FirestoreSession(),
        tool_name="create_or_update_listing",
        tool_args=tool_args,
        staff_user=_staff(admin_staff, [VAR]),
        conversation_id=str(uuid.uuid4()),
    )

    assert staged is True
    assert result.get("status") == "PROPOSED"
    # Must NOT have hijacked the ID to base_id
    assert tool_args.get("property_id") == new_id
    # Must NOT have switched to update mode
    assert tool_args.get("mode") == "create"
    assert tool_args.get("resolve_source") is None

    # Verify staged pending action in Firestore
    pending = fs_users.pending_for(admin_staff.email)
    staged_action = next(a for a in pending if a["id"] == action_id)
    payload = staged_action.get("action_payload") or {}
    assert payload.get("property_id") == new_id
    assert payload.get("mode") == "create"
    assert payload.get("resolve_source") is None

    # 3. Confirm the action
    headers = {
        "Authorization": f"Bearer {admin_staff.token}",
        "X-Selected-Websites": VAR,
    }
    confirm_resp = client.post(
        f"/api/v1/admin/actions/{action_id}/confirm",
        headers=headers,
        json={"confirmation_token": fs_users.pending_token(action_id)},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text

    # Both documents must exist independently
    base_data = _row(base_id)
    new_data = _row(new_id)

    assert base_data is not None
    assert base_data.name == "Base Property K1"
    assert base_data.websites == [TAJ]

    assert new_data is not None
    assert new_data.name == "New Property K1b"
    assert new_data.websites == [VAR]
    assert int(new_data.price) == 15_000_000


def test_chat_create_with_exact_same_id_still_updates_existing(admin_staff):
    """
    When the proposed ID matches an existing listing ID EXACTLY,
    chat create intentionally updates it rather than duplicating.
    """
    exact_id = f"exact-{uuid.uuid4().hex[:6]}"
    fs = fsvc.get_admin_firestore_client()
    fs.collection("projects").document(exact_id).set({
        "property_id": exact_id,
        "name": "Original Name",
        "property_type": "Villa",
        "location": "Phuket",
        "priceFrom": 5_000_000,
        "price_numeric_min": 5_000_000,
        "websites": [TAJ],
        "isActive": True,
        "status": "Available",
    })

    tool_args = {
        "property_id": exact_id,
        "mode": "create",
        "fields_to_update": {
            "price_numeric_min": 7_000_000,
        },
    }

    result, staged, action_id = _execute_admin_tool(
        db=FirestoreSession(),
        tool_name="create_or_update_listing",
        tool_args=tool_args,
        staff_user=_staff(admin_staff, [TAJ]),
        conversation_id=str(uuid.uuid4()),
    )

    assert staged is True
    # Because ID matched exactly, it resolves to update
    assert tool_args.get("mode") == "update"
    assert tool_args.get("resolve_source") == "firestore"

    headers = {
        "Authorization": f"Bearer {admin_staff.token}",
        "X-Selected-Websites": TAJ,
    }
    confirm_resp = client.post(
        f"/api/v1/admin/actions/{action_id}/confirm",
        headers=headers,
        json={"confirmation_token": fs_users.pending_token(action_id)},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    updated = _row(exact_id)
    assert int(updated.price) == 7_000_000
