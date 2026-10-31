"""Lead submissions report whether the lead was really saved (never 'registered' for a dropped lead)."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda *a, **kw: None)
    monkeypatch.setattr("app.api.v1.chat.record_or_update_lead", lambda **kw: SimpleNamespace(id="lead-1", lead_score=0.9))


def _contact_only():
    return client.post("/api/v1/chat/public", json={
        "session_id": f"lead-honesty-{uuid.uuid4().hex[:8]}", "contact_name": "Ann", "contact_email": "ann@example.com"})


def _direct():
    return client.post("/api/v1/chat/lead", json={
        "session_id": f"lead-honesty-{uuid.uuid4().hex[:8]}", "contact_name": "Ann", "contact_email": "ann@example.com"})


def _saving(monkeypatch, enqueue):
    monkeypatch.setattr(settings, "ENABLE_LEAD_SAVING", True)
    monkeypatch.setattr("app.services.lead_service.enqueue_lead_save", enqueue)


def _fails(**kw):
    raise RuntimeError("database down")


def test_saving_disabled_is_reported_as_not_saved(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LEAD_SAVING", False)
    assert _contact_only().json()["lead_saved"] is False
    direct = _direct().json()
    assert direct["lead_saved"] is False and direct["status"] == "not_saved"
    assert "registered" not in direct["message"].lower()


def test_successful_save_is_reported_as_saved(monkeypatch):
    saved = []
    _saving(monkeypatch, lambda **kw: saved.append(kw))
    assert _contact_only().json()["lead_saved"] is True
    direct = _direct().json()
    assert direct["lead_saved"] is True and direct["status"] == "success"
    assert len(saved) == 2


def test_a_failed_save_is_never_reported_as_saved(monkeypatch):
    _saving(monkeypatch, _fails)
    assert _contact_only().json()["lead_saved"] is False
    direct = _direct().json()
    assert direct["lead_saved"] is False and direct["status"] == "not_saved"
