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


def test_any_under_keeps_bedrooms_from_the_same_sentence():
    message = "i want under 50million any under 50million and wants 3 bedroom"
    extracted = extract_search_filters_deterministic(message)
    stored = {
        "active_filters": {"location": "Bangkok", "property_type": "Villa"},
        "location": "Bangkok",
        "property_type": "Villa",
        "last_result_ids": ["old-1"],
    }
    applied = merge_applied_filters(stored, extracted, message)
    assert extracted.get("bedrooms") == 3
    assert extracted.get("max_price") == 50_000_000
    assert applied.get("bedrooms") == extracted.get("bedrooms")
    assert applied.get("max_price") == extracted.get("max_price")
    assert applied.get("location") in (None, "")


def test_money_words_are_not_bedroom_counts():
    five = extract_search_filters_deterministic("up to 5 million")
    assert five.get("max_price") == 5_000_000
    assert five.get("max_bedrooms") is None
    five_m = extract_search_filters_deterministic("up to 5M THB")
    assert five_m.get("max_price") == 5_000_000
    assert five_m.get("max_bedrooms") is None
    beds = extract_search_filters_deterministic("up to 5 bedrooms")
    assert beds.get("max_bedrooms") == 5
    assert beds.get("max_price") is None
    villa = extract_search_filters_deterministic("up to 3 bed villa under 10 million")
    assert villa.get("max_bedrooms") == 3
    assert villa.get("max_price") == 10_000_000
    assert str(villa.get("property_type") or "").lower() == "villa"


def test_budget_range_is_stored_and_printed_with_both_ends():
    from app.services.conversation_controls import describe_applied_filters

    for message in ("budget 20M to 30M", "between 20 and 30 million"):
        extracted = extract_search_filters_deterministic(message)
        applied = merge_applied_filters({}, extracted, message)
        assert applied.get("min_price") == 20_000_000
        assert applied.get("max_price") == 30_000_000
        text = describe_applied_filters(applied)
        assert "20M-30M THB" in text


def test_no_result_range_names_both_ends(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(public_agent_mod, "search_properties_unified", lambda *a, **k: [])
    conv = _conv()
    result = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="budget 20M to 30M", requested_language="en",
    )
    assert "20M-30M THB" in result["text"]
    assert "between 20M and 30M" in result["text"]
    stored = (result.get("preferences") or {}).get("active_filters") or {}
    assert stored.get("min_price") == 20_000_000
    assert stored.get("max_price") == 30_000_000


def test_price_sort_direction():
    for message in ("most expensive", "luxury", "highest price", "top end"):
        filters = extract_search_filters_deterministic(message)
        assert filters.get("sort_by_price") == "desc", message
    assert extract_search_filters_deterministic("cheapest").get("sort_by_price") == "asc"
    assert extract_search_filters_deterministic("Explore luxury properties in Thailand").get("sort_by_price") == "desc"


def test_bedrooms_area_line_does_not_repeat_beds():
    from app.services.public_agent import _beds_area_phrase

    line = _beds_area_phrase("Beds: on request", "92 sqm")
    assert line == "Bedrooms: on request | Area: 92 sqm"
    assert "Beds:" not in line


def _conv():
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    return conv


def _cards():
    rows = [
        ("Cheap Condo", 1_990_000),
        ("Mid Condo", 4_800_000),
        ("Five Condo", 5_000_000),
        ("Canapaya Residences", 60_000_000),
    ]
    cards = []
    for name, price in rows:
        cards.append({
            "property_id": name.lower().replace(" ", "-"),
            "name": name,
            "property_type": "Condominium",
            "price": f"{price / 1_000_000:.2f}M THB",
            "price_thb_min": price,
            "bedrooms": "2",
            "area": "80 sqm",
            "location": "Bangkok",
            "status": "Ready",
            "detail_page_url": "https://varsoviaestate.com/p",
        })
    return cards


