"""Card buttons (PDF / Email / Tour / Inquire) mention a listing name but must NOT be treated as a details lookup."""
import pytest

from app.services import intent_router as ir
from app.services.intent_router import IntentType
from app.services.property_name_index import PropertyNameIndex


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    idx = PropertyNameIndex()
    idx.load_from_rows([
        {"doc_id": "anava-condominium", "name": {"en": "Anava Condominium"}, "cityId": "samui"},
        {"doc_id": "aura-condominium", "name": {"en": "Aura Condominium"}, "cityId": "phuket"},
    ])
    idx._loaded_at = 10**12
    monkeypatch.setattr("app.services.property_name_index.get_property_name_index", lambda: idx)
    monkeypatch.setattr("app.services.property_name_index.match_property_name", idx.match)


@pytest.mark.parametrize("msg", [
    "Download brochure for Anava Condominium", "download brochure for anava condominium",
    "PDF brochure for Anava", "Send me the pdf of Aura Condominium", "brochure for Anava Condominuim",
])
def test_pdf_requests_stay_brochure_requests(msg):
    intent, params = ir.detect_deterministic_intent(msg)
    assert intent == IntentType.BROCHURE_REQUEST
    assert not params.get("needs_clarification")


def test_brochure_search_phrase_is_not_a_property_id():
    intent, params = ir.detect_deterministic_intent(
        "Download brochure for all Phuket listings under 10M"
    )
    assert intent == IntentType.BROCHURE_REQUEST
    assert params.get("property_id") is None
    assert params.get("needs_clarification") is True


def test_email_button_message_is_lead_capture_not_details():
    msg = "Please email the verified brochure and floor plans for MlijGwytZdYL1EiGumjV (Anava Condominium) to my email: a@b.com (Name: Vedant)."
    intent, _ = ir.detect_deterministic_intent(msg)
    assert intent != IntentType.PROPERTY_DETAILS


@pytest.mark.parametrize("msg", ["Schedule viewing for Anava Condominium", "I want a tour of Aura Condominium"])
def test_tour_requests_are_not_details(msg):
    intent, _ = ir.detect_deterministic_intent(msg)
    assert intent != IntentType.PROPERTY_DETAILS


def test_plain_name_questions_still_work():
    intent, params = ir.detect_deterministic_intent("does Anava Condominium have a pool?")
    assert intent == IntentType.PROPERTY_DETAILS


def test_inquire_button_message_with_listing_id_is_not_a_location_search():
    msg = "I am interested in MIijGwytZdYL1EiGumjV. Can you provide details and arrange a viewing?"
    intent, params = ir.detect_deterministic_intent(msg)
    assert intent != IntentType.PROPERTY_SEARCH
    assert "foreign_location" not in params


def test_real_search_with_numbers_still_a_search():
    intent, _ = ir.detect_deterministic_intent("3 bedroom villas in Phuket under 25 million THB")
    assert intent == IntentType.PROPERTY_SEARCH
