"""
TASK 5: Unified amenity helper for cards + focused answers.
TASK 6: Canonical amenity map, gym filter, honest none-match, RU slug mapping.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.amenity_service import (
    apply_amenity_labels,
    collect_property_amenities,
    enrich_card_amenity_flags,
    extract_requested_amenities,
    filter_cards_by_amenities,
    map_token_to_canonical,
    property_has_amenity,
)
from app.services.i18n_service import build_amenity_none_match_reply
from app.services.intent_router import extract_search_filters_deterministic
from app.services.public_agent import PublicAgentService


def test_parking_in_highlights_answers_yes():
    card = {
        "property_id": "p1",
        "name": "Test Villa",
        "highlights": ["Sea view", "Parking", "Garden"],
        "amenities": [],
        "tags": [],
        "has_pool": False,
        "beach_access": False,
    }
    facts = collect_property_amenities(card)
    assert facts["parking"].present is True
    assert "highlights" in facts["parking"].sources
    assert property_has_amenity(card, "parking") is True


def test_no_parking_data_answers_not_in_verified():
    card = {
        "property_id": "p2",
        "name": "Bare Condo",
        "highlights": ["City view"],
        "amenities": [],
        "tags": [],
        "description": "Modern condo in Bangkok",
    }
    facts = collect_property_amenities(card)
    assert facts["parking"].present is False
    assert property_has_amenity(card, "parking") is False


def test_beach_access_from_tags_and_flags():
    card = {
        "highlights": [],
        "tags": ["beach-access", "swimming-pool"],
        "amenities": [],
        "beach_access": False,
        "has_pool": False,
    }
    enrich_card_amenity_flags(card)
    assert card["beach_access"] is True
    assert card["has_pool"] is True
    assert "beach_access" in card["amenities_canonical"]
    assert "pool" in card["amenities_canonical"]


def test_detail_parking_question_uses_helper(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {"language": "en", "property_context": {}}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    card = {
        "property_id": "isla-1",
        "name": "Isla Kata",
        "location": "Kata",
        "price": "10M THB",
        "bedrooms": "3",
        "area": "200",
        "status": "Available",
        "highlights": ["Parking", "Beach access"],
        "amenities": ["parking"],
        "tags": ["beach-access"],
        "description": "",
        "has_pool": False,
        "beach_access": False,
    }

    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: card if "isla" in str(tok).lower() else None,
    )
    monkeypatch.setattr("app.services.public_agent.extract_explicit_property_token", lambda *a, **k: "isla-1")
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", lambda **_k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args={"property_id": "isla-1", "focus_question": "is there any parking?"},
        user_message="is there any parking?",
        start_time=0.0,
        language="en",
    )
    text = (result["text"] or "").lower()
    assert "confirms parking" in text or "yes" in text
    assert "not documented" not in text
    assert "not in our verified data" not in text


def test_russian_slug_maps_to_running_track():
    assert map_token_to_canonical("begovaya-dorozhka") == "running_track"
    assert map_token_to_canonical("plavatelnyy-basseyn") == "pool"
    assert map_token_to_canonical("laundzh") == "lounge"


def test_gym_filter_returns_only_matching():
    cards = [
        {"property_id": "g1", "name": "Gym Tower", "tags": ["gym", "fitness"], "highlights": []},
        {"property_id": "n1", "name": "777 Beach Condo", "tags": ["beach"], "highlights": ["Sea view"]},
        {"property_id": "n2", "name": "Aceller", "tags": ["laundzh"], "highlights": []},
    ]
    matching, non = filter_cards_by_amenities(cards, ["gym"])
    assert [c["property_id"] for c in matching] == ["g1"]
    assert {c["property_id"] for c in non} == {"n1", "n2"}
    labelled = apply_amenity_labels(matching, ["gym"], language="en")
    assert labelled[0]["amenity_match"] is True
    assert any("Gym ✓" in str(x) for x in labelled[0].get("amenity_labels") or [])


def test_gym_none_match_honest_message_and_labels():
    alts = apply_amenity_labels(
        [
            {"property_id": "n1", "name": "777 Beach Condo", "location": "Phuket", "tags": ["beach"]},
            {"property_id": "n2", "name": "Aceller", "location": "Phuket", "tags": ["laundzh"]},
        ],
        ["gym"],
        language="en",
    )
    for c in alts:
        assert c["amenity_match"] is False
        assert any("not listed" in str(x).lower() for x in c.get("amenity_labels") or [])
    text = build_amenity_none_match_reply(
        language="en",
        location="Phuket",
        amenity_label="Gym",
        alternatives=alts,
    )
    assert "I couldn't confirm a gym" in text
    assert "closest alternatives" in text.lower()
    assert "777 Beach Condo" in text


def test_extract_gym_request_into_filters():
    assert "gym" in extract_requested_amenities("show me properties with a personal gym")
    filters = extract_search_filters_deterministic("properties with a gym in Phuket")
    assert filters.get("location") == "Phuket"
    assert "gym" in (filters.get("required_amenities") or [])


def test_description_fills_missing_tag_traceable():
    card = {
        "tags": [],
        "amenities": [],
        "highlights": [],
        "description": "The project includes a private gym and spa for residents.",
    }
    facts = collect_property_amenities(card)
    assert facts["gym"].present is True
    assert "description" in facts["gym"].sources
    assert facts["spa"].present is True
