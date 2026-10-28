"""Public chatbot: filters can be cleared, locations stay real, buying questions stay general."""
from unittest.mock import MagicMock

import app.services.public_agent as public_agent_mod
from app.services.conversation_controls import (
    align_cards_to_named_text,
    bedrooms_display_value,
    clear_filter_keys,
    cleared_confirmation,
    exclude_already_shown,
    foreign_ownership_reply,
    is_focused_attribute_question,
    is_foreign_ownership_question,
    is_full_reset,
    is_known_location,
    is_recall_shown,
    message_starts_new_search,
    normalize_pool_label,
    price_is_exclusive,
    price_within_budget,
    project_status_from_message,
    slots_to_clear,
    strip_unverified_marketing,
)
from app.services.intent_router import extract_search_filters_deterministic
from app.services.public_agent import PublicAgentService


def test_reset_phrases_in_supported_languages():
    phrases = [
        "forget my requirements",
        "forget everything",
        "start over",
        "new search",
        "clear filters",
        "reset",
        "ignore my earlier requirements",
        "never mind",
        "forgot all my requirements",
        "zapomnij o wymaganiach",
        "zacznij od nowa",
        "wyczyść filtry",
        "ลืมความต้องการ",
        "เริ่มใหม่",
        "मेरी जरूरतें भूल जाओ",
        "મારી જરૂરિયાતો ભૂલી જાઓ",
    ]
    for phrase in phrases:
        assert is_full_reset(phrase), phrase
    cleared = clear_filter_keys(
        {"budget_max": 5_000_000, "location": "Bangkok", "bedrooms_num": 2, "property_context": {"active_property_id": "x"}},
        {"all"},
    )
    assert "budget_max" not in cleared
    assert "location" not in cleared
    assert "property_context" not in cleared
    assert "Cleared" in cleared_confirmation({"all"})


def test_single_filter_removal_and_replace():
    assert slots_to_clear("any location") == {"location"}
    assert "budget" in slots_to_clear("no budget limit")
    assert "budget" in slots_to_clear("any budget")
    assert "pool" in slots_to_clear("remove the pool filter")
    assert "bedrooms" in slots_to_clear("any bedrooms")
    prefs = clear_filter_keys({"location": "Phuket", "budget_max": 1, "active_filters": {"location": "Phuket", "max_price": 1}}, {"location"})
    assert "location" not in prefs
    assert prefs["active_filters"].get("max_price") == 1
    # A new value replaces only that filter.
    updated = clear_filter_keys(prefs, set())
    updated["location"] = "Bangkok"
    assert updated["active_filters"].get("max_price") == 1
    assert updated["location"] == "Bangkok"


def test_location_validator_rejects_ordinary_words():
    for bad in ("New", "New New", "current chat", "free", "this chat", "previous results"):
        assert is_known_location(bad) is False
    assert is_known_location("Phuket") is True
    assert is_known_location("Bangkok") is True
    assert is_recall_shown("show me properties you showed in current chat") is True


def test_status_mapping_and_budget_boundary():
    filters = extract_search_filters_deterministic("new new properties in thailand under 60 million")
    assert filters.get("location") in (None, "")
    assert filters.get("status") == "Off-plan"
    assert filters.get("max_price_exclusive") is True
    assert project_status_from_message("ready to move in") == "Ready"
    assert price_is_exclusive("under 5 million") is True
    assert price_is_exclusive("up to 5 million") is False
    assert price_within_budget(5_000_000, 5_000_000, exclusive=True) is False
    assert price_within_budget(4_999_999, 5_000_000, exclusive=True) is True
    assert price_within_budget(5_000_000, 5_000_000, exclusive=False) is True


def test_next_page_excludes_shown_properties():
    page = exclude_already_shown(["a", "b", "c", "d"], ["a", "b"])
    assert page == ["c", "d"]


def test_new_search_overrides_focus_and_advisory_does_not_repeat_card():
    assert message_starts_new_search("i want 4 bedroom and i want in phuket") is True
    assert message_starts_new_search("any other?") is False
    assert is_focused_attribute_question("is it near to beach") is True
    assert is_focused_attribute_question("4 bedroom in Phuket") is False
    assert is_foreign_ownership_question("can indian buy this?") is True
    text = foreign_ownership_reply(
        "can indian buy this?",
        property_name="15 Suite Sukhumvit 15",
        ownership="freehold",
        property_type="Condominium",
    )
    assert "not legal advice" in text.lower()
    assert "49%" in text
    assert "conditionally" in text.lower()
    assert "15 Suite Sukhumvit 15" in text


def test_pool_conflict_marketing_and_bedroom_slot(caplog):
    label = normalize_pool_label({
        "property_id": "suite-sukhumvit-15",
        "highlights": ["Private Pool"],
        "description": "Shared pool on the amenity deck.",
    })
    assert label == "Pool (type to be confirmed)"
    assert "suite-sukhumvit-15" in caplog.text
    cleaned = strip_unverified_marketing(
        "It is strategically positioned for European investors. The price is 12M THB.",
        verified_blob="The price is 12M THB.",
    )
    assert "investors" not in cleaned.lower()
    assert "12M" in cleaned
    kept = strip_unverified_marketing(
        "Notes: European investors visit Asoke corridor.",
        verified_blob="European investors visit Asoke corridor.",
    )
    assert "European investors" in kept
    assert bedrooms_display_value("Villa", "Condominium") == "Beds: on request"
    assert bedrooms_display_value("3", "Condominium") == "3"
    cards = align_cards_to_named_text(
        "1. Alpha\n2. Beta\n3. Gamma",
        [{"name": "Alpha"}, {"name": "Beta"}, {"name": "Gamma"}, {"name": "Delta"}],
    )
    assert [c["name"] for c in cards] == ["Alpha", "Beta", "Gamma"]


