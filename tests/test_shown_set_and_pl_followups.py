from app.services.intent_router import detect_deterministic_intent
from app.services.property_context_service import _FACT_HINTS
from app.services.shown_set import answer_shown_set_question, parse_price_thb
from app.services.listing_facts import short_summary
from app.services.public_agent import _clean_field

CARDS = [
    {"property_id": "a", "name": "Alpha", "price": "from 10.56M THB", "has_pool": True},
    {"property_id": "b", "name": "Beta", "price": "from 9.08M THB", "has_pool": True},
    {"property_id": "c", "name": "Gamma", "price": "Price on request", "has_pool": False},
]


def test_parse_price():
    assert parse_price_thb("from 19.98M THB") == 19_980_000
    assert parse_price_thb("from 98,890 USD") == 98_890 * 35.0
    assert parse_price_thb("Price on request") is None


def test_cheapest_and_priciest_of_shown():
    r = answer_shown_set_question("Which of these is the cheapest?", CARDS, "en")
    assert r and r.index("Beta") < r.index("Alpha") and "9.08M" in r
    r = answer_shown_set_question("Który jest tańszy?", CARDS, "pl")
    assert r and "Beta" in r
    r = answer_shown_set_question("Which one is the most expensive?", CARDS, "en")
    assert r and r.index("Alpha") < r.index("Beta")


def test_pool_of_shown():
    r = answer_shown_set_question("Which one has a pool?", CARDS, "en")
    assert "Alpha" in r and "Beta" in r and "Gamma" in r  # confirmed for A,B; not confirmed for Gamma
    assert "Not confirmed" in r


def test_does_not_hijack_searches_or_single_card():
    assert answer_shown_set_question("What is the cheapest villa in Samui?", CARDS, "en") is None
    assert answer_shown_set_question("Which is the cheapest in Phuket?", CARDS, "en") is None
    assert answer_shown_set_question("Which one is cheaper?", CARDS[:1], "en") is None
    assert answer_shown_set_question("Which one is cheaper?", CARDS, "th") is None


def test_polish_ordinals_resolve_to_shown_ids():
    st = {"last_result_ids": ["a", "b", "c"]}
    for q, pid in (("Pokaż pierwszy", "a"), ("Pokaż drugą", "b"), ("Opowiedz o pierwszym", "a"),
                   ("Szczegóły pierwszej oferty", "a")):
        intent, args = detect_deterministic_intent(message=q, stored_state=st)
        assert args.get("property_id") == pid, q
    # a project whose name merely starts with an ordinal stem is still a name
    _, args = detect_deterministic_intent(message="Pokaż Drugi Dom Resort", stored_state=st)
    assert args.get("property_name") == "Drugi Dom Resort"


def test_polish_fact_followups_are_recognised():
    for q in ("Ile kosztuje?", "Kiedy ukończenie?", "Który jest tańszy?", "When is it ready?", "Czy ma parking?"):
        assert _FACT_HINTS.search(q.lower()), q


def test_none_never_shown():
    assert _clean_field(None) == "" and _clean_field("None") == "" and _clean_field("N/A") == ""
    assert _clean_field("2-3 Beds") == "2-3 Beds"


def test_summary_drops_marketing_label_sentences():
    t = "Completed villa in a quiet area. Competitive entry for the buyer developer marketing claim with developer marketing claim. Near the beach."
    s = short_summary(t)
    assert "developer marketing claim" not in s and "quiet area" in s


def test_pool_unknown_is_never_stated_as_absent():
    cards = [{"property_id": "x", "name": "X", "price": "from 1M THB"}, {"property_id": "y", "name": "Y", "price": "from 2M THB"}]
    r = answer_shown_set_question("Which one has a pool?", cards, "en")
    assert "can't confirm" in r and "None of" not in r
