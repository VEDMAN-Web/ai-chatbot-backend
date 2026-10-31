import pytest

from app.services.intent_router import IntentType, detect_deterministic_intent


@pytest.mark.parametrize("msg", [
    "What is the price of Aura Condominium?",
    "How much does Aura Condominium cost?",
    "price of Aura Condominium",
])
def test_price_question_extracts_the_project_name(msg):
    intent, params = detect_deterministic_intent(msg)
    assert intent == IntentType.PROPERTY_DETAILS
    assert params["property_name"].lower() == "aura condominium"


@pytest.mark.parametrize("msg", [
    "What is the price of villas in Phuket?",
    "how much does a villa in Phuket cost?",
])
def test_price_question_about_a_category_is_not_a_name_lookup(msg):
    intent, params = detect_deterministic_intent(msg)
    assert not (intent == IntentType.PROPERTY_DETAILS and (params or {}).get("property_name"))
