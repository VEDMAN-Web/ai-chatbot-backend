"""
TASK 3: any other / show others → SHOW_MORE with previous filters (never off-topic).
TASK 4: fuzzy city matching (puket → Phuket) before off-topic.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.intent_router import (
    IntentType,
    detect_deterministic_intent,
    extract_search_filters_deterministic,
    fuzzy_resolve_location,
    message_rejects_active_property,
)
from app.services.public_agent import PublicAgentService, is_off_topic_query


STATE_WITH_SEARCH = {
    "active_filters": {
        "location": "Phuket",
        "property_type": "Villa",
        "max_price": 15_000_000,
        "bedrooms": 3,
    },
    "location": "Phuket",
    "property_type": "Villa",
    "budget_max": 15_000_000,
    "bedrooms_num": 3,
    "last_result_ids": ["seen-1", "seen-2", "seen-3"],
    "seen_property_ids": ["seen-1", "seen-2", "seen-3"],
    "selected_property_ids": ["seen-1"],
    "property_context": {
        "active_property_id": "seen-1",
        "recently_discussed_ids": ["seen-1", "seen-2", "seen-3"],
        "reference_map": {},
    },
}


@pytest.mark.parametrize(
    "msg",
    [
        "show me any other properties",
        "any other properties instead of this?",
        "any other?",
        "something else",
        "different ones",
        "show me others",
        "I don't like this",
        "instead of this",
    ],
)
def test_show_more_phrases_are_show_more_not_details_or_off_topic(msg):
    intent, args = detect_deterministic_intent(msg, STATE_WITH_SEARCH)
    assert intent == IntentType.SHOW_MORE, msg
    assert intent != IntentType.PROPERTY_DETAILS
    assert intent != IntentType.OFF_TOPIC
    assert is_off_topic_query(msg) is False


def test_reject_active_flag_on_dislike_and_instead():
    _, args = detect_deterministic_intent(
        "any other properties instead of this?", STATE_WITH_SEARCH
    )
    assert args.get("reject_active") is True
    assert message_rejects_active_property("I don't like this") is True
    _, args2 = detect_deterministic_intent("show me any other properties", STATE_WITH_SEARCH)
    assert args2.get("reject_active") is False


def test_show_more_returns_unseen_cards_with_previous_filters(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = dict(STATE_WITH_SEARCH)
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    new_cards = [
        {
            "property_id": "new-a",
            "name": "Fresh Villa A",
            "location": "Phuket",
            "price": "10M",
            "property_type": "Villa",
            "bedrooms": "3",
            "has_pool": True,
        },
        {
            "property_id": "new-b",
            "name": "Fresh Villa B",
            "location": "Phuket",
            "price": "11M",
            "property_type": "Villa",
            "bedrooms": "3",
            "has_pool": True,
        },
    ]

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        # Must reuse previous filters
        assert params.location == "Phuket"
        assert params.property_type == "Villa"
        assert params.bedrooms == 3
        assert params.max_price == Decimal("15000000") or float(params.max_price) == 15_000_000
        excl = {str(x).lower() for x in (exclude_property_ids or [])}
        assert "seen-1" in excl and "seen-2" in excl
        return [c for c in new_cards if c["property_id"].lower() not in excl]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards))
    monkeypatch.setattr(
        "app.services.public_agent.build_rich_property_recommendation_text",
        lambda **kw: "Here are more listings",
    )
    monkeypatch.setattr(
        "app.services.public_agent.ensure_structured_response",
        lambda text, **kw: text,
    )

    result = agent._handle_show_more(
        db=MagicMock(),
        conversation=conversation,
        args={"reject_active": False, "reuse_active_filters": True},
        user_message="show me any other properties",
        start_time=0.0,
        language="en",
    )
    ids = [c["property_id"] for c in result["properties"]]
    assert ids
    assert "seen-1" not in ids
    assert "seen-2" not in ids
    assert set(ids) <= {"new-a", "new-b"}
    # Filters preserved
    prefs = conversation.preferences or {}
    assert (prefs.get("active_filters") or {}).get("location") == "Phuket"


def test_reject_active_excludes_current_property(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = dict(STATE_WITH_SEARCH)
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    captured = {}

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        captured["exclude"] = list(exclude_property_ids or [])
        return [
            {
                "property_id": "alt-1",
                "name": "Alt",
                "location": "Phuket",
                "price": "9M",
                "property_type": "Villa",
                "bedrooms": "3",
                "has_pool": False,
            }
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards))
    monkeypatch.setattr(
        "app.services.public_agent.build_rich_property_recommendation_text",
        lambda **kw: "Alternatives",
    )
    monkeypatch.setattr(
        "app.services.public_agent.ensure_structured_response",
        lambda text, **kw: text,
    )

    result = agent._handle_show_more(
        db=MagicMock(),
        conversation=conversation,
        args={"reject_active": True},
        user_message="I don't like this",
        start_time=0.0,
        language="en",
    )
    assert "seen-1" in [str(x) for x in captured.get("exclude", [])]
    assert result["properties"]
    assert result["properties"][0]["property_id"] == "alt-1"


# --- TASK 4 ---

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("puket", "Phuket"),
        ("phucket", "Phuket"),
        ("samui", "Koh Samui"),
        ("pattya", "Pattaya"),
        ("okay in puket?", "Phuket"),
    ],
)
def test_fuzzy_city_resolves(raw, expected):
    assert fuzzy_resolve_location(raw) == expected
    filters = extract_search_filters_deterministic(raw)
    assert filters.get("location") == expected


@pytest.mark.parametrize("raw", ["python", "hello", "banana", "xyzzy"])
def test_fuzzy_unrelated_words_do_not_resolve(raw):
    assert fuzzy_resolve_location(raw) is None


def test_puket_is_property_search_not_off_topic():
    intent, args = detect_deterministic_intent(
        "okay in puket?",
        STATE_WITH_SEARCH,
    )
    assert intent == IntentType.PROPERTY_SEARCH
    assert args.get("location") == "Phuket"
    assert is_off_topic_query("okay in puket?") is False
