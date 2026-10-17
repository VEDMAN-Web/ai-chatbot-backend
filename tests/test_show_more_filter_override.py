"""
TASK 1: "show me other under 20 million" must override stored budget, not reuse 40M.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.intent_router import (
    IntentType,
    detect_deterministic_intent,
    extract_search_filters_deterministic,
)
from app.services.public_agent import PublicAgentService


STATE_40M_PHUKET = {
    "active_filters": {
        "location": "Phuket",
        "property_type": "Villa",
        "max_price": 40_000_000,
    },
    "location": "Phuket",
    "property_type": "Villa",
    "budget_max": 40_000_000,
    "last_result_ids": ["seen-40a", "seen-40b"],
    "seen_property_ids": ["seen-40a", "seen-40b"],
    "pagination_offset": 4,
    "limit": 4,
}


def test_extract_finds_20m_in_show_other_message():
    f = extract_search_filters_deterministic("okay now show me other under 20 million")
    assert f.get("max_price") == 20_000_000.0


def test_show_other_under_20m_carries_budget_override():
    intent, args = detect_deterministic_intent(
        "okay now show me other under 20 million",
        STATE_40M_PHUKET,
    )
    assert intent == IntentType.SHOW_MORE
    overrides = args.get("filter_overrides") or {}
    assert float(overrides.get("max_price") or 0) == 20_000_000.0
    # New budget → fresh offset (not paginating the old 40M list)
    assert int(args.get("offset") or 0) == 0


def test_show_other_alone_reuses_filters_no_override():
    intent, args = detect_deterministic_intent("show me other", STATE_40M_PHUKET)
    assert intent == IntentType.SHOW_MORE
    overrides = args.get("filter_overrides") or {}
    assert "max_price" not in overrides
    assert "location" not in overrides
    assert args.get("reuse_active_filters") is True


def test_show_other_in_samui_overrides_location_keeps_budget_path():
    intent, args = detect_deterministic_intent(
        "show me other in Samui",
        STATE_40M_PHUKET,
    )
    assert intent == IntentType.SHOW_MORE
    overrides = args.get("filter_overrides") or {}
    assert overrides.get("location") in ("Koh Samui", "Samui")
    assert "max_price" not in overrides  # budget stays from stored


def test_handle_show_more_applies_20m_and_keeps_phuket(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = dict(STATE_40M_PHUKET)
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    captured = {}

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        captured["params"] = params
        captured["exclude"] = list(exclude_property_ids or [])
        # Only return listings that satisfy the NEW budget
        price = float(params.max_price) if params.max_price is not None else None
        assert price == 20_000_000.0
        assert params.location == "Phuket"
        return [
            {
                "property_id": "under-20a",
                "name": "Affordable Villa",
                "location": "Phuket",
                "price": "18M THB",
                "price_thb": 18_000_000,
                "property_type": "Villa",
                "bedrooms": "3",
                "has_pool": True,
            }
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards))
    monkeypatch.setattr(
        "app.services.public_agent.build_rich_property_recommendation_text",
        lambda **kw: "Here are options under 20M",
    )
    monkeypatch.setattr(
        "app.services.public_agent.ensure_structured_response",
        lambda text, **kw: text,
    )

    result = agent._handle_show_more(
        db=MagicMock(),
        conversation=conversation,
        args={
            "reject_active": False,
            "reuse_active_filters": True,
            "filter_overrides": {"max_price": 20_000_000.0},
            "filters_changed": True,
            "offset": 0,
        },
        user_message="okay now show me other under 20 million",
        start_time=0.0,
        language="en",
    )

    prefs = conversation.preferences or result.get("preferences") or {}
    af = prefs.get("active_filters") or {}
    assert af.get("location") == "Phuket"
    assert float(af.get("max_price")) == 20_000_000.0
    assert float(prefs.get("budget_max") or 0) == 20_000_000.0
    assert "no listing matches" not in (result.get("text") or "").lower()
    for card in result["properties"]:
        # cards from fake_search are already under 20M; assert price field if numeric
        p = card.get("price_thb")
        if p is not None:
            assert float(p) <= 20_000_000


def test_handle_show_more_alone_excludes_seen(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = dict(STATE_40M_PHUKET)
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    captured = {}

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        captured["params"] = params
        captured["exclude"] = {str(x).lower() for x in (exclude_property_ids or [])}
        assert float(params.max_price) == 40_000_000.0
        assert params.location == "Phuket"
        return [
            {
                "property_id": "new-40",
                "name": "Next Villa",
                "location": "Phuket",
                "price": "35M",
                "property_type": "Villa",
                "bedrooms": "3",
                "has_pool": True,
            }
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards))
    monkeypatch.setattr(
        "app.services.public_agent.build_rich_property_recommendation_text",
        lambda **kw: "More under 40M",
    )
    monkeypatch.setattr(
        "app.services.public_agent.ensure_structured_response",
        lambda text, **kw: text,
    )

    agent._handle_show_more(
        db=MagicMock(),
        conversation=conversation,
        args={"reject_active": False, "reuse_active_filters": True},
        user_message="show me other",
        start_time=0.0,
        language="en",
    )
    assert "seen-40a" in captured["exclude"]
    assert "seen-40b" in captured["exclude"]


def test_handle_show_more_samui_keeps_budget(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = dict(STATE_40M_PHUKET)
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        assert params.location in ("Koh Samui", "Samui")
        assert float(params.max_price) == 40_000_000.0
        return [
            {
                "property_id": "samui-1",
                "name": "Samui Villa",
                "location": "Koh Samui",
                "price": "30M",
                "property_type": "Villa",
                "bedrooms": "3",
                "has_pool": True,
            }
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards))
    monkeypatch.setattr(
        "app.services.public_agent.build_rich_property_recommendation_text",
        lambda **kw: "Samui options",
    )
    monkeypatch.setattr(
        "app.services.public_agent.ensure_structured_response",
        lambda text, **kw: text,
    )

    result = agent._handle_show_more(
        db=MagicMock(),
        conversation=conversation,
        args={
            "reject_active": False,
            "reuse_active_filters": True,
            "filter_overrides": {"location": "Koh Samui"},
            "filters_changed": True,
            "offset": 0,
        },
        user_message="show me other in Samui",
        start_time=0.0,
        language="en",
    )
    af = (conversation.preferences or {}).get("active_filters") or {}
    assert af.get("location") == "Koh Samui"
    assert float(af.get("max_price")) == 40_000_000.0
    assert result["properties"][0]["property_id"] == "samui-1"
