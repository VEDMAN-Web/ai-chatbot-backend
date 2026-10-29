"""Confirm requires the proposer (or a super admin), a real token, and website access."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedStaff
from app.services.action_service import ACTION_STATUS_EXECUTED, ACTION_STATUS_PENDING, confirm_and_execute_action


def _staff(email, role, websites):
    return AuthenticatedStaff(
        user_id=email,
        email=email,
        role=role,
        allowed_websites=websites,
    )


def _action(**overrides):
    payload = {
        "mode": "update",
        "property_id": "prop-1",
        "websites": ["tajlandia.com"],
        "fields_to_update": {"property_name": "New Name"},
    }
    payload.update(overrides.pop("action_payload", {}))
    base = dict(
        id=uuid4(),
        confirmation_token="act_secret",
        staff_user_id="proposer@example.com",
        tool_name="create_or_update_listing",
        action_payload=payload,
        status=ACTION_STATUS_PENDING,
        conversation_id=None,
        expires_at=None,
        execution_result=None,
        confirmed_at=None,
        executed_at=None,
        error_message=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _confirm(action, staff, token="act_secret"):
    db = MagicMock()
    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a1")
    ), patch(
        "app.services.action_service._execute_update_listing",
        return_value={"property_id": "prop-1", "operation_id": "op-1"},
    ) as execute:
        result = confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            confirmation_token=token,
        )
    return result, execute


def test_other_admin_cannot_confirm():
    """Ownership is checked before the token — 403 even with a valid token."""
    action = _action()
    other = _staff("other@example.com", "admin", ["tajlandia.com"])
    with pytest.raises(HTTPException) as exc:
        _confirm(action, other)
    assert exc.value.status_code == 403
    assert "Only the staff member who proposed this action, or a super admin, can confirm it" in str(
        exc.value.detail
    )
    assert action.status == ACTION_STATUS_PENDING


def test_other_admin_gets_403_before_token_check():
    """A different admin with a missing token still gets 403 (not 400)."""
    action = _action()
    other = _staff("other@example.com", "admin", ["tajlandia.com"])
    with pytest.raises(HTTPException) as exc:
        _confirm(action, other, token=None)
    assert exc.value.status_code == 403
    assert "Only the staff member who proposed this action, or a super admin, can confirm it" in str(
        exc.value.detail
    )
    assert action.status == ACTION_STATUS_PENDING


def test_super_admin_can_confirm():
    action = _action()
    boss = _staff("boss@example.com", "super_admin", ["tajlandia.com"])
    result, execute = _confirm(action, boss)
    assert execute.called
    assert result["status"] == ACTION_STATUS_EXECUTED


def test_missing_token_is_rejected():
    """Missing token returns 400 only for the owner (or super admin)."""
    action = _action()
    proposer = _staff("proposer@example.com", "admin", ["tajlandia.com"])
    with pytest.raises(HTTPException) as exc:
        _confirm(action, proposer, token=None)
    assert exc.value.status_code == 400
    assert "required" in str(exc.value.detail).lower()
    assert action.status == ACTION_STATUS_PENDING


def test_super_admin_missing_token_is_400():
    action = _action()
    boss = _staff("boss@example.com", "super_admin", ["tajlandia.com"])
    with pytest.raises(HTTPException) as exc:
        _confirm(action, boss, token=None)
    assert exc.value.status_code == 400
    assert "required" in str(exc.value.detail).lower()


def test_wrong_website_is_rejected():
    action = _action(action_payload={"websites": ["thai.estate"]})
    proposer = _staff("proposer@example.com", "admin", ["tajlandia.com"])
    with pytest.raises(HTTPException) as exc:
        _confirm(action, proposer)
    assert exc.value.status_code == 403
    assert action.status == ACTION_STATUS_PENDING


def test_double_confirm_executes_once():
    action = _action()
    proposer = _staff("proposer@example.com", "admin", ["tajlandia.com"])
    db = MagicMock()
    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a1")
    ), patch(
        "app.services.action_service._execute_update_listing",
        return_value={"property_id": "prop-1", "operation_id": "op-1"},
    ) as execute:
        first = confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=proposer,
            confirmation_token="act_secret",
        )
        second = confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=proposer,
            confirmation_token="act_secret",
        )
    assert execute.call_count == 1
    assert second["execution_result"] == first["execution_result"]
    assert second.get("idempotent") is True
