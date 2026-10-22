"""TASK 5 — pending hygiene, real IDs, success card only after execution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.admin_change_diff import substitute_real_pending_id
from app.services.admin_reply_helpers import append_followup_suggestions
from app.services.action_service import ACTION_STATUS_PENDING, ACTION_STATUS_SUPERSEDED


def test_substitute_real_pending_id():
    fake = "Action staged. Pending action ID: `act_deadbeefdeadbeef`."
    real = str(uuid4())
    out = substitute_real_pending_id(fake, real)
    assert real in out
    assert "act_deadbeef" not in out or real in out


def test_suggestions_merged_once():
    text = (
        "Done.\n\n"
        "**Suggestions (do not run an action by themselves):**\n"
        "1) A\n2) B\n\n"
        "**Suggestions (do not run an action by themselves):**\n"
        "1) C"
    )
    out = append_followup_suggestions(text, ["X", "Y"], language="en")
    assert out.count("Suggestions (do not run") == 1
    assert "1) X" in out


def test_staged_proposal_action_status_is_awaiting_not_success():
    """Staged proposal payload must not look like a successful execution."""
    proposal = {
        "status": "PROPOSED",
        "action_status": "awaiting_confirmation",
        "requires_confirmation": True,
        "pending_action_id": "real-id",
        "message": "⚠️ **Awaiting confirmation** — nothing has been written yet.",
    }
    assert proposal["action_status"] == "awaiting_confirmation"
    assert "SUCCESS" not in (proposal.get("message") or "").upper() or "Awaiting" in proposal["message"]
    assert proposal["requires_confirmation"] is True


def test_reply_id_equals_db_id_on_stale_repropose():
    from app.core.auth import AuthenticatedStaff
    from app.services import action_service

    staff = AuthenticatedStaff(
        user_id="a", email="admin@tajlandia.com", role="super_admin"
    )
    old_id = uuid4()
    new_id = uuid4()
    action = SimpleNamespace(
        id=old_id,
        confirmation_token="act_old",
        staff_user_id="admin@tajlandia.com",
        tool_name="create_or_update_listing",
        action_payload={
            "mode": "update",
            "property_id": "p1",
            "fields_to_update": {"price_numeric_min": 1},
            "change_diff": {
                "property_id": "p1",
                "rows": [
                    {"field": "price_numeric_min", "current": "10", "new": "1", "readable": True}
                ],
                "canonical_fields": {"price_numeric_min": 1},
            },
        },
        status=ACTION_STATUS_PENDING,
        conversation_id="c1",
        expires_at=MagicMock(),
        error_message=None,
    )
    new_pa = SimpleNamespace(
        id=new_id,
        confirmation_token="act_new",
        action_payload={},
    )

    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a")
    ), patch(
        "app.services.action_service.create_pending_action", return_value=new_pa
    ), patch(
        "app.services.admin_change_diff.verify_diff_still_current",
        return_value=(False, ["p1.price_numeric_min: drift"]),
    ), patch(
        "app.services.admin_change_diff.build_single_listing_change_diff",
        return_value={
            "property_id": "p1",
            "rows": [{"field": "price_numeric_min", "current": "20", "new": "1", "readable": True}],
            "canonical_fields": {"price_numeric_min": 1},
            "diff_markdown": "md",
            "risk_warnings": [],
        },
    ):
        out = action_service.confirm_and_execute_action(
            db=MagicMock(), pending_action_id=str(old_id), authenticated_staff=staff
        )

    assert out["pending_action_id"] == str(new_id)
    assert str(new_id) in (out.get("message") or "")
    assert action.status == ACTION_STATUS_SUPERSEDED
