"""
TASK 2: Fact questions about a named property must not become a filtered search.
"""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.intent_router import IntentType, detect_deterministic_intent
from app.services.property_name_index import get_property_name_index, set_last_name_search_meta
from app.services.public_agent import PublicAgentService

FIXTURE = Path(__file__).parent / "fixtures" / "listings_snapshot_2026-10-16.csv"

STATE_WITH_BUDGET = {
    "active_filters": {
        "location": "Phuket",
        "property_type": "Villa",
        "max_price": 40_000_000,
    },
    "location": "Phuket",
    "property_type": "Villa",
    "budget_max": 40_000_000,
}


@pytest.fixture(scope="module")
def csv_rows():
    assert FIXTURE.is_file(), f"Missing fixture: {FIXTURE}"
    with FIXTURE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def name_index(csv_rows):
    idx = get_property_name_index()
    idx.clear()
    idx.load_from_rows(csv_rows)
    set_last_name_search_meta(None, [])
    yield idx
    set_last_name_search_meta(None, [])


@pytest.mark.parametrize(
    "msg,expect_name_part",
    [
        ("is Above Element Villas have spa?", "above element"),
        ("does Pirak Villa have parking?", "pirak"),
        ("does Botanica Four Seasons have a pool?", "botanica"),
    ],
)
def test_named_fact_questions_are_property_details_not_search(name_index, msg, expect_name_part):
    intent, args = detect_deterministic_intent(msg, STATE_WITH_BUDGET)
    assert intent == IntentType.PROPERTY_DETAILS, f"{msg} -> {intent} {args}"
    assert intent != IntentType.PROPERTY_SEARCH
    blob = " ".join(
        str(args.get(k) or "") for k in ("property_id", "property_name", "focus_question")
    ).lower()
    assert expect_name_part in blob or expect_name_part.replace(" ", "-") in blob
    # Must not carry stored search filters into the details args
    assert args.get("max_price") is None
    assert args.get("required_amenities") is None or intent == IntentType.PROPERTY_DETAILS


def test_show_me_villas_in_phuket_still_search(name_index):
    intent, args = detect_deterministic_intent("show me villas in Phuket", {})
    assert intent == IntentType.PROPERTY_SEARCH
    assert args.get("location") == "Phuket"
    assert args.get("property_type") == "Villa"


def test_unknown_named_fact_is_details_not_search(name_index):
    """Unknown proper names go to details (honest not-found), never a filtered search."""
    intent, args = detect_deterministic_intent(
        "does Zorblax Towers have a gym?",
        STATE_WITH_BUDGET,
    )
    assert intent == IntentType.PROPERTY_DETAILS
    assert intent != IntentType.PROPERTY_SEARCH
    name = (args.get("property_name") or args.get("property_id") or "").lower()
    assert "zorblax" in name
    assert args.get("ignore_stored_filters") is True
    assert args.get("max_price") is None


def test_above_element_spa_reply_is_about_property_not_no_match(name_index, monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    conversation = MagicMock()
    conversation.preferences = dict(STATE_WITH_BUDGET)
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    card = {
        "property_id": "above-element-villas",
        "name": "Above Element Villas",
        "location": "Phuket",
        "price": "39M THB",
        "bedrooms": "4",
        "amenities": ["pool", "parking"],
        "highlights": ["Private pool"],
        "has_pool": True,
        "description": "Hillside villas.",
        "status": "Available",
    }

    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: card if "above" in str(tok).lower() or "element" in str(tok).lower() else None,
    )
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", lambda **_k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))

    intent, args = detect_deterministic_intent(
        "is Above Element Villas have spa?",
        STATE_WITH_BUDGET,
    )
    assert intent == IntentType.PROPERTY_DETAILS

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args=args,
        user_message="is Above Element Villas have spa?",
        start_time=0.0,
        language="en",
    )
    text = (result.get("text") or "").lower()
    assert "no verified listing matches" not in text
    assert "no listing matches" not in text
    assert "budget <=" not in text
    # About this property (name or honest amenity answer)
    assert "above element" in text or "spa" in text or "not documented" in text or "not in" in text


def test_zorblax_returns_honest_not_found(name_index, monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = dict(STATE_WITH_BUDGET)
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: None,
    )
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])

    intent, args = detect_deterministic_intent(
        "does Zorblax Towers have a gym?",
        STATE_WITH_BUDGET,
    )
    assert intent == IntentType.PROPERTY_DETAILS

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args=args,
        user_message="does Zorblax Towers have a gym?",
        start_time=0.0,
        language="en",
    )
    text = (result.get("text") or "").lower()
    assert "zorblax" in text
    assert "no verified listing matches your criteria" not in text
    assert result.get("properties") == [] or not result.get("properties")
