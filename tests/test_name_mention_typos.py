"""A listing name anywhere in a sentence, with typos, must never resolve to the WRONG listing."""
import pytest

from app.services import intent_router as ir
from app.services.intent_router import IntentType
from app.services.property_name_index import PropertyNameIndex

ROWS = [
    {"doc_id": "anava-condominium", "name": {"en": "Anava Condominium"}, "cityId": "samui"},
    {"doc_id": "aqua-sukhumvit-49", "name": {"en": "Aequa Sukhumvit 49"}, "cityId": "bangkok"},
    {"doc_id": "tpj-condo", "name": {"en": "T.P.J. Condo"}, "cityId": "bangkok"},
    {"doc_id": "grand-langsuan", "name": {"en": "Grand Langsuan"}, "cityId": "bangkok"},
    {"doc_id": "akara-3", "name": {"en": "Akara Pool Villas Phase 3"}, "cityId": "phuket"},
    {"doc_id": "akara-4", "name": {"en": "Akara Pool Villas Phase 4"}, "cityId": "phuket"},
    {"doc_id": "777-beach", "name": {"en": "777 Beach Condo"}, "cityId": "phuket"},
    {"doc_id": "premier-24", "name": {"en": "Premier Condominium Sukhumvit 24"}, "cityId": "bangkok"},
    {"doc_id": "raintree", "name": {"en": "Raintree Villa"}, "cityId": "phuket"},
]


@pytest.fixture()
def idx(monkeypatch):
    i = PropertyNameIndex()
    i.load_from_rows(ROWS)
    i._loaded_at = 10**12
    monkeypatch.setattr("app.services.property_name_index.get_property_name_index", lambda: i)
    monkeypatch.setattr("app.services.property_name_index.match_property_name", i.match)
    return i


@pytest.mark.parametrize("msg,expected", [
    ("does Anava Condominium have a pool?", "Anava Condominium"),
    ("hi, I would like to know more about anava condominuim please", "Anava Condominium"),
    ("what is the price of Aequa Sukhumvit 49", "Aequa Sukhumvit 49"),
    ("tell me about Grand Langsuan", "Grand Langsuan"),
    ("is 777 Beach Condo freehold?", "777 Beach Condo"),
])
def test_name_in_sentence_resolves_to_that_listing(idx, msg, expected):
    m = idx.find_mention(msg)
    assert m.confident is not None and m.confident.display_name == expected


@pytest.mark.parametrize("msg,forbidden", [
    ("Nauara Luxury Grand Villas", "Grand Langsuan"),
    ("tell me about Aequa Sukhumvit 94 please", "T.P.J. Condo"),
    ("does Akara Pool Villsa Phase 3 have a pool?", "Akara Pool Villas Phase 4"),
])
def test_typos_and_unknown_words_never_confident_on_the_wrong_listing(idx, msg, forbidden):
    m = idx.find_mention(msg)
    assert not (m.confident and m.confident.display_name == forbidden)


def test_phase_number_is_discriminating(idx):
    m = idx.find_mention("does Akara Pool Villas Phase 3 have a pool?")
    assert m.confident is not None and m.confident.display_name.endswith("Phase 3")


@pytest.mark.parametrize("msg", [
    "show me villas in Phuket", "3 bedroom villas under 10 million", "what is the best condo in Bangkok?",
    "hello", "thanks!",
])
def test_ordinary_text_is_not_mistaken_for_a_name(idx, msg):
    intent, _ = ir.detect_deterministic_intent(msg)
    assert intent != IntentType.PROPERTY_DETAILS


def test_uncertain_name_becomes_did_you_mean_not_a_guess(idx):
    intent, params = ir.detect_deterministic_intent("show me Premier Condominum Sukhumvit 24")
    assert intent == IntentType.PROPERTY_DETAILS
    assert params.get("name_uncertain") or params["property_name"] == "Premier Condominium Sukhumvit 24"


@pytest.mark.parametrize("msg", [
    "villas under 5 million", "under ,", "budget 1.2.3 million", "price from . to 5", "between , and 7",
    "more than 1,,000", "bedrooms ...", "cheaper than ,.",
])
def test_filter_extraction_never_crashes_on_odd_numbers(msg):
    ir.extract_search_filters_deterministic(msg)
