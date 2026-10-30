"""
Admin Guard Rails B5–B7: batch propose-only, honest audit, strict confirmation parsing.
All fakes — no production Firestore/Postgres writes, no real email/calendar/Drive.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.auth import AuthenticatedStaff
from app.services import action_service
from app.services.action_service import (
    ACTION_STATUS_PENDING,
    ACTION_STATUS_REJECTED,
    create_pending_action,
)
from tests.fake_firestore import audit_docs, patch_firestore, set_store_modes
from app.services.admin_agent import (
    ADMIN_SYSTEM_PROMPT,
    _execute_admin_tool,
    is_strict_confirm_message,
    is_strict_reject_message,
    process_admin_chat,
)
from app.services.audit_service import record_audit_log


@pytest.fixture(autouse=True)
def _firestore_stores_for_file(monkeypatch):
    patch_firestore(monkeypatch)
    set_store_modes(monkeypatch)


def _send_email_success(*_a, **_k):
    return {
        "delivered": True,
        "success": True,
        "completed": True,
        "provider": "gmail",
        "is_mock": False,
    }


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
        allowed_websites=["tajlandia.com", "varsoviaestate.com", "thai.estate"],   # what a super admin account carries
    )


def _fake_pending(**kwargs):
    return SimpleNamespace(
        id=kwargs.get("id", uuid4()),
        confirmation_token=kwargs.get("confirmation_token", "act_testtoken"),
        staff_user_id=kwargs.get("staff_user_id", "admin@tajlandia.com"),
        tool_name=kwargs.get("tool_name", "execute_batch_property_action"),
        action_payload=kwargs.get("action_payload", {}),
        status=kwargs.get("status", ACTION_STATUS_PENDING),
        conversation_id=kwargs.get("conversation_id", "conv-1"),
        expires_at=kwargs.get("expires_at", MagicMock()),
        created_at=kwargs.get("created_at", MagicMock()),
        confirmed_at=None,
        executed_at=None,
        execution_result=None,
        error_message=None,
    )


# ---------------------------------------------------------------------------
# TASK 1 — batch propose only
# ---------------------------------------------------------------------------


def test_system_prompt_never_promises_instant_delete():
    text = ADMIN_SYSTEM_PROMPT.lower()
    assert "execute the deletion immediately" not in text
    assert "removed instantly" not in text
    assert "propose" in text or "confirmation" in text
    assert "pending" in text or "confirm" in text


def test_batch_delete_tool_proposes_no_firestore_mutation(staff, monkeypatch):
    writes = []

    def boom_execute(**kwargs):
        writes.append(kwargs)
        raise AssertionError("execute_batch_property_operation must not run on propose")

    pending = _fake_pending(
        tool_name="execute_batch_property_action",
        action_payload={
            "operation_type": "delete",
            "items": [{"property_id": "prop-a", "name": "Alpha Villa"}],
        },
    )
    created = []

    def fake_create(**kwargs):
        created.append(kwargs)
        return pending

    monkeypatch.setattr(
        "app.services.admin_batch_service.execute_batch_property_operation",
        boom_execute,
    )
    monkeypatch.setattr(
        "app.services.admin_agent.create_pending_action",
        fake_create,
    )
    monkeypatch.setattr(
        "app.services.admin_agent._resolve_batch_targets",
        lambda db, p_ids, update_fields=None, operation_type="delete", **kw: [
            {
                "property_id": "prop-a",
                "name": "Alpha Villa",
                "current": {"status": "Available", "price": "10M"},
                "change": {"operation": "delete"},
            }
        ],
    )

    res, needs_confirm, pid = _execute_admin_tool(
        db=MagicMock(),
        tool_name="execute_batch_property_action",
        tool_args={"operation_type": "delete", "property_ids": ["Alpha Villa"]},
        staff_user=staff,
        conversation_id="conv-1",
    )
    assert writes == []
    assert needs_confirm is True
    assert pid
    assert res.get("requires_confirmation") is True or res.get("status") == "PROPOSED"
    assert created and created[0]["tool_name"] == "execute_batch_property_action"


def test_confirm_batch_applies_listed_items_only(staff, monkeypatch):
    applied = []

    def fake_exec(**kwargs):
        applied.append(kwargs)
        return {
            "success": True,
            "affected_ids": [r["property_id"] for r in kwargs.get("records") or []],
            "affected_count": len(kwargs.get("records") or []),
            "operation_id": "op_1",
        }

    monkeypatch.setattr(
        "app.services.admin_batch_service.execute_batch_property_operation",
        fake_exec,
    )
    action = _fake_pending(
        action_payload={
            "operation_type": "delete",
            "items": [
                {"property_id": "prop-a", "name": "A"},
                {"property_id": "prop-b", "name": "B"},
            ],
            "records": [
                {"property_id": "prop-a", "name": "A"},
                {"property_id": "prop-b", "name": "B"},
            ],
        },
    )
    db = MagicMock()

    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log",
        return_value=SimpleNamespace(id="aud1"),
    ):
        out = action_service.confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            confirmation_token=action.confirmation_token,
        )
    assert out["status"] in ("EXECUTED", action_service.ACTION_STATUS_EXECUTED)
    assert applied and len(applied[0]["records"]) == 2
    assert {r["property_id"] for r in applied[0]["records"]} == {"prop-a", "prop-b"}


def test_reject_batch_changes_nothing(staff, monkeypatch):
    fake = patch_firestore(monkeypatch)
    action = create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="execute_batch_property_action",
        action_payload={
            "operation_type": "delete",
            "records": [{"property_id": "p1", "name": "P1"}],
            "items": [{"property_id": "p1", "name": "P1"}],
        },
        conversation_id="conv-1",
    )
    with patch("app.services.admin_batch_service.execute_batch_property_operation") as ex:
        out = action_service.reject_pending_action(
            db=MagicMock(),
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            reason="cancel",
        )
    assert out["status"] == ACTION_STATUS_REJECTED
    assert not ex.called
    audits = audit_docs(fake)
    assert audits
    rejected = [a for a in audits if a.get("status") == "REJECTED"]
    assert rejected and rejected[-1].get("confirmed_by_staff") is False
    assert fake.docs[("pending_actions", action.id)]["status"] == ACTION_STATUS_REJECTED


def test_wipe_audit_trail_refused(monkeypatch, staff):
    logs = []

    def capture_audit(**kwargs):
        logs.append(kwargs)
        return SimpleNamespace(id="a1")

    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id="c1",
            session_id="s1",
            preferences={"language": "en"},
            message_history=[],
            site_origin="admin",
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    monkeypatch.setattr("app.services.audit_service.record_audit_log", capture_audit)

    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None):
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message="Permanently delete this listing and wipe the audit trail",
            staff_user=staff,
            site_origin="admin",
        )
    text = (res.get("text") or "").lower()
    assert "audit" in text and ("cannot" in text or "can't" in text or "not" in text)
    assert res.get("action_status") != "executed"


def test_email_everyone_asks_clarification_sends_nothing(monkeypatch, staff):
    sent = []

    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id="c1",
            session_id="s1",
            preferences={"language": "en"},
            message_history=[],
            site_origin="admin",
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    monkeypatch.setattr(
        "app.services.google_services.google_manager.send_email",
        lambda *a, **k: sent.append(1) or (_ for _ in ()).throw(AssertionError("no email")),
    )

    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None):
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message="email everyone about the new listing",
            staff_user=staff,
            site_origin="admin",
        )
    text = (res.get("text") or "").lower()
    assert sent == []
    assert any(w in text for w in ("clarif", "who", "recipient", "list", "which", "specify"))


# ---------------------------------------------------------------------------
# TASK 2 — honest audit
# ---------------------------------------------------------------------------


def test_propose_logs_pending_unconfirmed(monkeypatch, staff):
    audits = []

    def capture(**kwargs):
        audits.append(kwargs)
        return SimpleNamespace(id="aud")

    monkeypatch.setattr("app.services.action_service.record_audit_log", capture)
    monkeypatch.setattr(
        "app.services.admin_agent._resolve_batch_targets",
        lambda db, p_ids, update_fields=None, operation_type="delete", **kw: [
            {"property_id": "p1", "name": "P1", "current": {}, "change": {"operation": "delete"}}
        ],
    )

    def fake_create(**kwargs):
        # emulate create_pending_action audit side-effect
        capture(
            staff_user_id=kwargs["staff_user_id"],
            tool_name=kwargs["tool_name"],
            proposed_payload=kwargs["action_payload"],
            confirmed_by_staff=False,
            status=ACTION_STATUS_PENDING,
            pending_action_id="x",
        )
        return _fake_pending(action_payload=kwargs["action_payload"])

    monkeypatch.setattr("app.services.admin_agent.create_pending_action", fake_create)

    _execute_admin_tool(
        db=MagicMock(),
        tool_name="execute_batch_property_action",
        tool_args={"operation_type": "delete", "property_ids": ["p1"]},
        staff_user=staff,
        conversation_id="c",
    )
    assert audits
    assert audits[-1]["confirmed_by_staff"] is False
    assert audits[-1]["status"] == ACTION_STATUS_PENDING


def test_confirm_logs_success_confirmed(staff, monkeypatch):
    fake = patch_firestore(monkeypatch)
    action = create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="execute_batch_property_action",
        action_payload={
            "operation_type": "update",
            "records": [{"property_id": "p1", "name": "P1"}],
            "items": [{"property_id": "p1", "name": "P1"}],
        },
        conversation_id="conv-1",
    )
    with patch(
        "app.services.admin_batch_service.execute_batch_property_operation",
        return_value={"success": True, "affected_ids": ["p1"], "operation_id": "op"},
    ):
        action_service.confirm_and_execute_action(
            db=MagicMock(),
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            confirmation_token=action.confirmation_token,
        )
    audits = audit_docs(fake)
    confirmed = [a for a in audits if a.get("confirmed_by_staff") is True]
    assert confirmed and confirmed[-1].get("status") in ("SUCCESS", "CONFIRMED")
    assert fake.docs[("pending_actions", action.id)]["status"] == "EXECUTED"


def test_reject_logs_rejected_unconfirmed(staff, monkeypatch):
    fake = patch_firestore(monkeypatch)
    action = create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="execute_batch_property_action",
        action_payload={
            "operation_type": "delete",
            "records": [{"property_id": "p1", "name": "P1"}],
            "items": [{"property_id": "p1", "name": "P1"}],
        },
        conversation_id="conv-1",
    )
    action_service.reject_pending_action(
        db=MagicMock(), pending_action_id=str(action.id), authenticated_staff=staff
    )
    audits = audit_docs(fake)
    rejected = [a for a in audits if a.get("status") == "REJECTED"]
    assert rejected and rejected[-1].get("confirmed_by_staff") is False


def test_failed_tool_logs_failed(staff):
    action = _fake_pending(
        action_payload={"operation_type": "delete", "records": [{"property_id": "p1"}], "items": []}
    )
    audits = []

    def capture(**kwargs):
        audits.append(kwargs)
        return SimpleNamespace(id="aud")

    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", side_effect=capture
    ), patch(
        "app.services.admin_batch_service.execute_batch_property_operation",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(Exception):
            action_service.confirm_and_execute_action(
                db=MagicMock(), pending_action_id=str(action.id), authenticated_staff=staff, confirmation_token=action.confirmation_token
            )
    failed = [a for a in audits if a.get("status") == "FAILED"]
    assert failed


def test_record_audit_strips_secrets():
    db = MagicMock()
    entry = record_audit_log(
        db=db,
        staff_user_id="admin@tajlandia.com",
        tool_name="send_email",
        tool_arguments={"api_key": "sk-secret", "to": "a@b.com"},
        confirmed_by_staff=False,
        status="PENDING",
    )
    # When ENABLE_AUDIT_LOGGING is False, returns ephemeral with sanitized fields set on object
    assert entry.tool_arguments.get("api_key") == "[REDACTED_SECRET]"
    assert entry.tool_arguments.get("to") == "a@b.com"


# ---------------------------------------------------------------------------
# TASK 3 — strict confirmation parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg,expect",
    [
        ("yes", True),
        ("confirm", True),
        ("yes, proceed", True),
        ("tak", True),
        ("zatwierdzam", True),
        ("yes show me the leads", False),
        ("don't confirm", False),
        ("do not proceed", False),
        ("please confirm after you show me leads", False),
        ("", False),
    ],
)
def test_strict_confirm_message_rules(msg, expect):
    assert is_strict_confirm_message(msg) is expect


@pytest.mark.parametrize(
    "msg,expect",
    [
        ("no", True),
        ("cancel", True),
        ("don't confirm", True),
        ("reject", True),
        ("nie", True),
        ("yes show me the leads", False),
    ],
)
def test_strict_reject_message_rules(msg, expect):
    assert is_strict_reject_message(msg) is expect


def test_yes_show_me_leads_does_not_confirm(monkeypatch, staff):
    pending = _fake_pending()
    confirmed = []

    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id="conv-1", session_id="s1", preferences={"language": "en"}, message_history=[], site_origin="admin"
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    monkeypatch.setattr(
        "app.services.action_service.confirm_and_execute_action",
        lambda **k: confirmed.append(1) or {},
    )

    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.order_by.return_value.first.return_value = pending
    q.filter.return_value.order_by.return_value.all.return_value = [pending]
    db.query.return_value = q

    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None), patch(
        "app.services.admin_agent._run_deterministic_admin",
        return_value={
            "type": "text",
            "text": "Here are the leads… (pending action still waiting for confirmation)",
            "requires_confirmation": False,
            "pending_action_id": None,
            "action_status": None,
            "tools_used": [],
            "session_id": "s1",
            "conversation_id": "c1",
            "language": "en",
            "download_link": None,
        },
    ), patch(
        "app.services.admin_agent.AIIntentClassifier.classify",
        return_value=SimpleNamespace(
            primary_intent="query",
            document_requested=False,
            negative_feedback=False,
            confidence=1.0,
        ),
    ), patch(
        "app.services.admin_agent.rewrite_admin_query",
        side_effect=lambda **k: k.get("current_message") or "yes show me the leads",
    ):
        res = process_admin_chat(
            db=db,
            session_id="s1",
            message="yes show me the leads",
            staff_user=staff,
        )
    assert confirmed == []
    # Pending still waiting note preferred
    text = (res.get("text") or "").lower()
    assert res.get("action_status") != "executed"


def test_bare_yes_confirms_single_pending(monkeypatch, staff):
    fake = patch_firestore(monkeypatch)
    monkeypatch.setattr("app.services.action_service._execute_send_email", _send_email_success)
    conv_id = "conv-1"
    action = create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="send_email",
        action_payload={"to": "a@b.com", "subject": "Hi", "body": "x"},
        conversation_id=conv_id,
    )
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id=conv_id, session_id="s1", preferences={"language": "en"}, message_history=[], site_origin="admin"
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))

    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None):
        res = process_admin_chat(db=MagicMock(), session_id="s1", message="yes", staff_user=staff)
    assert fake.docs[("pending_actions", action.id)]["status"] == "EXECUTED"
    assert res.get("action_status") == "executed"


def test_two_pending_yes_asks_which(monkeypatch, staff):
    fake = patch_firestore(monkeypatch)
    conv_id = "conv-1"
    create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="send_email",
        action_payload={"to": "a@b.com", "subject": "One", "body": "x"},
        conversation_id=conv_id,
    )
    create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="send_email",
        action_payload={"to": "b@b.com", "subject": "Two", "body": "y"},
        conversation_id=conv_id,
    )
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id=conv_id, session_id="s1", preferences={"language": "en"}, message_history=[], site_origin="admin"
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))

    with patch("app.services.action_service.confirm_and_execute_action") as conf, patch(
        "app.services.admin_agent.settings.ANTHROPIC_API_KEY", None
    ):
        res = process_admin_chat(db=MagicMock(), session_id="s1", message="yes", staff_user=staff)
    assert not conf.called
    assert "which" in (res.get("text") or "").lower()
    pending_open = [
        d
        for path, d in fake.docs.items()
        if path[0] == "pending_actions" and d.get("status") == ACTION_STATUS_PENDING
    ]
    assert len(pending_open) == 2


def test_dont_confirm_rejects(monkeypatch, staff):
    fake = patch_firestore(monkeypatch)
    conv_id = "conv-1"
    action = create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="send_email",
        action_payload={"to": "a@b.com", "subject": "Hi", "body": "x"},
        conversation_id=conv_id,
    )
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id=conv_id, session_id="s1", preferences={"language": "en"}, message_history=[], site_origin="admin"
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))

    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None):
        res = process_admin_chat(
            db=MagicMock(), session_id="s1", message="don't confirm", staff_user=staff
        )
    assert fake.docs[("pending_actions", action.id)]["status"] == ACTION_STATUS_REJECTED
    assert res.get("action_status") == "rejected"


def test_action_confirm_button_parameter_works(monkeypatch, staff):
    pending = _fake_pending()
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id="conv-1", session_id="s1", preferences={"language": "en"}, message_history=[], site_origin="admin"
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    monkeypatch.setattr(
        "app.services.action_service.get_pending_action",
        lambda db, pid: pending,
    )

    with patch(
        "app.services.action_service.confirm_and_execute_action",
        return_value={"status": "EXECUTED", "execution_result": {}},
    ) as conf, patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None):
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message=None,
            staff_user=staff,
            action="confirm",
            pending_action_id=str(pending.id),
        )
    assert conf.called
    assert res.get("action_status") == "executed"
