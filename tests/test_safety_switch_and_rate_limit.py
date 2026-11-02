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


# --- the switch is enforced inside every listing write ----------------------

def test_write_functions_refuse_and_change_nothing_when_switch_off(monkeypatch):
    from app.core.listing_writes import ListingWritesDisabled
    from app.services import admin_batch_service
    fake = patch_firestore(monkeypatch, FakeFirestore())
    fake.collection("projects").document("keep").set({"name": "Keep", "priceFrom": 1})
    before = dict(fake.docs)
    monkeypatch.setattr("app.core.config.settings.ADMIN_LISTING_WRITES_ENABLED", False)
    calls = [
        lambda: fs.save_or_update_property_firestore("new-1", {"property_name": "X"}, target="admin"),
        lambda: fs.save_or_update_property_firestore("keep", {"status": "Sold"}, target="admin"),
        lambda: fs.write_admin_property_document("keep", {"name": "Y"}),
        lambda: fs.delete_property_firestore("keep", hard_delete=True),
        lambda: admin_batch_service.execute_batch_property_operation("delete", [{"property_id": "keep"}], None),
        lambda: admin_batch_service.undo_batch_property_operation("op1", None),
    ]
    for call in calls:
        with pytest.raises(ListingWritesDisabled):
            call()
    assert fake.docs == before


def test_document_import_write_refused_when_switch_off(monkeypatch):
    dip = pytest.importorskip("app.services.document_ingestion_pipeline")
    from app.core.listing_writes import ListingWritesDisabled
    fake = patch_firestore(monkeypatch, FakeFirestore())
    monkeypatch.setattr("app.core.config.settings.ADMIN_LISTING_WRITES_ENABLED", False)
    with pytest.raises(ListingWritesDisabled):
        dip._save_to_postgresql_and_firestore({"property_id": "doc-1", "name": "Z"}, [])
    assert fake.docs == {}


def test_chat_url_import_confirm_is_refused_when_switch_off(monkeypatch):
    from app.services import property_web_ingest
    monkeypatch.setattr("app.core.config.settings.ADMIN_LISTING_WRITES_ENABLED", False)
    reply = property_web_ingest._save_one(None, None, SimpleNamespace(action_payload={"property_name": "X"}))
    assert "read-only" in reply


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