def test_most_expensive_sorts_descending_over_current_filters(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(public_agent_mod, "search_properties_unified", lambda *a, **k: _cards())
    conv = _conv()
    conv.preferences = {"active_filters": {"location": "Bangkok"}, "location": "Bangkok"}
    result = agent.run_conversation(db=MagicMock(), conversation=conv, user_message="most expensive ones", requested_language="en")
    text = result["text"]
    assert text.startswith("Most expensive in Bangkok:")
    names = [p["name"] for p in result["properties"]]
    order = ["Canapaya Residences", "Five Condo", "Mid Condo", "Cheap Condo"]
    assert names == order[: len(names)]
    assert names[0] == "Canapaya Residences"
    assert text.index("Canapaya Residences") < text.index("Mid Condo")
    assert "60.00M" in text
    cheap = agent.run_conversation(db=MagicMock(), conversation=conv, user_message="cheapest", requested_language="en")
    assert cheap["text"].startswith("Cheapest in Bangkok:")
    assert [p["name"] for p in cheap["properties"]][0] == "Cheap Condo"


def test_cheaper_is_strictly_below_the_shown_price(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)

    def fake_search(_db, params, limit=4, exclude_property_ids=None):
        if getattr(params, "max_price_exclusive", False):
            return []
        return [{
            "property_id": "sun-hills",
            "name": "Sun Hills Layan Phuket",
            "property_type": "Villa",
            "price": "4.00M THB",
            "price_thb_min": 4_000_000,
            "bedrooms": "3",
            "area": "200 sqm",
            "location": "Phuket",
            "status": "Ready",
            "detail_page_url": "https://varsoviaestate.com/sun",
        }]

    monkeypatch.setattr(public_agent_mod, "search_properties_unified", fake_search)
    conv = _conv()
    agent.run_conversation(db=MagicMock(), conversation=conv, user_message="villas in Phuket", requested_language="en")
    result = agent.run_conversation(db=MagicMock(), conversation=conv, user_message="cheaper ones", requested_language="en")
    assert result["text"] == (
        "Sun Hills Layan Phuket at 4.00M THB is already the cheapest Phuket villa I have. "
        "Want me to look at condos or other areas?"
    )
    assert "up to" not in result["text"].lower()


def test_amenity_phrase_refines_the_search(monkeypatch):
    from app.services.conversation_controls import is_search_amenity_refinement

    assert is_search_amenity_refinement("with private pool")
    assert is_search_amenity_refinement("with beach access")
    assert is_search_amenity_refinement("with sea view")
    assert is_search_amenity_refinement("pet friendly")
    assert is_search_amenity_refinement("furnished")
    assert not is_search_amenity_refinement("does it have a pool")
    assert not is_search_amenity_refinement("does Sun Hills have a pool")
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    seen = {}

    def fake_search(_db, params, limit=4, exclude_property_ids=None):
        seen["has_pool"] = params.has_pool
        seen["location"] = params.location
        return [{
            "property_id": "villa-1",
            "name": "Layan Palm Villa",
            "property_type": "Villa",
            "price": "12.00M THB",
            "price_thb_min": 12_000_000,
            "bedrooms": "3",
            "area": "180 sqm",
            "location": "Phuket",
            "status": "Ready",
            "detail_page_url": "https://varsoviaestate.com/v",
            "has_pool": True,
        }]

    monkeypatch.setattr(public_agent_mod, "search_properties_unified", fake_search)
    conv = _conv()
    agent.run_conversation(db=MagicMock(), conversation=conv, user_message="villas in Phuket", requested_language="en")
    result = agent.run_conversation(db=MagicMock(), conversation=conv, user_message="with private pool", requested_language="en")
    assert seen["has_pool"] is True
    assert seen["location"] == "Phuket"
    assert "A pool is not in our verified data" not in result["text"]
    assert "Searching" in result["text"] or "Layan Palm Villa" in result["text"] or "Layan Palm Villa" in result["text"]


def test_pdf_of_it_uses_the_property_in_focus(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)

    class Prop:
        property_id = "replay-bk-2"
        property_name = "15 Suite Sukhumvit 15"
        district = "Sukhumvit"
        province = "Bangkok"
        property_type = "Condominium"

    class Query:
        def filter(self, *_a, **_k):
            return self

        def all(self):
            return []

        def first(self):
            return Prop()

    class DB:
        def query(self, _model):
            return Query()

    monkeypatch.setattr(
        "app.services.brochure_service.generate_and_upload_brochure",
        lambda **_k: {"file_url": "https://files.example/brochure.pdf"},
    )
    conv = _conv()
    conv.preferences = {
        "property_context": {"active_property_id": "replay-bk-2", "recently_discussed_ids": ["replay-bk-2"]},
        "selected_property_ids": ["replay-bk-2"],
    }
    missing = agent.run_conversation(db=DB(), conversation=_conv(), user_message="give me pdf of it", requested_language="en")
    assert "which property" in missing["text"].lower()
    result = agent.run_conversation(db=DB(), conversation=conv, user_message="give me pdf of it", requested_language="en")
    assert "which property" not in result["text"].lower()
    assert result.get("brochure_property_id") == "replay-bk-2" or "15 Suite Sukhumvit 15" in result["text"]


def test_seen_all_count_is_this_search_only(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)

    def fake_search(_db, params, limit=4, exclude_property_ids=None):
        if exclude_property_ids:
            return []
        if params.location == "Phuket":
            return [
                {"property_id": "p1", "name": "One", "price": "12.00M THB", "property_type": "Villa", "location": "Phuket", "bedrooms": "3", "area": "100 sqm", "status": "Ready", "detail_page_url": "https://varsoviaestate.com/1"},
                {"property_id": "p2", "name": "Two", "price": "18.00M THB", "property_type": "Villa", "location": "Phuket", "bedrooms": "3", "area": "100 sqm", "status": "Ready", "detail_page_url": "https://varsoviaestate.com/2"},
            ]
        return []

    monkeypatch.setattr(public_agent_mod, "search_properties_unified", fake_search)
    conv = _conv()
    conv.preferences = {
        "active_filters": {"location": "Phuket", "property_type": "Villa"},
        "location": "Phuket",
        "property_type": "Villa",
        "seen_property_ids": ["p1", "p2", "bkk1", "bkk2", "bkk3"],
        "last_result_ids": ["p1", "p2"],
    }
    result = agent._handle_show_more(db=MagicMock(), conversation=conv, args={}, user_message="show me more", start_time=0, language="en")
    assert "(2 properties shown)" in result["text"]
    assert "(5 properties shown)" not in result["text"]
