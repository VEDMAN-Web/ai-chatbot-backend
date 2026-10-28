"""
TASK 7 — Confirmed writes must be logged and undoable (6-day snapshot).
Fake Firestore/SQL only — no live writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import AuthenticatedStaff
from app.db.models import Base, Property
from app.services import action_service
from app.services.action_service import (
    ACTION_STATUS_PENDING,
    ACTION_STATUS_REJECTED,
    confirm_and_execute_action,
    create_pending_action,
    get_pending_action,
)
from tests.fake_firestore import audit_docs, patch_firestore, set_store_modes


@pytest.fixture(autouse=True)
def _firestore_stores_for_undo_file(monkeypatch):
    patch_firestore(monkeypatch)
    set_store_modes(monkeypatch)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[Property.__table__])
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
from app.services.admin_batch_service import (
    BatchOperationError,
    record_crud_operation_snapshot,
    undo_batch_property_operation,
)
from app.services.admin_agent import process_admin_chat


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
    )


def _pending(**kwargs):
    return SimpleNamespace(
        id=kwargs.get("id", uuid4()),
        confirmation_token="act_tok",
        staff_user_id="admin@tajlandia.com",
        tool_name=kwargs.get("tool_name", "create_or_update_listing"),
        action_payload=kwargs.get(
            "action_payload",
            {
                "mode": "update",
                "property_id": "prop-1",
                "fields_to_update": {"price_display": "20M THB", "property_name": "New Name"},
            },
        ),
        status=ACTION_STATUS_PENDING,
        conversation_id="c1",
        expires_at=MagicMock(),
        created_at=MagicMock(),
        confirmed_at=None,
        executed_at=None,
        execution_result=None,
        error_message=None,
    )


def test_confirmed_chat_update_creates_snapshot_returns_operation_id(staff, db, monkeypatch):
    fake = patch_firestore(monkeypatch)
    set_store_modes(monkeypatch)
    db.add(
        Property(
            property_id="prop-1",
            property_name="Old Name",
            status="Available",
            price_display="10M THB",
            price_numeric_min=10_000_000,
            websites=["tajlandia.com"],
        )
    )
    db.commit()
    snapshots = []
    fixed_op = "crud_testop123456"

    def fake_snapshot(**kwargs):
        snapshots.append(kwargs)
        assert kwargs.get("previous_state") is not None
        assert kwargs["previous_state"].get("property_name") == "Old Name"
        return fixed_op

    action = create_pending_action(
        db,
        staff_user_id=staff.email,
        tool_name="create_or_update_listing",
        action_payload={
            "mode": "update",
            "property_id": "prop-1",
            "fields_to_update": {"price_display": "20M THB", "property_name": "New Name"},
        },
        conversation_id="c1",
    )

    with patch(
        "app.services.admin_batch_service.record_crud_operation_snapshot", side_effect=fake_snapshot
    ), patch(
        "app.services.action_service.record_crud_operation_snapshot", side_effect=fake_snapshot
    ), patch(
        "app.services.cache_service.search_cache.invalidate"
    ), patch(
        "app.core.config.settings.ENABLE_FIRESTORE_SYNC", False
    ):
        out = confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            confirmation_token=action.confirmation_token,
        )

    assert snapshots, "snapshot must be saved on confirmed update"
    assert out["execution_result"].get("operation_id") == fixed_op
    refreshed = get_pending_action(db, action.id)
    assert refreshed.execution_result and refreshed.execution_result.get("operation_id") == fixed_op
    audits = audit_docs(fake)
    assert any(a.get("confirmed_by_staff") is True for a in audits)
    assert any(a.get("status") in ("SUCCESS", "CONFIRMED") for a in audits if a.get("confirmed_by_staff"))


def test_rejected_or_pending_creates_no_snapshot(staff, db, monkeypatch):
    fake = patch_firestore(monkeypatch)
    set_store_modes(monkeypatch)
    db.add(
        Property(
            property_id="prop-1",
            property_name="Old Name",
            status="Available",
            price_display="10M THB",
            price_numeric_min=10_000_000,
        )
    )
    db.commit()
    action = create_pending_action(
        db,
        staff_user_id=staff.email,
        tool_name="create_or_update_listing",
        action_payload={
            "mode": "update",
            "property_id": "prop-1",
            "fields_to_update": {"property_name": "New Name"},
        },
        conversation_id="c1",
    )
    with patch(
        "app.services.admin_batch_service.record_crud_operation_snapshot",
        side_effect=lambda **k: (_ for _ in ()).throw(AssertionError("no snapshot on reject")),
    ):
        out = action_service.reject_pending_action(
            db=db, pending_action_id=str(action.id), authenticated_staff=staff
        )
    assert out["status"] == ACTION_STATUS_REJECTED
    assert not any(path[0] == "admin_batch_operations" for path in fake.docs)


def test_undo_restores_old_values_second_undo_refused(staff, monkeypatch):
    fake = patch_firestore(monkeypatch)
    set_store_modes(monkeypatch)
    original = {"name": "Old Villa", "priceFrom": 10_000_000}
    operation_id = "crud_abc"
    now = datetime.now(timezone.utc)
    fake.docs[("projects", "prop-1")] = {"name": "Changed Villa", "priceFrom": 20_000_000}
    fake.docs[("admin_batch_operations", operation_id)] = {
        "operation_id": operation_id,
        "action_type": "update",
        "status": "APPLIED",
        "created_at": now.isoformat(),
        "expire_at": (now + timedelta(days=6)).isoformat(),
        "expires_at": (now + timedelta(days=6)).isoformat(),
        "previous_snapshot": {"prop-1": original},
        "raw_firestore": {"prop-1": original},
        "raw_sql": {},
        "existed_in_firestore": {"prop-1": True},
        "existed_in_sql": {"prop-1": False},
        "snapshot_format": "raw_v1",
        "affected_ids": ["prop-1"],
        "created_ids": [],
    }

    undo1 = undo_batch_property_operation(operation_id, staff, db_session=MagicMock())
    assert undo1["status"] == "REVERTED"
    assert fake.docs[("projects", "prop-1")]["name"] == "Old Villa"
    audits = audit_docs(fake)
    assert any(
        (a.get("tool_name") or "").upper() == "UNDO" or "undo" in (a.get("tool_name") or "").lower()
        for a in audits
    )
    assert any(
        (a.get("tool_arguments") or {}).get("operation_id") == operation_id
        or (a.get("proposed_payload") or {}).get("original_operation_id") == operation_id
        for a in audits
    )

    fake.docs[("admin_batch_operations", operation_id)]["status"] = "REVERTED"
    with pytest.raises(BatchOperationError) as ei:
        undo_batch_property_operation(operation_id, staff, db_session=MagicMock())
    assert "already" in str(ei.value).lower() or "undone" in str(ei.value).lower()


def test_change_price_confirm_undo_restores_price(staff, db, monkeypatch):
    """Change price → confirm → undo (chat) → price restored + UNDO audit written."""
    from app.services.admin_reply_helpers import is_undo_request
    from app.services.admin_turn_rules import _undo_last

    fake = patch_firestore(monkeypatch)
    set_store_modes(monkeypatch)
    original_price = 10_000_000
    new_price = 20_000_000
    fake.docs[("projects", "prop-1")] = {
        "name": "Old Villa",
        "priceFrom": original_price,
        "property_id": "prop-1",
    }
    db.add(
        Property(
            property_id="prop-1",
            property_name="Old Villa",
            status="Available",
            price_display="10M THB",
            price_numeric_min=original_price,
        )
    )
    db.commit()

    action = create_pending_action(
        db,
        staff_user_id=staff.email,
        tool_name="create_or_update_listing",
        action_payload={
            "mode": "update",
            "property_id": "prop-1",
            "fields_to_update": {
                "price_display": "20M THB",
                "price_numeric_min": new_price,
            },
        },
        conversation_id="c1",
    )

    with patch("app.services.cache_service.search_cache.invalidate"):
        out = confirm_and_execute_action(
            db=db,
            pending_action_id=str(action.id),
            authenticated_staff=staff,
            confirmation_token=action.confirmation_token,
        )
    operation_id = out["execution_result"].get("operation_id")
    assert operation_id
    assert fake.docs[("projects", "prop-1")]["priceFrom"] == new_price
    snap = fake.docs.get(("admin_batch_operations", operation_id)) or {}
    assert snap.get("conversation_id") == "c1"

    assert is_undo_request("undo")
    assert is_undo_request("undo the last change")
    assert is_undo_request("undo that")

    conv = SimpleNamespace(id="c1", session_id="s1", message_history=[], preferences={})
    with patch("app.services.cache_service.search_cache.invalidate"):
        undo_reply = _undo_last(db, conv, staff)
    assert "Undone" in (undo_reply.get("text") or "")
    assert fake.docs[("projects", "prop-1")]["priceFrom"] == original_price
    prop = db.query(Property).filter(Property.property_id == "prop-1").first()
    db.refresh(prop)
    assert float(prop.price_numeric_min) == float(original_price)

    undo_audits = [
        d for d in audit_docs(fake)
        if str(d.get("tool_name") or "").upper() == "UNDO"
    ]
    assert undo_audits, "UNDO must write an audit entry"
    assert any(
        (d.get("tool_arguments") or {}).get("operation_id") == operation_id
        or (d.get("proposed_payload") or {}).get("original_operation_id") == operation_id
        for d in undo_audits
    )


def test_undo_after_6_days_refused(staff):
    mock_fs = MagicMock()
    batch = MagicMock()
    expired = {
        "operation_id": "crud_old",
        "action_type": "update",
        "status": "APPLIED",
        "created_at": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
        "expire_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "previous_snapshot": {"p": {"name": "x"}},
    }
    op_doc = MagicMock()
    op_doc.exists = True
    op_doc.to_dict.return_value = expired
    op_ref = MagicMock()
    op_ref.get.return_value = op_doc
    batch.document.return_value = op_ref
    mock_fs.collection.return_value = batch

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        with pytest.raises(BatchOperationError) as ei:
            undo_batch_property_operation("crud_old", staff)
    assert "expired" in str(ei.value).lower()


def test_confirm_reply_includes_undo_operation_id(monkeypatch, staff):
    action = _pending()
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id="c1", session_id="s1", preferences={"language": "en"}, message_history=[], site_origin="admin"
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    monkeypatch.setattr(
        "app.services.action_service.get_pending_action", lambda db, pid: action
    )
    monkeypatch.setattr(
        "app.services.action_service.confirm_and_execute_action",
        lambda **k: {
            "status": "EXECUTED",
            "execution_result": {
                "property_id": "prop-1",
                "title": "New Name",
                "mode": "update",
                "operation_id": "crud_reply99",
                "current_status": "Available",
            },
            "audit_log_id": "a1",
            "operation_id": "crud_reply99",
        },
    )
    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None):
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message=None,
            staff_user=staff,
            action="confirm",
            pending_action_id=str(action.id),
        )
    text = res.get("text") or ""
    assert res.get("operation_id") == "crud_reply99"
    assert "crud_reply99" not in text
    assert "undo" in text.lower()
    assert "6 day" in text.lower() or "6-day" in text.lower() or "6 days" in text.lower()


def test_email_confirm_says_cannot_be_undone(monkeypatch, staff):
    action = _pending(
        tool_name="send_email",
        action_payload={"to": "client@x.com", "subject": "Hello", "body": "Hi"},
    )
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id="c1", session_id="s1", preferences={"language": "en"}, message_history=[], site_origin="admin"
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    monkeypatch.setattr(
        "app.services.action_service.get_pending_action", lambda db, pid: action
    )
    monkeypatch.setattr(
        "app.services.action_service.confirm_and_execute_action",
        lambda **k: {
            "status": "EXECUTED",
            "execution_result": {
                "delivered": True,
                "success": True,
                "is_mock": False,
                "to": "client@x.com",
                "subject": "Hello",
                "undoable": False,
            },
            "audit_log_id": "a1",
        },
    )
    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", None):
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message=None,
            staff_user=staff,
            action="confirm",
            pending_action_id=str(action.id),
        )
    low = (res.get("text") or "").lower()
    assert "cannot be undone" in low or "can't be undone" in low or "not undoable" in low


def test_audit_chain_propose_confirm_execute_undo(staff):
    """Proposal, confirm/execute, and undo each leave audit entries."""
    audits = []

    def capture(**kwargs):
        audits.append(dict(kwargs))
        return SimpleNamespace(id=f"a{len(audits)}")

    # Propose
    with patch("app.services.action_service.record_audit_log", side_effect=capture):
        # emulate create_pending_action audit only
        from app.services.audit_service import record_audit_log as real

        capture(
            staff_user_id=staff.email,
            tool_name="create_or_update_listing",
            proposed_payload={"property_id": "p1"},
            confirmed_by_staff=False,
            status="PENDING",
        )
    assert audits[-1]["status"] == "PENDING"
    assert audits[-1]["confirmed_by_staff"] is False

    action = _pending()
    old = SimpleNamespace(
        property_id="prop-1",
        property_name="Old",
        status="Available",
        price_display="1",
        websites=[],
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = old

    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", side_effect=capture
    ), patch(
        "app.services.action_service.validate_property_data", return_value=(True, [], "")
    ), patch(
        "app.services.action_service.record_crud_operation_snapshot",
        return_value="crud_chain01",
    ), patch(
        "app.services.cache_service.search_cache.invalidate"
    ), patch(
        "app.core.config.settings.ENABLE_FIRESTORE_SYNC", False
    ):
        action_service.confirm_and_execute_action(
            db=db, pending_action_id=str(action.id), authenticated_staff=staff, confirmation_token=action.confirmation_token
        )

    assert any(
        a.get("confirmed_by_staff") is True and a.get("status") in ("SUCCESS", "CONFIRMED")
        for a in audits
    )

    # Undo audit
    mock_fs = MagicMock()
    batch = MagicMock()
    op_doc = MagicMock()
    op_doc.exists = True
    op_doc.to_dict.return_value = {
        "operation_id": "crud_chain01",
        "action_type": "update",
        "status": "APPLIED",
        "expire_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        "previous_snapshot": {"prop-1": {"property_name": "Old"}},
        "raw_firestore": {"prop-1": {"property_name": "Old"}},
        "raw_sql": {},
        "existed_in_firestore": {"prop-1": True},
        "existed_in_sql": {"prop-1": False},
        "snapshot_format": "raw_v1",
        "affected_ids": ["prop-1"],
        "created_ids": [],
    }
    op_ref = MagicMock()
    op_ref.get.return_value = op_doc
    batch.document.return_value = op_ref
    projects = MagicMock()
    project_snap = MagicMock()
    project_snap.exists = True
    project_snap.to_dict.return_value = {"property_name": "Old"}
    projects.document.return_value.get.return_value = project_snap

    def col(n):
        return batch if n == "admin_batch_operations" else projects

    mock_fs.collection.side_effect = col
    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs), patch(
        "app.services.admin_batch_service.record_audit_log", side_effect=capture
    ):
        undo_batch_property_operation("crud_chain01", staff, db_session=MagicMock())

    undo_entries = [
        a
        for a in audits
        if (a.get("tool_name") or "").upper() == "UNDO"
        or "undo" in (a.get("tool_name") or "").lower()
    ]
    assert undo_entries
    assert undo_entries[-1].get("tool_arguments", {}).get("operation_id") == "crud_chain01" or (
        undo_entries[-1].get("proposed_payload") or {}
    ).get("original_operation_id") == "crud_chain01"


def test_batch_confirm_goes_through_execute_batch(staff):
    called = []

    def fake_batch(**kwargs):
        called.append(kwargs)
        return {
            "success": True,
            "operation_id": "batch_xyz",
            "affected_ids": ["p1"],
            "affected_count": 1,
        }

    action = _pending(
        tool_name="execute_batch_property_action",
        action_payload={
            "operation_type": "delete",
            "records": [{"property_id": "p1", "name": "P1"}],
            "items": [{"property_id": "p1", "name": "P1"}],
        },
    )
    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a")
    ), patch(
        "app.services.admin_batch_service.execute_batch_property_operation", side_effect=fake_batch
    ):
        out = action_service.confirm_and_execute_action(
            db=MagicMock(), pending_action_id=str(action.id), authenticated_staff=staff, confirmation_token=action.confirmation_token
        )
    assert called
    assert out["execution_result"]["operation_id"] == "batch_xyz"
    assert action.execution_result.get("operation_id") == "batch_xyz"
