import pytest

from app.services import intent_router as ir
from app.services.intent_router import IntentType
from app.services.property_name_index import PropertyNameIndex, get_property_name_index


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    idx = PropertyNameIndex()
    idx.load_from_rows([
        {"doc_id": "satoshis-paradise", "name": {"en": "Satoshi's Paradise"}, "cityId": "sihanoukville"},
        {"doc_id": "anava-condominium", "name": {"en": "Anava Condominium"}, "cityId": "samui"},
        {"doc_id": "aura-condominium", "name": {"en": "Aura Condominium"}, "cityId": "phuket"},
    ])
    idx._loaded_at = 10**12
    monkeypatch.setattr("app.services.property_name_index.match_property_name", idx.match)


@pytest.mark.parametrize("msg", [
    "Show me satoshi paradise", "Show me Satoshi's Paradise", "Show me anava",
    "Show me aura condominium", "find Anava Samui", "Show me Atlantis Bay Villas",
])
def test_search_verb_plus_project_name_is_a_name_lookup(msg):
    intent, params = ir.detect_deterministic_intent(msg)
    assert intent == IntentType.PROPERTY_DETAILS
    assert params["property_name"]


@pytest.mark.parametrize("msg", [
    "show me villas in Phuket", "Show me something cheap", "show me villas",
    "Show me properties in the Maldives", "looking for a pool villa", "3 bedroom villas in Phuket under 25 million THB",
])
def test_real_filter_searches_stay_searches(msg):
    intent, _ = ir.detect_deterministic_intent(msg)
    assert intent == IntentType.PROPERTY_SEARCH
