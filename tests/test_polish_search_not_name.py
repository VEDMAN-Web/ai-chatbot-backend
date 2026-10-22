"""
PRIORITY 1: Polish filter searches must not be treated as property-name lookups.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.intent_router import (
    IntentType,
    detect_deterministic_intent,
    extract_search_filters_deterministic,
    message_looks_like_filter_search,
)


@pytest.mark.parametrize(
    "query,expect_max",
    [
        ("Pokaż wille w Phuket", None),
        ("Pokaż wille w Phuket do 15 mln THB", 15_000_000),
        ("Pokaz wille w Phuket do 15 mln THB", 15_000_000),
        ("Show me villas in Phuket under 15 million THB", 15_000_000),
    ],
)
def test_polish_villa_search_is_property_search_not_details(query, expect_max):
    intent, args = detect_deterministic_intent(query, {})
    assert intent == IntentType.PROPERTY_SEARCH, f"{query!r} -> {intent} {args}"
    assert args.get("location") == "Phuket"
    assert args.get("property_type") == "Villa"
    if expect_max is not None:
        assert args.get("max_price") == expect_max
    assert "property_name" not in args or not args.get("property_name")
    assert message_looks_like_filter_search(query) is True


def test_anava_and_satoshi_still_name_like_not_filter_search():
    assert message_looks_like_filter_search("Anava") is False
    assert message_looks_like_filter_search("Satoshi Residence") is False
    # Bare names: never a search. With an empty name index there is no deterministic intent (it is resolved
    # later); with a loaded index a confident name goes straight to that listing's details.
    intent_a, _ = detect_deterministic_intent("Anava", {})
    intent_s, _ = detect_deterministic_intent("Satoshi Residence", {})
    assert intent_a in (None, IntentType.PROPERTY_DETAILS)
    assert intent_s in (None, IntentType.PROPERTY_DETAILS)
    assert intent_a != IntentType.PROPERTY_SEARCH
    assert intent_s != IntentType.PROPERTY_SEARCH


def test_polish_filters_extract_budget_and_type():
    f = extract_search_filters_deterministic("Pokaż wille w Phuket do 15 mln THB")
    assert f["location"] == "Phuket"
    assert f["property_type"] == "Villa"
    assert f["max_price"] == 15_000_000


def test_name_not_found_falls_back_to_filter_search(monkeypatch):
    """If name match fails but message has filters, use filter search not name-not-found."""
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {"language": "pl"}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    fake_villas = [
        {
            "property_id": f"phuket-villa-{i}",
            "name": f"Phuket Villa {i}",
            "location": "Phuket",
            "property_type": "Villa",
            "price": f"from {10 + i}.00M THB",
            "price_numeric_min": (10 + i) * 1_000_000,
            "bedrooms": "3 Beds",
            "area": "180 sqm",
            "status": "Available",
            "highlights": [],
            "amenities": [],
            "tags": [],
        }
        for i in range(4)
    ]

    calls = {"name": 0, "filter": 0}

    def fake_name_search(*a, **k):
        calls["name"] += 1
        return []

    def fake_unified(db, params, limit=None, exclude_property_ids=None):
        calls["filter"] += 1
        out = [
            c for c in fake_villas
            if (params.location is None or "phuket" in (c["location"] or "").lower())
            and (params.max_price is None or (c.get("price_numeric_min") or 0) <= params.max_price)
        ]
        return out[: (limit or 4)]

    monkeypatch.setattr(
        "app.services.public_agent.search_properties_by_name",
        fake_name_search,
        raising=False,
    )
    # Name path inside execute_tool
    monkeypatch.setattr(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.search_properties_by_name",
        lambda *a, **k: [],
        raising=False,
    )
    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_unified)

    # Prefer calling the search branch the way PROPERTY_SEARCH does
    result, cards, _ = agent.execute_tool(
        db=MagicMock(),
        conversation=conversation,
        tool_name="search_properties",
        args={
            "location": "Phuket",
            "property_type": "Villa",
            "max_price": 15_000_000,
            # Simulate a mistaken name that should fall back
            "query": "wille w Phuket do 15 mln THB",
        },
    )
    assert cards, "Expected Phuket villa cards from filter fallback"
    assert all("phuket" in (c.get("location") or "").lower() for c in cards)
    assert all((c.get("price_numeric_min") or 0) <= 15_000_000 for c in cards)
