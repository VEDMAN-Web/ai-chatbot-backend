"""
TASK 1: Explicit slug/name overrides active property.
TASK 2: Seen-all vs true no-match search replies.
No live Firestore/Postgres required.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.i18n_service import build_no_match_reply, build_seen_all_reply
from app.services.public_agent import (
    PublicAgentService,
    extract_explicit_property_token,
    lookup_property_by_slug_or_name,
    resolve_empty_search_after_seen_exclusion,
)
from app.services.property_search import PropertyFilterParams
from app.services.property_context_service import PropertyContext, set_active_property


# ---------------------------------------------------------------------------
# TASK 1 helpers
# ---------------------------------------------------------------------------

def test_extract_explicit_token_view_details_for_slug():
    tok = extract_explicit_property_token(
        "View details for isla-kata-sea-view-villa",
        {},
    )
    assert tok == "isla-kata-sea-view-villa"


def test_extract_explicit_token_from_args_slug():
    tok = extract_explicit_property_token(
        "show me something",
        {"property_id": "isla-kata-sea-view-villa", "explicit_property_token": "isla-kata-sea-view-villa"},
    )
    assert tok == "isla-kata-sea-view-villa"


def test_lookup_slug_not_active_property(monkeypatch):
    active_card = {
        "property_id": "modern-semi-detached-pool-villas",
        "name": "Modern Semi-Detached Pool Villas",
    }
    isla_card = {
        "property_id": "isla-kata-sea-view-villa",
        "name": "Isla Kata Sea View Villa",
        "location": "Kata, Phuket",
        "price": "12,000,000 THB",
    }

    def fake_get(pid, fs_target="user"):
        key = str(pid).lower()
        if key == "isla-kata-sea-view-villa":
            return isla_card
        if key == "modern-semi-detached-pool-villas":
            return active_card
        return None

    monkeypatch.setattr(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        fake_get,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.ensure_name_index_from_firestore",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.match_property_name",
        lambda q: SimpleNamespace(confident=None, candidates=[]),
    )
    got = lookup_property_by_slug_or_name("isla-kata-sea-view-villa")
    assert isinstance(got, dict)
    assert got["property_id"] == "isla-kata-sea-view-villa"
    assert "Modern" not in (got.get("name") or "")


def test_property_details_slug_overrides_active(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"

    target_card = {
        "property_id": "isla-kata-sea-view-villa",
        "name": "Isla Kata Sea View Villa",
        "location": "Kata, Phuket",
        "price": "12,000,000 THB",
        "bedrooms": "3",
        "area": "200 sqm",
        "status": "Available",
        "highlights": [],
        "description": "",
        "has_pool": True,
        "beach_access": False,
    }

    conversation = MagicMock()
    conversation.preferences = {
        "language": "en",
        "selected_property_ids": ["modern-semi-detached-pool-villas"],
        "property_context": {
            "active_property_id": "modern-semi-detached-pool-villas",
            "active_property_reference": "Modern Semi-Detached Pool Villas",
            "recently_discussed_ids": ["modern-semi-detached-pool-villas"],
            "reference_map": {
                "Modern Semi-Detached Pool Villas": "modern-semi-detached-pool-villas",
            },
        },
    }
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: target_card if tok == "isla-kata-sea-view-villa" else None,
    )
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", lambda **_k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args={
            "property_id": "isla-kata-sea-view-villa",
            "property_name": "isla-kata-sea-view-villa",
            "explicit_property_token": "isla-kata-sea-view-villa",
            "from_property_context": False,
        },
        user_message="View details for isla-kata-sea-view-villa",
        start_time=0.0,
        language="en",
    )
    assert result["properties"]
    assert result["properties"][0]["property_id"] == "isla-kata-sea-view-villa"
    assert "Modern Semi-Detached" not in (result["text"] or "")
    assert "Isla Kata" in (result["text"] or "") or "isla-kata" in (result["text"] or "").lower()
    # Active property updated to the resolved slug
    prefs = conversation.preferences or {}
    pctx = prefs.get("property_context") or {}
    assert pctx.get("active_property_id") == "isla-kata-sea-view-villa"


def test_property_details_unknown_slug_honest_not_found(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"

    conversation = MagicMock()
    conversation.preferences = {
        "language": "en",
        "property_context": {
            "active_property_id": "modern-semi-detached-pool-villas",
            "recently_discussed_ids": ["modern-semi-detached-pool-villas"],
            "reference_map": {},
        },
    }
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

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args={
            "property_id": "totally-unknown-slug-xyz",
            "explicit_property_token": "totally-unknown-slug-xyz",
        },
        user_message="View details for totally-unknown-slug-xyz",
        start_time=0.0,
        language="en",
    )
    assert result["properties"] == []
    assert "not found" in (result["text"] or "").lower() or "not in our verified" in (result["text"] or "").lower()
    assert "Modern Semi-Detached" not in (result["text"] or "")


# ---------------------------------------------------------------------------
# TASK 2 helpers
# ---------------------------------------------------------------------------

def test_build_seen_all_reply_en_pl_th():
    en = build_seen_all_reply("en", criteria_summary="Koh Samui", total_matching=5)
    assert "already seen all 5" in en.lower()
    assert "Koh Samui" in en
    assert "Relax the budget" in en

    pl = build_seen_all_reply("pl", criteria_summary="Phuket, budget <= 5000000", total_matching=3)
    assert "Widziałeś już wszystkie 3" in pl
    assert "poluzuj budżet" in pl.lower() or "budżet" in pl.lower()

    th = build_seen_all_reply("th", criteria_summary="Phuket", total_matching=4)
    assert "4" in th
    assert "งบประมาณ" in th


def test_resolve_empty_search_seen_all(monkeypatch):
    matching = [
        {"property_id": "a1", "name": "A"},
        {"property_id": "a2", "name": "B"},
        {"property_id": "a3", "name": "C"},
    ]
    seen = ["a1", "a2", "a3"]

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        if exclude_property_ids:
            return []
        return list(matching)

    monkeypatch.setattr(
        "app.services.public_agent.search_properties_unified",
        fake_search,
    )
    params = PropertyFilterParams(location="Koh Samui", limit=4)
    text, cards, reason = resolve_empty_search_after_seen_exclusion(
        MagicMock(),
        params,
        seen,
        language="en",
        criteria_summary="Koh Samui",
    )
    assert reason == "seen_all"
    assert cards == []
    assert "already seen all 3" in text.lower()
    assert "no verified listing matches" not in text.lower()


def test_resolve_empty_search_true_no_match(monkeypatch):
    def fake_search(db, params, limit=None, exclude_property_ids=None):
        return []

    monkeypatch.setattr(
        "app.services.public_agent.search_properties_unified",
        fake_search,
    )
    params = PropertyFilterParams(location="Koh Samui", max_price=Decimal("1"), limit=4)
    text, cards, reason = resolve_empty_search_after_seen_exclusion(
        MagicMock(),
        params,
        ["x1"],
        language="en",
        criteria_summary="Koh Samui, budget <= 1",
    )
    assert reason == "no_match"
    assert cards == []
    assert "No verified listing matches" in text
    assert "already seen" not in text.lower()


def test_show_more_uses_seen_all_reply(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {
        "active_filters": {"location": "Koh Samui"},
        "location": "Koh Samui",
        "seen_property_ids": ["p1", "p2"],
        "last_result_ids": ["p1", "p2"],
    }
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    monkeypatch.setattr(
        "app.services.public_agent.get_session_seen_property_ids",
        lambda _c: ["p1", "p2"],
    )

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        if exclude_property_ids:
            return []
        return [
            {"property_id": "p1", "name": "One", "location": "Koh Samui", "price": "1"},
            {"property_id": "p2", "name": "Two", "location": "Koh Samui", "price": "2"},
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(
        agent,
        "_validate_or_fallback",
        lambda text, cards, **kw: (text, cards),
    )

    result = agent._handle_show_more(
        db=MagicMock(),
        conversation=conversation,
        args={},
        user_message="show me more",
        start_time=0.0,
        language="en",
    )
    assert result["properties"] == []
    assert "already seen" in (result["text"] or "").lower()
    assert "no verified listing matches" not in (result["text"] or "").lower()
