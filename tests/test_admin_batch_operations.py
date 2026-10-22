import io
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.core.auth import AuthenticatedStaff
from app.services.admin_batch_service import (
    parse_property_batch_file,
    execute_batch_property_operation,
    undo_batch_property_operation,
    get_batch_operations_history,
    BatchOperationError,
    SNAPSHOT_RETENTION_DAYS,
    EXPIRED_UNDO_MESSAGE,
)


@pytest.fixture
def mock_staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin"
    )


def test_parse_csv_file():
    csv_content = b"property_id,name,price,status\nPROP-TEST-001,Cozy Villa,15000000,Ready\nPROP-TEST-002,Sunset Hills,25000000,Under Construction"
    records = parse_property_batch_file(csv_content, "properties.csv")
    assert len(records) == 2
    assert records[0]["property_id"] == "PROP-TEST-001"
    assert records[0]["name"] == "Cozy Villa"
    assert records[1]["property_id"] == "PROP-TEST-002"


def test_parse_excel_file():
    import pandas as pd
    df = pd.DataFrame([
        {"id": "PROP-PHK-001", "title": "9D Cozy Villas", "price": 14900000},
        {"id": "PROP-PHK-002", "title": "Above Element Villas", "price": 39990000}
    ])
    out = io.BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)

    records = parse_property_batch_file(out.read(), "listings.xlsx")
    assert len(records) == 2
    assert records[0]["property_id"] == "PROP-PHK-001"
    assert records[0]["name"] == "9D Cozy Villas"
    assert records[1]["property_id"] == "PROP-PHK-002"


def test_execute_batch_and_undo_flow(mock_staff):
    mock_fs = MagicMock()
    mock_projects_col = MagicMock()
    mock_batch_col = MagicMock()

    # Mock project document
    doc_snapshot = {
        "name": "Original Villa Name",
        "priceFrom": 10000000,
        "isActive": True
    }
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = doc_snapshot
    mock_doc_ref = MagicMock()
    mock_doc_ref.get.return_value = mock_doc
    mock_projects_col.document.return_value = mock_doc_ref

    def collection_side_effect(name):
        if name == "projects":
            return mock_projects_col
        elif name == "admin_batch_operations":
            return mock_batch_col
        return MagicMock()

    mock_fs.collection.side_effect = collection_side_effect

    saved_batch_doc = {}
    def mock_batch_set(payload):
        nonlocal saved_batch_doc
        saved_batch_doc = dict(payload)

    mock_batch_doc_ref = MagicMock()
    mock_batch_doc_ref.set.side_effect = mock_batch_set
    mock_batch_col.document.return_value = mock_batch_doc_ref

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        # 1. Execute batch update
        records = [{"property_id": "PROP-TEST-001", "priceFrom": 18000000}]
        res = execute_batch_property_operation(
            action_type="update",
            records=records,
            staff_user=mock_staff,
            source_filename="updates.xlsx"
        )

        assert res["success"] is True
        assert res["affected_count"] == 1
        assert res["undo_window_days"] == 6
        op_id = res["operation_id"]

        # Verify snapshot was stored with 6 days expiration (expire_at + legacy expires_at)
        assert saved_batch_doc["operation_id"] == op_id
        assert saved_batch_doc["status"] == "APPLIED"
        assert saved_batch_doc["previous_snapshot"]["PROP-TEST-001"] == doc_snapshot
        assert "expire_at" in saved_batch_doc
        created_at = datetime.fromisoformat(saved_batch_doc["created_at"])
        expire_at = datetime.fromisoformat(saved_batch_doc["expire_at"])
        assert (expire_at - created_at).days == SNAPSHOT_RETENTION_DAYS
        assert saved_batch_doc.get("expires_at") == saved_batch_doc["expire_at"]

        # Verify update was instantly applied to the project doc
        mock_doc_ref.update.assert_called()

        # 2. Undo the batch operation
        mock_saved_op_doc = MagicMock()
        mock_saved_op_doc.exists = True
        mock_saved_op_doc.to_dict.return_value = saved_batch_doc
        mock_batch_doc_ref.get.return_value = mock_saved_op_doc

        undo_res = undo_batch_property_operation(
            operation_id=op_id,
            staff_user=mock_staff
        )

        assert undo_res["success"] is True
        assert undo_res["status"] == "REVERTED"
        assert undo_res["restored_count"] == 1

        # Verify original snapshot was restored to projects document
        mock_doc_ref.set.assert_called_with(doc_snapshot)


def test_undo_rejected_after_expiration(mock_staff):
    mock_fs = MagicMock()
    mock_batch_col = MagicMock()

    # Expired 7 days ago
    expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    created_at = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    expired_batch_doc = {
        "operation_id": "batch-expired-01",
        "action_type": "delete",
        "status": "APPLIED",
        "created_at": created_at,
        "expire_at": expired_at,
        "expires_at": expired_at,
        "previous_snapshot": {"PROP-001": {"name": "Test"}}
    }

    mock_op_doc = MagicMock()
    mock_op_doc.exists = True
    mock_op_doc.to_dict.return_value = expired_batch_doc
    mock_op_ref = MagicMock()
    mock_op_ref.get.return_value = mock_op_doc
    mock_batch_col.document.return_value = mock_op_ref

    mock_fs.collection.return_value = mock_batch_col

    with patch("app.services.admin_batch_service.get_firestore_client", return_value=mock_fs):
        with pytest.raises(BatchOperationError) as exc_info:
            undo_batch_property_operation("batch-expired-01", mock_staff)

        # Deliberate copy change: expired undo uses fixed retention phrase
        assert str(exc_info.value) == EXPIRED_UNDO_MESSAGE
