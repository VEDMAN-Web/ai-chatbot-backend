"""Property writes, snapshots and undo go to Firestore only (REST and chat-confirm paths)."""
from __future__ import annotations

from tests.test_admin_rest_crud_golden import (  # noqa: F401  (world is a pytest fixture)
    _fs_doc,
    _minimal_body,
    _staff,
    world,
)


def test_create_goes_to_firestore(world):
    res = world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="fs-only-1"))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["firestore"] is True and "sql" not in body
    assert "Postgres" not in body["message"]
    assert _fs_doc(world["admin_fs"], "fs-only-1") is not None


def test_update_and_undo_restore_the_document(world):
    world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="fs-only-2"))
    before = _fs_doc(world["admin_fs"], "fs-only-2")["priceFrom"]

    upd = world["client"].put("/api/v1/admin/properties/fs-only-2", json={"price_numeric_min": 9999999})
    assert upd.status_code == 200, upd.text
    assert upd.json()["firestore"] is True
    assert _fs_doc(world["admin_fs"], "fs-only-2")["priceFrom"] == 9999999

    undone = world["client"].post(f"/api/v1/admin/operations/{upd.json()['operation_id']}/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["restored_ok"] is True
    assert undone.json()["differing_fields"] == []
    assert _fs_doc(world["admin_fs"], "fs-only-2")["priceFrom"] == before


def test_soft_delete_and_undo(world):
    world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="fs-only-3"))
    res = world["client"].delete("/api/v1/admin/properties/fs-only-3")
    assert res.status_code == 200, res.text
    assert res.json()["firestore"] is True
    undone = world["client"].post(f"/api/v1/admin/operations/{res.json()['operation_id']}/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["restored_ok"] is True
    assert _fs_doc(world["admin_fs"], "fs-only-3").get("isActive") is True


def test_duplicate_create_blocked(world):
    world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="fs-dup"))
    again = world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="fs-dup"))
    assert again.status_code == 409


def test_chat_confirm_update_is_undoable_and_not_treated_as_create(world):
    from app.services.action_service import _execute_update_listing

    world["client"].post("/api/v1/admin/properties", json=_minimal_body(property_id="fs-chat-1"))
    old = _fs_doc(world["admin_fs"], "fs-chat-1").get("priceFrom")
    out = _execute_update_listing(
        world["db"],
        {"property_id": "fs-chat-1", "mode": "update", "fields_to_update": {"price_numeric_min": 7777777}},
        authenticated_staff=_staff(),
    )
    assert out["operation_id"]
    assert _fs_doc(world["admin_fs"], "fs-chat-1").get("priceFrom") != old

    undone = world["client"].post(f"/api/v1/admin/operations/{out['operation_id']}/undo")
    assert undone.status_code == 200, undone.text
    doc = _fs_doc(world["admin_fs"], "fs-chat-1")
    assert doc is not None, "undo of an update must not delete the document"
    assert doc.get("priceFrom") == old


def test_chat_confirm_create_writes_firestore(world):
    from app.services.action_service import _execute_update_listing

    out = _execute_update_listing(
        world["db"],
        {
            "property_id": "fs-chat-new",
            "mode": "create",
            "fields_to_update": {
                "property_name": "Chat Created Villa", "property_type": "Villa",
                "province": "Phuket", "price_numeric_min": 3000000, "status": "Available",
            },
            "websites": ["tajlandia.com"],
        },
        authenticated_staff=_staff(),
    )
    assert out["mode"] == "create"
    assert _fs_doc(world["admin_fs"], "fs-chat-new") is not None
    undone = world["client"].post(f"/api/v1/admin/operations/{out['operation_id']}/undo")
    assert undone.status_code == 200, undone.text
    assert _fs_doc(world["admin_fs"], "fs-chat-new") is None
