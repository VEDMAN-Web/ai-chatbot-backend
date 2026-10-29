"""Creating a listing from chat must not ask the admin for a property_id."""

from __future__ import annotations

from tests import fs_helpers as fsh
from app.services.action_service import generate_property_id
from app.services.admin_agent import _execute_admin_tool

from tests.test_admin_url_ingest import world  # noqa: F401  (shared in-memory Firestore fixture)

FIELDS = {
    "property_name": "QA Test Villa Krishna",
    "province": "Phuket",
    "bedrooms": 4,
    "price_numeric_min": 10900000,
    "status": "Available",
    "property_type": "Villa",
}


def _propose(world, args):
    return _execute_admin_tool(world["db"], "create_or_update_listing", args, world["staff"], "conv-ingest")


def test_create_without_property_id_generates_one_from_the_name(world):
    result, needs_confirm, pending_id = _propose(world, {"fields_to_update": dict(FIELDS)})
    assert result["status"] == "PROPOSED", result
    assert needs_confirm is True and pending_id
    row = fsh.pending_one(world["fs"], "PENDING")
    assert row.action_payload["property_id"] == "qa-test-villa-krishna"
    assert row.action_payload["mode"] == "create"
    assert "property_id" not in result.get("message", "").split("missing")[-1]


def test_blank_property_id_is_treated_like_no_property_id(world):
    result, _confirm, pending_id = _propose(world, {"property_id": "  ", "fields_to_update": dict(FIELDS)})
    assert result["status"] == "PROPOSED", result
    row = fsh.pending_one(world["fs"], "PENDING")
    assert row.action_payload["property_id"] == "qa-test-villa-krishna"


def test_generated_id_never_collides_with_an_existing_listing(world):
    db = world["db"]
    assert generate_property_id(db, "QA Test Villa Krishna") == "qa-test-villa-krishna"
    world["fs"].set("projects", "qa-test-villa-krishna", {"name": "Something Else", "status": "Available", "isActive": True})
    assert generate_property_id(db, "QA Test Villa Krishna") == "qa-test-villa-krishna-2"
    world["fs"].set("projects", "qa-test-villa-krishna-2", {"name": "Another", "status": "Available", "isActive": True})
    assert generate_property_id(db, "QA Test Villa Krishna") == "qa-test-villa-krishna-3"


def test_name_with_no_usable_characters_still_gets_a_valid_id(world):
    pid = generate_property_id(world["db"], "วิลล่า ภูเก็ต")
    assert pid.startswith("prop-") and len(pid) <= 64


def test_missing_required_value_other_than_id_is_still_reported(world):
    fields = dict(FIELDS)
    fields.pop("status")
    result, needs_confirm, pending_id = _propose(world, {"fields_to_update": fields})
    assert result["status"] == "VALIDATION_FAILED"
    assert "status" in result["missing_fields"]
    assert "property_id" not in result["missing_fields"]
    assert not needs_confirm and not pending_id


def test_updating_an_existing_listing_still_uses_the_given_id(world):
    db = world["db"]
    world["fs"].set("projects", "old-villa", {"name": "Old Villa", "status": "Available", "isActive": True})
    result, _confirm, pending_id = _propose(
        world, {"property_id": "old-villa", "fields_to_update": {"price_numeric_min": 5000000}}
    )
    assert result["status"] == "PROPOSED", result
    row = fsh.pending_one(world["fs"], "PENDING")
    assert row.action_payload["property_id"] == "old-villa"
    assert row.action_payload["mode"] == "update"
