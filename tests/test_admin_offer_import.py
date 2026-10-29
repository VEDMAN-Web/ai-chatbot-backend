"""Bulk Add/Create from an offer-style document: preview first, save only after confirm."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api.v1 import admin as admin_api
from tests import fs_helpers as fsh
from app.services.admin_offer_import import PAGE_BREAK, extract_offer_listings

from tests.test_admin_url_ingest import world  # noqa: F401  (shared in-memory Firestore fixture)

COVER = "OFFER FOR A CLIENT\nThree ready buildings\nFirst Building · Area One\n"

SECTION_A = """First Sample Tower — near the beach
Pattaya · Area One (North), north Pattaya
Ready building by Sample Developer. Apartments with a pool.
1 sypialnia · 38,94 m²
od 98 200 USD
3 300 000 THB
dostępny
"""
SECTION_B = """Second Sample Hills — ready since 2015
Pattaya · Hill Two
Gotowy budynek. Mieszkania i studia.
1 sypialnia · 32,46 m²
3 150 000 THB
Studio · 26,57 m²
2 550 000 THB
"""
SUMMARY = """Comparison of all buildings
First Sample Tower
Pattaya · Area One
od 3 300 000 THB
Second Sample Hills
od 2 550 000 THB
Third Sample Bay
od 9 999 000 THB
"""
BARE = "Bare Sample House — nice\nPattaya · Somewhere\nA lovely place.\n"


def _offer(*pages: str) -> str:
    return PAGE_BREAK.join(pages)


def test_each_listing_gets_only_the_values_in_its_own_section():
    drafts = extract_offer_listings(_offer(COVER, SECTION_A, SECTION_B, SUMMARY))
    assert [d["label"] for d in drafts] == ["First Sample Tower", "Second Sample Hills"]
    a, b = (d["fields"] for d in drafts)
    assert a["price_numeric_min"] == 3300000 and "price_numeric_max" not in a
    assert a["province"] == "Pattaya" and a["property_type"] == "Apartment" and a["status"] == "Ready to move in"
    assert a["bedrooms_min"] == 1 and a["area_sqm_min"] == 38.94
    # The summary page after the last section lists other prices; they must not leak in.
    assert b["price_numeric_min"] == 2550000 and b["price_numeric_max"] == 3150000
    assert b["bedrooms_min"] == 0 and b["bedrooms_max"] == 1
    assert b["area_sqm_min"] == 26.57 and b["area_sqm_max"] == 32.46
    assert all(not d["missing"] for d in drafts)


def test_values_the_document_does_not_state_stay_missing():
    (draft,) = extract_offer_listings(BARE)
    assert set(draft["missing"]) == {"property_type", "price", "status"}
    assert "price_numeric_min" not in draft["fields"] and "status" not in draft["fields"]


def test_a_document_without_listing_sections_yields_nothing():
    assert extract_offer_listings("Just a letter.\nNo listings here.\n") == []


# --- endpoints ------------------------------------------------------------------------------------------

@pytest.fixture
def api(world, monkeypatch):  # noqa: F811
    staff = world["staff"]
    main.app.dependency_overrides[admin_api.require_admin_or_super_admin] = lambda: staff
    main.app.dependency_overrides[admin_api.require_admin_or_employee] = lambda: staff
    main.app.dependency_overrides[admin_api.get_db] = lambda: world["db"]
    monkeypatch.setattr("app.services.admin_offer_import.read_document_text", lambda data, ext: data.decode("utf-8"))
    yield TestClient(main.app), world
    for dep in (admin_api.require_admin_or_super_admin, admin_api.require_admin_or_employee, admin_api.get_db):
        main.app.dependency_overrides.pop(dep, None)


def _upload(client, text, action="create", name="offer.txt"):
    return client.post(
        "/api/v1/admin/properties/batch-file",
        files={"file": (name, text.encode("utf-8"), "text/plain")},
        data={"action": action},
    )


def _confirm(client, body, token=None):
    return client.post(
        f"/api/v1/admin/actions/{body['pending_action_id']}/confirm",
        json={"confirmation_token": body["confirmation_token"] if token is None else token},
    )


def _renew(world):
    pass  # pending actions live in Firestore with an ISO expiry; nothing to renew


def test_upload_stages_a_preview_and_saves_nothing(api):
    client, world = api
    res = _upload(client, _offer(COVER, SECTION_A, SECTION_B, SUMMARY))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "PREVIEW" and body["requires_confirmation"] is True
    assert body["ready_count"] == 2 and len(body["listings"]) == 2
    assert body["pending_action_id"] and body["confirmation_token"]
    assert world["fs"].items("projects") == []


def test_confirm_creates_the_listings_and_verifies_them(api):
    client, world = api
    body = _upload(client, _offer(COVER, SECTION_A, SECTION_B, SUMMARY)).json()
    _renew(world)
    done = _confirm(client, body)
    assert done.status_code == 200, done.text
    result = done.json()["execution_result"]
    assert sorted(c["property_id"] for c in result["created"]) == ["first-sample-tower", "second-sample-hills"]
    assert result["skipped"] == []
    row = fsh.prop_one(world["fs"], property_id="second-sample-hills")
    assert row.price_numeric_min == 2550000 and row.property_type == "Apartment"
    assert world["fs"].get("projects", "first-sample-tower") is not None
    # A second confirm must not write again.
    again = _confirm(client, body)
    assert again.status_code == 200 and again.json().get("idempotent") is True
    assert len(world["fs"].items("projects")) == 2


def test_confirm_needs_the_right_token(api):
    client, world = api
    body = _upload(client, _offer(COVER, SECTION_A)).json()
    _renew(world)
    assert _confirm(client, body, token="act_nope").status_code == 400
    missing = client.post(f"/api/v1/admin/actions/{body['pending_action_id']}/confirm", json={})
    assert missing.status_code == 400
    assert world["fs"].items("projects") == []


def test_listing_missing_required_values_is_not_created(api):
    client, world = api
    body = _upload(client, _offer(COVER, SECTION_A, BARE)).json()
    by_name = {row["name"]: row for row in body["listings"]}
    assert by_name["Bare Sample House"]["ready"] is False
    assert "missing" in by_name["Bare Sample House"]["outcome"]
    _renew(world)
    done = _confirm(client, body).json()["execution_result"]
    assert [c["name"] for c in done["created"]] == ["First Sample Tower"]
    assert done["skipped"][0]["name"] == "Bare Sample House"
    assert fsh.prop_first(world["fs"], name="Bare Sample House") is None


def test_existing_listing_is_skipped_not_duplicated(api):
    client, world = api
    world["fs"].set("projects", "first-sample-tower", {"name": "First Sample Tower", "isActive": True})
    body = _upload(client, _offer(COVER, SECTION_A, SECTION_B)).json()
    row = {r["name"]: r for r in body["listings"]}["First Sample Tower"]
    assert row["ready"] is False and "already exists" in row["outcome"]
    assert body["ready_count"] == 1


def test_discarded_preview_cannot_be_confirmed(api):
    client, world = api
    body = _upload(client, _offer(COVER, SECTION_A)).json()
    _renew(world)
    rejected = client.post(f"/api/v1/admin/actions/{body['pending_action_id']}/reject", json={"reason": "no"})
    assert rejected.status_code == 200
    assert _confirm(client, body).status_code == 400
    assert world["fs"].items("projects") == []


def test_unreadable_document_gets_a_helpful_error(api):
    client, _world = api
    res = _upload(client, "Hello, this is just a letter.")
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "No property identifiers" in detail and "CSV or Excel" in detail


def test_update_and_delete_uploads_never_use_the_offer_preview(api):
    client, _world = api
    for action in ("update", "delete"):
        res = _upload(client, _offer(COVER, SECTION_A), action=action)
        assert res.json().get("status") != "PREVIEW"


def test_modal_shows_the_preview_and_confirms_through_the_action_endpoint():
    js = (Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "data.status === 'PREVIEW'" in js
    assert "/api/v1/admin/actions/${encodeURIComponent(id)}/confirm" in js
    assert "confirmation_token: token" in js
