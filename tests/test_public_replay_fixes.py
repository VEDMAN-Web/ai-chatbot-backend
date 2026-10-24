"""Public chatbot repairs: foreign places, advisor handoff, filters, typos, recall."""
from unittest.mock import MagicMock

import app.services.public_agent as public_agent_mod
from app.services.conversation_controls import (
    fuzzy_command_token,
    is_advisor_request,
    is_full_reset,
    is_weather_question,
    merge_applied_filters,
)
from app.services.i18n_service import get_off_topic_text, get_quick_replies
from app.services.intent_router import IntentType, detect_deterministic_intent, extract_search_filters_deterministic
from app.services.public_agent import PublicAgentService, is_off_topic_query
from app.services.shown_set import contextual_quick_replies as shown_chips


def test_foreign_trivia_is_not_a_property_search():
    for message in (
        "who is the prime minister of india",
        "what is the capital of France",
        "who is the president of USA",
    ):
        intent, _args = detect_deterministic_intent(message)
        assert intent != IntentType.PROPERTY_SEARCH, message
        assert is_off_topic_query(message) is True


def test_foreign_property_request_is_thailand_only(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    for message, place in (
        ("properties in Dubai", "Dubai"),
        ("villa in Bali under 10 million", "Bali"),
        ("is India good for investment", "India"),
    ):
        result = agent.run_conversation(db=MagicMock(), conversation=conv, user_message=message, requested_language="en")
        text = result["text"]
        assert "Thailand property only" in text
        assert place in text
        assert "No verified listing matches" not in text
        assert result["properties"] == []


def test_thailand_searches_stay_searches():
    for message in ("properties in Phuket", "buy a villa in Thailand"):
        intent, _args = detect_deterministic_intent(message)
        assert intent == IntentType.PROPERTY_SEARCH, message


def test_weather_does_not_search(monkeypatch):
    assert is_weather_question("what is the weather in Phuket tomorrow")
    intent, _args = detect_deterministic_intent("what is the weather in Phuket tomorrow")
    assert intent != IntentType.PROPERTY_SEARCH
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    result = agent.run_conversation(
        db=MagicMock(), conversation=conv,
        user_message="what is the weather in Phuket tomorrow", requested_language="en",
    )
    assert "weather" in result["text"].lower()
    assert "Phuket" in result["text"]
    assert result["properties"] == []


def test_advisor_chip_is_not_off_topic():
    assert is_advisor_request("connect me with an advisor")
    intent, args = detect_deterministic_intent("connect me with an advisor")
    assert intent == IntentType.LEAD_CAPTURE
    assert args.get("advisor_request") is True
    assert is_off_topic_query("connect me with an advisor") is False


def test_every_generated_chip_stays_on_topic():
    chips = []
    for language in ("en", "pl", "th"):
        for has_properties in (True, False):
            for focused in (True, False):
                chips.extend(get_quick_replies(language, has_properties, "villas in Phuket", location="Phuket", focused=focused))
                chips.extend(get_quick_replies(language, has_properties, "under 5 million", zero_results=True))
        chips.extend(shown_chips(language, "Fantasea Condo"))
        off = get_off_topic_text(language, site_origin="varsoviaestate.com", user_message="who is the prime minister")
        assert off
    chips.extend(["Show villas in Phuket", "Condo in Koh Samui", "Budget under 15M THB", "Talk to an advisor", "Connect me with an advisor"])
    for chip in chips:
        intent, _args = detect_deterministic_intent(chip, stored_state={"last_result_ids": ["a"]})
        on_topic = intent is not None or is_full_reset(chip) or is_advisor_request(chip) or not is_off_topic_query(chip, lenient=True)
        assert on_topic, chip


def test_any_under_drops_stale_place_and_bedrooms():
    stored = {
        "location": "Koh Samui",
        "bedrooms_num": 3,
        "budget_max": 50_000_000,
        "active_filters": {"location": "Koh Samui", "bedrooms": 3, "max_price": 50_000_000, "beach_access": True},
    }
    extracted = extract_search_filters_deterministic("any under 1 million")
    applied = merge_applied_filters(stored, extracted, "any under 1 million")
    assert applied.get("location") in (None, "")
    assert applied.get("bedrooms") in (None, "")
    assert applied.get("beach_access") in (None, False)
    assert applied.get("max_price") == 1_000_000


def test_ready_condos_drop_beach_access():
    stored = {"active_filters": {"location": "Phuket", "beach_access": True, "bedrooms": 3}}
    extracted = extract_search_filters_deterministic("ready to move in condos in Bangkok")
    applied = merge_applied_filters(stored, extracted, "ready to move in condos in Bangkok")
    assert applied.get("location") == "Bangkok"
    assert applied.get("property_type") == "Condominium"
    assert not applied.get("beach_access")
    assert applied.get("bedrooms") in (None, "")


def test_bedroom_place_keeps_previous_budget():
    stored = {"budget_max": 5_000_000, "max_price_exclusive": True, "active_filters": {"max_price": 5_000_000}}
    extracted = extract_search_filters_deterministic("i want 4 bedroom and i want in phuket")
    applied = merge_applied_filters(stored, extracted, "i want 4 bedroom and i want in phuket")
    assert applied.get("location") == "Phuket"
    assert applied.get("bedrooms") == 4
    assert applied.get("max_price") == 5_000_000


def test_price_ranges_are_inclusive_bounds():
    filters = extract_search_filters_deterministic("budget 20M to 30M")
    assert filters.get("min_price") == 20_000_000
    assert filters.get("max_price") == 30_000_000
    ranged = extract_search_filters_deterministic("between 20 and 30 million")
    assert ranged.get("min_price") == 20_000_000
    assert ranged.get("max_price") == 30_000_000
    hyphen = extract_search_filters_deterministic("20-30M")
    assert hyphen.get("min_price") == 20_000_000
    polish = extract_search_filters_deterministic("od 20 do 30 mln")
    assert polish.get("min_price") == 20_000_000
    assert polish.get("max_price") == 30_000_000


def test_typo_commands():
    assert fuzzy_command_token("requiredments") == "requirements"
    assert fuzzy_command_token("bedroon") == "bedroom"
    assert fuzzy_command_token("chepest") == "cheapest"
    assert is_full_reset("forgot my all requiredments")
    assert is_full_reset("forget all my requirments")
    assert is_full_reset("reset my requirments")
    filters = extract_search_filters_deterministic("umm i want free properies")
    assert filters.get("explain_not_free") is True
    assert filters.get("sort_by_price") == "asc"


def test_most_expensive_is_a_sort_not_a_name():
    intent, args = detect_deterministic_intent("most expensive ones")
    assert intent == IntentType.PROPERTY_SEARCH
    assert args.get("property_name") in (None, "")
