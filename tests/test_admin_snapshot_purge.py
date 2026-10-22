"""
TASK 2 — Admin undo snapshots: expire_at retention + purge_expired_snapshots.
Fakes only — no live Firestore writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.auth import AuthenticatedStaff
from app.core.config import settings
from app.services.admin_batch_service import (
    BatchOperationError,
    EXPIRED_UNDO_MESSAGE,
    execute_batch_property_operation,
    get_batch_operations_history,
    get_snapshot_retention_days,
    purge_expired_snapshots,
    record_crud_operation_snapshot,
    undo_batch_property_operation,
    _compute_expire_at,
)


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
    )


def test_retention_config_default_six():
    assert get_snapshot_retention_days() == getattr(settings, "ADMIN_SNAPSHOT_RETENTION_DAYS", 6)
    assert get_snapshot_retention_days() == 6


def test_expire_at_is_six_days_ahead_on_batch_snapshot(staff):
    mock_fs = MagicMock()
    projects = MagicMock()
    batch = MagicMock()
    saved = {}

    doc = MagicMock()
    doc.exists = True
    doc.to_dict.return_value = {"name": "Villa", "priceFrom": 1}
    projects.document.return_value.get.return_value = doc
    projects.document.return_value.update = MagicMock()

    def col(name):
        return projects if name == "projects" else batch

    mock_fs.collection.side_effect = col
    batch.document.return_value.set.side_effect = lambda payload: saved.update(payload)

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        res = execute_batch_property_operation(
            action_type="update",
            records=[{"property_id": "PROP-1", "priceFrom": 2}],
            staff_user=staff,
            source_filename="t.xlsx",
        )

    assert "expire_at" in saved
    created = datetime.fromisoformat(saved["created_at"])
    expire = datetime.fromisoformat(saved["expire_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=timezone.utc)
    assert (expire - created).days == 6
    assert res["expire_at"] == saved["expire_at"]
    assert res.get("expires_at") == saved["expire_at"]


def test_expire_at_on_crud_snapshot(staff):
    mock_fs = MagicMock()
    batch = MagicMock()
    saved = {}
    mock_fs.collection.return_value = batch
    batch.document.return_value.set.side_effect = lambda payload: saved.update(payload)

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        op_id = record_crud_operation_snapshot(
            action_type="update",
            property_id="p1",
            previous_state={"property_name": "Old"},
            staff_user=staff,
            confirmed_by_staff=True,
        )
    assert op_id
    created = datetime.fromisoformat(saved["created_at"])
    expire = datetime.fromisoformat(saved["expire_at"])
    assert (_compute_expire_at(created) - expire).total_seconds() < 2


def test_purge_removes_only_expired_docs():
    now = datetime.now(timezone.utc)
    expired_payload = {
        "operation_id": "op_old",
        "status": "APPLIED",
        "expire_at": (now - timedelta(days=1)).isoformat(),
        "created_at": (now - timedelta(days=7)).isoformat(),
    }
    fresh_payload = {
        "operation_id": "op_new",
        "status": "APPLIED",
        "expire_at": (now + timedelta(days=5)).isoformat(),
        "created_at": now.isoformat(),
    }

    expired_doc = MagicMock()
    expired_doc.id = "op_old"
    expired_doc.to_dict.return_value = expired_payload
    expired_doc.reference.delete = MagicMock()

    fresh_doc = MagicMock()
    fresh_doc.id = "op_new"
    fresh_doc.to_dict.return_value = fresh_payload
    fresh_doc.reference.delete = MagicMock()

    col = MagicMock()
    # Query path may fail → scan path
    col.where.return_value.limit.return_value.stream.side_effect = RuntimeError("no index")
    col.limit.return_value.stream.return_value = [expired_doc, fresh_doc]

    mock_fs = MagicMock()
    mock_fs.collection.return_value = col

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        deleted = purge_expired_snapshots(now=now)

    assert deleted == 1
    expired_doc.reference.delete.assert_called_once()
    fresh_doc.reference.delete.assert_not_called()


def test_purge_never_raises_when_firestore_fails():
    with patch(
        "app.services.admin_batch_service.get_firestore_client",
        side_effect=RuntimeError("firestore down"),
    ):
        assert purge_expired_snapshots() == 0

    with patch(
        "app.services.admin_batch_service.get_firestore_client",
        return_value=None,
    ):
        assert purge_expired_snapshots() == 0


def test_undo_of_expired_op_refused(staff):
    now = datetime.now(timezone.utc)
    expired = {
        "operation_id": "op_exp",
        "action_type": "update",
        "status": "APPLIED",
        "expire_at": (now - timedelta(hours=1)).isoformat(),
        "created_at": (now - timedelta(days=7)).isoformat(),
        "previous_snapshot": {"p1": {"name": "x"}},
    }
    op_doc = MagicMock()
    op_doc.exists = True
    op_doc.to_dict.return_value = expired
    op_ref = MagicMock()
    op_ref.get.return_value = op_doc
    mock_fs = MagicMock()
    mock_fs.collection.return_value.document.return_value = op_ref

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        with pytest.raises(BatchOperationError) as ei:
            undo_batch_property_operation("op_exp", staff)
    assert str(ei.value) == EXPIRED_UNDO_MESSAGE


def test_second_undo_refused(staff):
    saved = {
        "operation_id": "op_done",
        "action_type": "update",
        "status": "REVERTED",
        "expire_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        "previous_snapshot": {"p1": {"name": "x"}},
    }
    op_doc = MagicMock()
    op_doc.exists = True
    op_doc.to_dict.return_value = saved
    mock_fs = MagicMock()
    mock_fs.collection.return_value.document.return_value.get.return_value = op_doc

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        with pytest.raises(BatchOperationError) as ei:
            undo_batch_property_operation("op_done", staff)
    assert "already" in str(ei.value).lower() or "undone" in str(ei.value).lower()


def test_history_hides_expired_ops():
    now = datetime.now(timezone.utc)
    expired = {
        "operation_id": "old",
        "status": "APPLIED",
        "action_type": "delete",
        "expire_at": (now - timedelta(days=1)).isoformat(),
        "created_at": (now - timedelta(days=8)).isoformat(),
        "affected_count": 1,
    }
    active = {
        "operation_id": "new",
        "status": "APPLIED",
        "action_type": "update",
        "expire_at": (now + timedelta(days=4)).isoformat(),
        "created_at": now.isoformat(),
        "affected_count": 2,
        "affected_ids": ["a"],
    }
    d1 = MagicMock()
    d1.to_dict.return_value = expired
    d1.id = "old"
    d2 = MagicMock()
    d2.to_dict.return_value = active
    d2.id = "new"

    col = MagicMock()
    col.limit.return_value.stream.return_value = [d1, d2]
    mock_fs = MagicMock()
    mock_fs.collection.return_value = col

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        hist = get_batch_operations_history(limit=50)

    ids = [h["operation_id"] for h in hist]
    assert "old" not in ids
    assert "new" in ids
    assert hist[0]["can_undo"] is True


def test_maintenance_purge_endpoint_registered():
    from app.api.v1.admin import router

    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/admin/maintenance/purge-expired" in paths or any(
        "purge-expired" in (getattr(r, "path", "") or "") for r in router.routes
    )