def _agent(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    agent.api_key = "sk-test"
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)
    monkeypatch.setattr(agent, "_maybe_resolve_pending_name_candidates", lambda *a, **k: None)
    return agent


def _conv(**prefs):
    conv = MagicMock()
    conv.preferences = dict(prefs)
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "tajlandia.com"
    conv.agent_type = "public"
    return conv


def test_replay_reset_recall_and_foreign_buyer(monkeypatch):
    agent = _agent(monkeypatch)
    conv = _conv(
        budget_max=5_000_000,
        location="Bangkok",
        last_shown_names=["Sea Villa", "City Condo"],
        last_result_ids=["sea", "city"],
        property_context={"active_property_id": "city", "active_property_reference": "City Condo"},
    )
    reset = agent.run_conversation(db=MagicMock(), conversation=conv, user_message="forgot all my requirements", requested_language="en")
    assert "Cleared" in reset["text"]
    assert reset["properties"] == []
    assert "budget_max" not in (reset["preferences"] or {})

    conv2 = _conv(last_shown_names=["Sea Villa", "City Condo"], last_result_ids=["sea", "city"])
    recalled = agent.run_conversation(
        db=MagicMock(),
        conversation=conv2,
        user_message="show me properties you showed in current chat",
        requested_language="en",
    )
    assert "Sea Villa" in recalled["text"]
    assert "City Condo" in recalled["text"]
    assert "current chat" not in str((recalled.get("preferences") or {}).get("location", "")).lower()

    conv3 = _conv()
    answer = agent.run_conversation(
        db=MagicMock(), conversation=conv3, user_message="can indian buy this?", requested_language="en",
    )
    assert "not legal advice" in answer["text"].lower()
    assert answer["properties"] == []
    agent.client.chat.completions.create.assert_not_called()


def test_replay_search_turns_use_filters_not_the_old_card(monkeypatch):
    agent = _agent(monkeypatch)
    seen = []

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        loc = (getattr(params, "location", None) or "").lower()
        max_price = getattr(params, "max_price", None)
        exclusive = bool(getattr(params, "max_price_exclusive", False))
        beds = getattr(params, "min_bedrooms", None) or getattr(params, "bedrooms", None)
        sort = getattr(params, "sort_by_price", None)
        seen.append({
            "location": loc,
            "max_price": float(max_price) if max_price is not None else None,
            "exclusive": exclusive,
            "beds": beds,
            "sort": sort,
            "exclude": list(exclude_property_ids or []),
            "status": getattr(params, "status", None),
        })
        if sort == "asc" and max_price is None:
            return [{"property_id": "cheap-1", "name": "Cheapest Suite", "price": "1.88M THB", "price_thb_min": 1_880_000, "location": "Bangkok"}]
        if max_price is not None and float(max_price) <= 1_000_000:
            return []
        if exclusive and max_price is not None:
            return [{"property_id": "under-5", "name": "Under Five", "price": "4.20M THB", "price_numeric_min": 4_200_000, "location": loc or "Phuket", "bedrooms": "2"}]
        if "phuket" in loc and beds:
            return []
        if "phuket" in loc:
            return [{"property_id": "phuket-1", "name": "Phuket Villa", "price": "8M THB", "location": "Phuket", "bedrooms": "3"}]
        if exclude_property_ids:
            return [{"property_id": "next-1", "name": "Next Listing", "price": "6M THB", "location": "Bangkok", "bedrooms": "2"}]
        return [
            {"property_id": "bkk-1", "name": "Bangkok One", "price": "9M THB", "location": "Bangkok", "bedrooms": "2"},
            {"property_id": "bkk-2", "name": "Bangkok Two", "price": "11M THB", "location": "Bangkok", "bedrooms": "3"},
        ]

    monkeypatch.setattr(public_agent_mod, "search_properties_unified", fake_search)
    conv = _conv(property_context={"active_property_id": "bkk-1", "active_property_reference": "Bangkok One"})

    hello = agent.run_conversation(db=MagicMock(), conversation=conv, user_message="hello", requested_language="en")
    assert hello.get("text")

    fresh = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="Explore luxury properties in Thailand", requested_language="en",
    )
    assert fresh.get("properties")
    assert "Bangkok One" in fresh["text"] or any(p.get("name") == "Bangkok One" for p in fresh["properties"])

    budgeted = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="under 5 million", requested_language="en",
    )
    assert budgeted.get("properties")
    assert all(
        (p.get("price_numeric_min") or 0) < 5_000_000
        for p in budgeted["properties"]
        if p.get("price_numeric_min")
    )

    narrowed = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="i want 4 bedroom and i want in phuket", requested_language="en",
    )
    phuket_calls = [call for call in seen if "phuket" in str(call["location"])]
    assert phuket_calls
    assert phuket_calls[-1]["max_price"] == 5_000_000
    assert phuket_calls[-1]["exclusive"] is True
    assert not any(p.get("property_id") == "bkk-1" for p in (narrowed.get("properties") or []))
    assert "Bangkok One" not in (narrowed.get("text") or "")

    low = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="any under 1 million", requested_language="en",
    )
    assert "1.88" in (low.get("text") or "")
    assert "cheapest" in (low.get("text") or "").lower() or "clear" in (low.get("text") or "").lower()

    free = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="free properties", requested_language="en",
    )
    assert "not free" in (free.get("text") or "").lower()

    featured = agent.run_conversation(
        db=MagicMock(),
        conversation=conv,
        user_message="Show all featured luxury properties in Thailand",
        requested_language="en",
    )
    assert featured.get("properties") or featured.get("text")
