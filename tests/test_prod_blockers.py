"""Production blockers: read-only switch, real place ids on bot-saved listings, Cloud Run visitor IP."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.listing_writes import is_listing_write_request
from app.core.rate_limit import client_ip
from app.services import firestore_service as fs
from tests.fake_firestore import FakeFirestore, patch_firestore


# --- read-only switch -------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("POST", "/admin/properties"), ("PUT", "/api/v1/admin/properties/abc"),
    ("DELETE", "/admin/properties/abc"), ("POST", "/api/v1/admin/properties/batch-file"),
    ("POST", "/admin/property-images/upload"), ("POST", "/api/v1/admin/operations/op1/undo"),
    ("POST", "/admin/drive-ingestion/jobs/j1/approve"), ("POST", "/api/v1/properties/ingest-url"),
])
def test_listing_write_routes_are_recognised(method, path):
    assert is_listing_write_request(method, path)


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/properties"), ("POST", "/api/v1/login"), ("POST", "/admin/chat"),
    ("POST", "/admin/actions/x/reject"), ("GET", "/api/v1/properties/abc"),
])
def test_reads_chat_and_login_are_not_blocked(method, path):
    assert not is_listing_write_request(method, path)


def test_rest_write_gets_423_when_switch_off(monkeypatch):
    from app.main import app
    monkeypatch.setattr("app.core.config.settings.ADMIN_LISTING_WRITES_ENABLED", False)
    r = TestClient(app).post("/api/v1/admin/properties", json={})
    assert r.status_code == 423
    assert "read-only" in r.json()["detail"]


def test_rest_write_passes_switch_when_on(monkeypatch):
    from app.main import app
    monkeypatch.setattr("app.core.config.settings.ADMIN_LISTING_WRITES_ENABLED", True)
    r = TestClient(app).post("/api/v1/admin/properties", json={})
    assert r.status_code != 423  # reaches the normal auth check


def test_confirm_refuses_listing_change_when_switch_off(monkeypatch):
    from unittest.mock import MagicMock, patch
    from tests.test_admin_change_diff_preview import _pending
    from app.core.auth import AuthenticatedStaff
    from app.services import action_service

    staff = AuthenticatedStaff(user_id="admin-001", email="admin@tajlandia.com", role="super_admin",
                               allowed_websites=["tajlandia.com", "varsoviaestate.com", "thai.estate"])
    action = _pending(action_payload={"mode": "update", "property_id": "prop-1",
                                      "fields_to_update": {"price_display": "12M THB"}})
    fake = patch_firestore(monkeypatch)
    fake.docs[("projects", "prop-1")] = {"name": "Keep", "priceDisplay": "10M THB", "websites": ["tajlandia.com"]}
    monkeypatch.setattr("app.core.config.settings.ADMIN_LISTING_WRITES_ENABLED", False)
    with patch("app.services.action_service.get_pending_action", return_value=action), \
         patch("app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a1")):
        with pytest.raises(HTTPException) as e:
            action_service.confirm_and_execute_action(
                db=MagicMock(), pending_action_id=str(action.id), authenticated_staff=staff,
                confirmation_token=action.confirmation_token)
    assert e.value.status_code == 423
    assert fake.docs[("projects", "prop-1")]["priceDisplay"] == "10M THB"
    assert action.status == action_service.ACTION_STATUS_PENDING


def test_config_status_shows_switch(monkeypatch):
    from app.core.config import get_admin_config_status
    monkeypatch.setattr("app.core.config.settings.ADMIN_LISTING_WRITES_ENABLED", False)
    st = get_admin_config_status()
    assert st["listing_writes_enabled"] is False
    assert st["flags"]["ADMIN_LISTING_WRITES_ENABLED"]["value"] is False


# --- real place ids ----------------------------------------------------------

@pytest.fixture()
def store(monkeypatch):
    fake = patch_firestore(monkeypatch, FakeFirestore())
    fs._PLACE_ID_CACHE.clear()
    fake.collection("cities").document("phuket").set({"name": {"en": "Phuket", "pl": "Phuket"}, "countryId": "thailand",
                                                     "coordinates": {"lat": 7.88, "lng": 98.39}})
    fake.collection("cities").document("koh-samui").set({"name": {"en": "Koh Samui"}, "countryId": "thailand"})
    fake.collection("countries").document("cambodia").set({"name": {"en": "Cambodia", "pl": "Kambodża"}})
    fake.collection("developers").document("dev-abc").set({"name": "ABC Developments"})
    return fake


def test_bot_listing_gets_real_city_country_and_developer_ids(store):
    ok = fs.save_or_update_property_firestore(
        "zz-ids-1", {"property_name": "ZZ Ids", "province": "Koh Samui", "developer_name": "abc developments"},
        target="admin")
    assert ok
    doc = store.collection("projects").document("zz-ids-1").get().to_dict()
    assert doc["cityId"] == "koh-samui"
    assert doc["countryId"] == "thailand"
    assert doc["developerId"] == "dev-abc"


def test_country_from_text_when_city_unknown(store):
    fs.save_or_update_property_firestore(
        "zz-ids-2", {"property_name": "ZZ Ids 2", "province": "Kampot", "country": "Cambodia"}, target="admin")
    doc = store.collection("projects").document("zz-ids-2").get().to_dict()
    assert doc["countryId"] == "cambodia"
    assert doc["cityId"] == "kampot"  # unchanged fallback when the city is not known
    assert doc["developerId"] == ""
    # unknown city: saved hidden so its public page never 404s, and staff are told
    assert doc["isActive"] is False and doc["needsReview"] == fs.CITY_NOT_RECOGNISED
    assert "HIDDEN" in fs.listing_review_note("zz-ids-2")


WEBSITE_FIELDS = {
    "name": dict, "slug": dict, "subtitle": dict, "description": dict, "shortDescription": dict,
    "address": dict, "seoTitle": dict, "seoDescription": dict, "countryId": str, "cityId": str,
    "developerId": str, "priceFrom": float, "priceTo": float, "pricePerSqm": float, "currency": str,
    "propertyType": str, "completionDate": str, "completionStatus": str, "totalUnits": float,
    "availableUnits": float, "areaFrom": float, "areaTo": float, "layouts": list, "images": list,
    "coverImage": str, "videoUrl": str, "coordinates": dict, "amenities": list, "expectedROI": float,
    "rentalYield": float, "paymentPlan": str, "tags": list, "isActive": bool, "isFeatured": bool,
    "isTopList": bool,
}


def test_new_listing_with_almost_no_data_has_every_website_field(store):
    fs.save_or_update_property_firestore("zz-bare", {"property_name": "ZZ Bare", "province": "Phuket"}, target="admin")
    doc = store.collection("projects").document("zz-bare").get().to_dict()
    for key, kind in WEBSITE_FIELDS.items():
        assert key in doc, key
        assert doc[key] is not None, key
        assert isinstance(doc[key], kind), (key, type(doc[key]))
    for key in ("name", "slug", "description", "address"):
        assert set(doc[key]) >= {"pl", "en"}, key
    assert doc["name"]["en"] == doc["name"]["pl"] == "ZZ Bare"
    assert doc["coordinates"] == {"lat": 7.88, "lng": 98.39}  # city centre, never empty
    assert doc["propertyType"] == "condo" and doc["completionStatus"] == "ready" and doc["currency"] == "THB"
    assert doc["priceDisplay"] == "Price on Request" and doc["priceOnRequest"] is True
    assert doc["isActive"] is True and "needsReview" not in doc
    assert fs.listing_review_note("zz-bare") == ""


def test_update_of_existing_listing_is_not_reshaped(store):
    store.collection("projects").document("old-1").set({"name": "Legacy", "priceFrom": 5})
    fs.save_or_update_property_firestore("old-1", {"status": "Sold"}, target="admin")
    doc = store.collection("projects").document("old-1").get().to_dict()
    assert doc["name"] == "Legacy" and "coordinates" not in doc and doc["status"] == "Sold"


# --- visitor IP on Cloud Run -------------------------------------------------

class _Req:
    def __init__(self, xff):
        self.client = SimpleNamespace(host="169.254.1.1")
        self.headers = {"x-forwarded-for": xff}


def test_cloud_run_uses_front_end_appended_ip(monkeypatch):
    monkeypatch.setattr("app.core.rate_limit.settings.TRUST_PROXY_HEADERS", False)
    monkeypatch.setenv("K_SERVICE", "staging-chatbot-backend")
    assert client_ip(_Req("1.2.3.4, 203.0.113.7")) == "203.0.113.7"  # forged first entry ignored


def test_outside_cloud_run_header_ignored_unless_trusted(monkeypatch):
    monkeypatch.setattr("app.core.rate_limit.settings.TRUST_PROXY_HEADERS", False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert client_ip(_Req("1.2.3.4")) == "169.254.1.1"
