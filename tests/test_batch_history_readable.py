"""Bug 14: the Undo / batch history must say which listings changed and how, not only raw operation ids."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.auth import AuthenticatedStaff
from app.services import admin_batch_service as abs_mod
from app.services.admin_batch_service import (
    BATCH_OPERATIONS_COLLECTION,
    execute_batch_property_operation,
    get_batch_operations_history,
    record_crud_operation_snapshot,
)

SITE = "tajlandia.com"


@pytest.fixture
def fs(_default_fake_firestore, monkeypatch):
    fake = _default_fake_firestore
    # admin_batch_service binds get_firestore_client at import time
    monkeypatch.setattr(abs_mod, "get_firestore_client", lambda *a, **k: fake)
    return fake


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
        allowed_websites=[SITE],
        selected_websites=[SITE],
    )


def _seed(fake, pid, name, price, **extra):
    doc = {"name": name, "priceFrom": price, "isActive": True, "status": "Ready", "websites": [SITE]}
    doc.update(extra)
    fake.collection("projects").document(pid).set(doc)


def _only_entry(staff):
    hist = get_batch_operations_history(limit=10, staff_user=staff)
    assert len(hist) == 1, hist
    return hist[0]


def test_batch_update_history_has_names_summary_and_changes(fs, staff):
    _seed(fs, "elite-chalong", "Elite Chalong", 10_000_000)
    _seed(fs, "ozone-campus", "Ozone Campus", 8_000_000)
    _seed(fs, "the-third", "The Third Place", 6_000_000)

    res = execute_batch_property_operation(
        "update",
        [
            {"property_id": "elite-chalong", "priceFrom": 11_000_000},
            {"property_id": "ozone-campus", "priceFrom": 9_000_000},
            {"property_id": "the-third", "priceFrom": 7_000_000},
        ],
        staff_user=staff,
        source_filename="prices.xlsx",
    )
    entry = _only_entry(staff)

    # existing fields unchanged
    assert entry["operation_id"] == res["operation_id"]
    assert entry["action_type"] == "update"
    assert entry["affected_count"] == 3
    assert entry["source_filename"] == "prices.xlsx"

    assert entry["summary"] == "Updated price on 3 listings: Elite Chalong, Ozone Campus, +1 more"
    assert entry["operation_label"] == "Batch update"
    assert entry["property_names"] == ["Elite Chalong", "Ozone Campus", "The Third Place"]
    assert entry["actor"] == "admin@tajlandia.com"
    assert entry["timestamp"] == entry["created_at"]

    by_id = {c["property_id"]: c for c in entry["changes"]}
    elite = by_id["elite-chalong"]
    assert elite["property_name"] == "Elite Chalong"
    assert elite["field"] == "priceFrom"
    assert elite["old"] == 10_000_000
    assert elite["new"] == 11_000_000
    assert "10,000,000" in elite["text"] and "11,000,000" in elite["text"] and "->" in elite["text"]
    # bookkeeping fields are not reported as changes
    assert not any(c["field"] in ("updatedAt", "_lastModifiedBy", "_batchOperationId") for c in entry["changes"])


def test_names_are_stored_at_write_time_and_survive_listing_deletion(fs, staff):
    _seed(fs, "elite-chalong", "Elite Chalong", 10_000_000)
    res = execute_batch_property_operation(
        "update", [{"property_id": "elite-chalong", "status": "Sold Out"}], staff_user=staff
    )
    stored = fs.collection(BATCH_OPERATIONS_COLLECTION).document(res["operation_id"]).get().to_dict()
    assert stored["property_names"] == {"elite-chalong": "Elite Chalong"}
    assert stored["summary"] == "Updated status on 1 listing: Elite Chalong"
    assert stored["changes"][0]["old"] == "Ready" and stored["changes"][0]["new"] == "Sold Out"

    # listing is gone later: history must not depend on it
    fs.collection("projects").document("elite-chalong").delete()
    entry = _only_entry(staff)
    assert entry["property_names"] == ["Elite Chalong"]
    assert "Elite Chalong" in entry["summary"]


def test_batch_delete_and_create_summaries(fs, staff):
    _seed(fs, "a-villa", "Alpha Villa", 5_000_000)
    _seed(fs, "b-villa", "Beta Villa", 6_000_000)
    execute_batch_property_operation(
        "delete", [{"property_id": "a-villa"}, {"property_id": "b-villa"}], staff_user=staff
    )
    entry = _only_entry(staff)
    assert entry["summary"] == "Archived 2 listings: Alpha Villa, Beta Villa"
    assert entry["operation_label"] == "Batch archive"


def test_crud_update_snapshot_records_readable_change(fs, staff):
    _seed(fs, "elite-chalong", "Elite Chalong", 4_000_000)
    op_id = record_crud_operation_snapshot(
        action_type="update",
        property_id="elite-chalong",
        previous_state=None,
        staff_user=staff,
        source="chat_confirm",
        new_values={"price_numeric_min": 5_000_000},
    )
    assert op_id
    entry = _only_entry(staff)
    assert entry["summary"] == "Updated price on 1 listing: Elite Chalong"
    assert entry["operation_label"] == "Listing update"
    ch = entry["changes"][0]
    assert ch["field"] == "price_numeric_min"
    assert ch["old"] == 4_000_000 and ch["new"] == 5_000_000


def test_crud_create_snapshot_captures_new_listing_name(fs, staff):
    record_crud_operation_snapshot(
        action_type="create",
        property_id="new-villa",
        previous_state=None,
        staff_user=staff,
        created_id="new-villa",
        new_values={"property_name": "Brand New Villa", "price_numeric_min": 3_000_000},
    )
    entry = _only_entry(staff)
    assert entry["property_names"] == ["Brand New Villa"]
    assert entry["summary"] == "Created 1 listing: Brand New Villa"


def _legacy_record(op_id, ids, **extra):
    now = datetime.now(timezone.utc)
    rec = {
        "operation_id": op_id,
        "action_type": "update",
        "source_filename": "old.xlsx",
        "staff_user_id": "admin-001",
        "staff_email": "admin@tajlandia.com",
        "websites": [SITE],
        "status": "APPLIED",
        "affected_count": len(ids),
        "affected_ids": ids,
        "created_at": now.isoformat(),
        "expire_at": (now + timedelta(days=5)).isoformat(),
        "details": [{"property_id": i, "action": "update"} for i in ids],
    }
    rec.update(extra)
    return rec


def test_legacy_records_resolve_names_with_one_batched_lookup(fs, staff, monkeypatch):
    _seed(fs, "p1", "Elite Chalong", 1)
    _seed(fs, "p2", "Ozone Campus", 1)
    _seed(fs, "p3", "Laguna Park", 1)
    col = fs.collection(BATCH_OPERATIONS_COLLECTION)
    col.document("crud_old1").set(_legacy_record("crud_old1", ["p1"]))
    col.document("crud_old2").set(_legacy_record("crud_old2", ["p2", "p3"]))

    calls = []

    def get_all(refs, *a, **k):
        refs = list(refs)
        calls.append([r.id for r in refs])
        return [r.get() for r in refs]

    monkeypatch.setattr(fs, "get_all", get_all, raising=False)

    hist = get_batch_operations_history(limit=10, staff_user=staff)
    assert len(calls) == 1, calls
    assert sorted(calls[0]) == ["p1", "p2", "p3"]
    names = {h["operation_id"]: h["property_names"] for h in hist}
    assert names["crud_old1"] == ["Elite Chalong"]
    assert names["crud_old2"] == ["Ozone Campus", "Laguna Park"]
    assert all(h["summary"] for h in hist)


def test_legacy_record_uses_stored_snapshot_names_without_lookup(fs, staff, monkeypatch):
    col = fs.collection(BATCH_OPERATIONS_COLLECTION)
    col.document("crud_old3").set(_legacy_record(
        "crud_old3", ["gone"],
        raw_firestore={"gone": {"name": {"en": "Deleted Villa", "pl": "Usunieta Willa"}}},
    ))
    called = []
    monkeypatch.setattr(fs, "get_all", lambda refs, *a, **k: called.append(1) or [], raising=False)
    entry = _only_entry(staff)
    assert entry["property_names"] == ["Deleted Villa"]
    assert not called


def test_history_entries_carry_the_field_names_the_portal_renders(fs, staff):
    _seed(fs, "elite-chalong", "Elite Chalong", 10_000_000)
    execute_batch_property_operation(
        "update", [{"property_id": "elite-chalong", "priceFrom": 11_000_000}],
        staff_user=staff, source_filename="prices.xlsx",
    )
    entry = _only_entry(staff)
    # app/static/js/app.js loadBatchHistory reads these names
    assert entry["operation_type"] == "update"
    assert entry["file_name"] == "prices.xlsx"
    assert entry["items_affected"] == 1
    assert entry["time_remaining"] == entry["remaining_undo_time"]
    assert entry["is_expired"] is False
