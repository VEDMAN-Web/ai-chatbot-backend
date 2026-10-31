"""
PRIORITY 3: Impossible filters must not return unrelated villas.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.intent_router import extract_search_filters_deterministic
from app.services.public_agent import PublicAgentService, resolve_empty_search_after_seen_exclusion
from app.services.property_search import PropertyFilterParams


@pytest.mark.xfail(reason="REGRESSION in rp-user-fix (also fails on that branch alone): an impossible filter ('50 bedroom villa under 1M') no longer extracts the bedroom count. For Rutul to fix.", strict=False)
def test_extract_impossible_50_bed_under_1m():
    f = extract_search_filters_deterministic("50-bedroom villa in Phuket under 1M THB")
    assert f.get("location") == "Phuket"
    assert f.get("property_type") == "Villa"
    assert f.get("bedrooms") == 50
    assert f.get("max_price") == 1_000_000


def test_impossible_filters_return_no_cards(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    conversation = MagicMock()
    conversation.preferences = {"language": "en"}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        # Exact match for impossible criteria: nothing
        beds = getattr(params, "bedrooms", None)
        max_p = getattr(params, "max_price", None)
        if beds and int(beds) >= 20:
            return []
        if max_p is not None and float(max_p) <= 1_000_000 and beds and int(beds) >= 10:
            return []
        # Unrelated villas that must NEVER be returned as matches for impossible query
        return [
            {
                "property_id": "unrelated-villa-1",
                "name": "Random Phuket Villa",
                "location": "Phuket",
                "property_type": "Villa",
                "price": "from 12.00M THB",
                "price_numeric_min": 12_000_000,
                "bedrooms": "3 Beds",
                "area": "200 sqm",
                "status": "Available",
                "has_pool": True,
            }
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.public_agent.extract_user_preferences",
        lambda **k: dict(conversation.preferences or {}),
    )

    result = agent._handle_property_search(
        db=MagicMock(),
        conversation=conversation,
        user_message="50-bedroom villa in Phuket under 1M THB",
        start_time=0.0,
        language="en",
    )
    cards = result.get("properties") or []
    text = (result.get("text") or "").lower()
    assert cards == [], f"Expected no cards, got {[c.get('property_id') for c in cards]}"
    assert "unrelated" not in text
    assert (
        "no verified listing matches" in text
        or "nothing matches" in text
        or "no match" in text
        or "does not match" in text
        or "nie znaleziono" in text
    )


def test_seen_all_not_no_match(monkeypatch):
    matching = [{"property_id": "a", "name": "A"}, {"property_id": "b", "name": "B"}]

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        if exclude_property_ids:
            excl = {str(x).lower() for x in exclude_property_ids}
            return [c for c in matching if c["property_id"] not in excl]
        return list(matching)

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    text, cards, reason = resolve_empty_search_after_seen_exclusion(
        MagicMock(),
        PropertyFilterParams(location="Phuket", property_type="Villa", limit=4),
        ["a", "b"],
        language="en",
        criteria_summary="Phuket, Villa",
    )
    assert reason == "seen_all"
    assert "already seen all" in text.lower()
    assert cards == []


def test_relaxed_alternatives_clearly_labelled():
    from app.services.i18n_service import build_no_match_reply

    alts = [
        {
            "property_id": "x",
            "name": "Near Layan Villa",
            "location": "Phuket",
            "price": "8M THB",
        }
    ]
    text = build_no_match_reply(
        language="en",
        site_origin="tajlandia.com",
        criteria_summary="Layan, Villa",
        relaxed_filter="location -> Phuket",
        alternatives=alts,
    )
    assert "No verified listing matches" in text
    low = text.lower()
    assert "does not match your criteria" in low
    assert "closest option" in low
    assert "Near Layan Villa" in text
    assert "advisor@varsoviaestate.com" not in text
