"""Email / Tour / Inquire card messages: confirm the listing they are about, never an invented place or random cards."""
from types import SimpleNamespace

import pytest

from app.services import intent_router as ir
from app.services import public_agent as pa
from app.services.intent_router import IntentType
from app.services.property_name_index import PropertyNameIndex

ID = "MIijGwytZdYL1EiGumjV"


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    idx = PropertyNameIndex()
    idx.load_from_rows([
        {"doc_id": ID, "name": {"en": "Anava Condominium"}, "cityId": "samui"},
        {"doc_id": "aura", "name": {"en": "Aura Condominium"}, "cityId": "phuket"},
    ])
    idx._loaded_at = 10**12
    monkeypatch.setattr("app.services.property_name_index.get_property_name_index", lambda: idx)
    monkeypatch.setattr("app.services.property_name_index.match_property_name", idx.match)


def test_date_is_not_a_phone_number():
    msg = f"Schedule a viewing for {ID} (Anava Condominium) on 2026-11-02 at 10:00. My name is QA Test, email is a@b.com."
    intent, params = ir.detect_deterministic_intent(msg)
    assert intent == IntentType.LEAD_CAPTURE
    assert params["phone"] is None and params["email"] == "a@b.com"
    assert params["target_ref"] == ID


def test_date_alone_does_not_trigger_lead_capture():
    intent, _ = ir.detect_deterministic_intent("show me villas available on 2026-11-02")
    assert intent != IntentType.LEAD_CAPTURE


def test_real_phone_still_detected():
    intent, params = ir.detect_deterministic_intent("call me on +66 81 679 4414 about Anava Condominium")
    assert intent == IntentType.LEAD_CAPTURE and params["phone"].startswith("+66")


def test_email_button_flags_specs_request():
    msg = f"Please email the verified brochure and floor plans for {ID} (Anava Condominium) to my email: a@b.com."
    intent, params = ir.detect_deterministic_intent(msg)
    assert intent == IntentType.LEAD_CAPTURE and params["wants_specs"] is True


@pytest.mark.parametrize("kwargs,must,must_not", [
    ({"is_viewing": True, "target_ref": ID, "email": "a@b.com", "name": "QA"}, ["Anava Condominium", "private viewing", "a@b.com"], ["Phuket", "VIP"]),
    ({"wants_specs": True, "target_ref": ID, "email": "a@b.com"}, ["Anava Condominium", "specifications"], ["Phuket", "viewing"]),
    ({"is_viewing": True, "email": "a@b.com"}, ["a@b.com"], ["Phuket"]),
])
def test_lead_confirmation_is_about_the_target(monkeypatch, kwargs, must, must_not):
    lead = SimpleNamespace(id="lead-1")
    monkeypatch.setattr(pa, "record_or_update_lead", lambda **k: lead)
    monkeypatch.setattr(pa, "append_conversation_turn", lambda **k: None)
    monkeypatch.setattr(pa.token_tracker, "log_turn", lambda *a, **k: None)
    conv = SimpleNamespace(site_origin="varsoviaestate.com", session_id="s", id="c", preferences={}, message_history=[])
    out = pa.public_agent._handle_lead_capture(None, conv, dict(kwargs), "msg", 0.0, language="en")
    assert out["properties"] == [] and out["lead_captured"] is True
    for s in must:
        assert s in out["text"]
    for s in must_not:
        assert s not in out["text"]
