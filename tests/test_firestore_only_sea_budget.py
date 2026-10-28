"""
Post-F1 public chatbot fixes:
2) STORE=firestore never reads SQL; IndexUnavailable when client down with no cache
3) Short/partial names ("tell me about Sea") → Did you mean… + quick replies
4) Budget follow-up keeps city/type/status; zero results explain which filter to relax
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.services.answer_policy import did_you_mean_reply
from app.services.conversation_controls import merge_applied_filters
from app.services.intent_router import (
    IntentType,
    detect_deterministic_intent,
    extract_search_filters_deterministic,
)
from app.services.property_index import (
    IndexUnavailable,
    get_property_index,
    reset_property_index_for_tests,
)
from app.services.property_search import PropertyFilterParams, get_property_by_id, search_properties_safe
from app.services.public_agent import PublicAgentService, lookup_property_by_slug_or_name, search_properties_unified
from tests.test_property_index_f1 import FakeFirestore, _project


@pytest.fixture
def fake_fs(monkeypatch):
    reset_property_index_for_tests()
    fs = FakeFirestore()
    for doc_id, data in [
        _project("sea-breeze", name="Sea Breeze Villa", city="phuket", price=15_000_000, beds=3),
        _project("sea-view-condo", name="Sea View Condo Bangkok", city="bangkok", price=22_000_000, beds=2, ptype="Condominium", status="Ready to move in"),
        _project("ocean-sea", name="Ocean Sea Residence", city="phuket", price=18_000_000, beds=3),
        _project("bk-ready-low", name="Bangkok Ready Low", city="bangkok", price=12_000_000, beds=2, ptype="Condominium", status="Ready"),
        _project("bk-ready-mid", name="Bangkok Ready Mid", city="bangkok", price=25_000_000, beds=2, ptype="Condominium", status="Ready to move in"),
        _project("bk-offplan", name="Bangkok Offplan Tower", city="bangkok", price=24_000_000, beds=2, ptype="Condominium", status="Off-plan"),
        _project("p1", name="Alpha Villa Phuket", city="phuket", price=12_000_000, beds=3),
    ]:
        fs.set("projects", doc_id, data)
    fs.set("meta", "properties_version", {"version": 1, "updated_at": "2026-01-01T00:00:00Z"})

    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda target="user": fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda: fs)
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "firestore")

    idx = get_property_index()
    idx.set_client_override(fs)
    idx.ensure_fresh(force=True)
    yield fs
    reset_property_index_for_tests()


# ---------------------------------------------------------------------------
# 2) No silent SQL fallback
# ---------------------------------------------------------------------------

def test_firestore_mode_never_queries_sql(fake_fs, monkeypatch):
    db = MagicMock()
    db.query.side_effect = AssertionError("SQL must not be queried when STORE=firestore")

    params = PropertyFilterParams(location="Phuket", property_type="Villa", limit=4)
    cards = search_properties_unified(db, params, limit=4)
    assert cards
    assert any("Phuket" in str(c.get("location") or "") or "phuket" in str(c.get("district") or "").lower() for c in cards)

    safe = search_properties_safe(db, params)
    assert safe

    card = get_property_by_id(db, "p1")
    assert card is not None
    assert getattr(card, "property_id", None) == "p1" or getattr(card, "property_id", None) is None or True
    # Adapter exposes property_id attribute
    assert str(getattr(card, "property_id", "") or "") == "p1"

    looked = lookup_property_by_slug_or_name("p1")
    assert looked and looked.get("property_id") == "p1"
    db.query.assert_not_called()


def test_firestore_unavailable_raises_index_unavailable(fake_fs, monkeypatch):
    idx = get_property_index()
    idx.clear()
    idx.set_client_override(None)
    monkeypatch.setattr(
        "app.services.firestore_service.get_firestore_client",
        lambda target="user": None,
    )
    with pytest.raises(IndexUnavailable):
        idx.ensure_fresh(force=True)

    db = MagicMock()
    db.query.side_effect = AssertionError("SQL must not be used as fallback")
    with pytest.raises(IndexUnavailable):
        search_properties_unified(db, PropertyFilterParams(location="Phuket", limit=3), limit=3)


def test_recall_and_compare_use_property_index(fake_fs):
    idx = get_property_index()
    first = idx.get_by_id("p1")
    second = idx.get_by_id("sea-breeze")
    assert first and second
    cards = idx.get_by_ids(["p1", "sea-breeze"])
    assert len(cards) == 2
    assert {c["property_id"] for c in cards} == {"p1", "sea-breeze"}

    # Compare path: resolve last_result_ids via index without SQL
    db = MagicMock()
    db.query.side_effect = AssertionError("no SQL")
    from app.services.property_search import get_property_by_id as gp
    a = gp(db, "p1")
    b = gp(db, "sea-breeze")
    assert a is not None and b is not None


# ---------------------------------------------------------------------------
# 3) Partial name "Sea" → Did you mean
# ---------------------------------------------------------------------------

def test_tell_me_about_sea_did_you_mean(fake_fs, monkeypatch):
    intent, args = detect_deterministic_intent("tell me about Sea")
    assert intent == IntentType.PROPERTY_DETAILS
    assert args.get("name_uncertain") is True
    cands = list(args.get("name_candidates") or [])
    assert len(cands) >= 1
    joined = " ".join(cands).lower()
    assert "sea" in joined
    assert intent != IntentType.OFF_TOPIC

    reply = did_you_mean_reply(cands)
    assert reply.startswith("Did you mean")
    for name in cands[:3]:
        assert name in reply

    import app.services.public_agent as pa
    agent = PublicAgentService.__new__(PublicAgentService)
    monkeypatch.setattr(pa, "append_conversation_turn", lambda **_k: None)
    conversation = SimpleNamespace(
        id="c1",
        session_id="s-sea",
        site_origin="tajlandia.com",
        preferences={},
        message_history=[],
    )
    result = agent._handle_property_details(
        None,
        conversation,
        {
            "property_name": "Sea",
            "name_uncertain": True,
            "name_candidates": cands[:3],
        },
        "tell me about Sea",
        0.0,
        language="en",
    )
    assert result.get("fallback_type") == "did_you_mean"
    assert "did you mean" in (result.get("text") or "").lower()
    assert result.get("quick_replies") == cands[:3]
    assert "off-topic" not in (result.get("text") or "").lower()


# ---------------------------------------------------------------------------
# 4) Budget with context + relax messaging
# ---------------------------------------------------------------------------

def test_budget_keeps_city_type_status(fake_fs):
    msg = "condos in Bangkok, ready to move in, budget 20-30M"
    extracted = extract_search_filters_deterministic(msg)
    assert extracted.get("location") == "Bangkok"
    assert "Condo" in str(extracted.get("property_type") or "")
    assert extracted.get("status") == "Ready"
    assert extracted.get("min_price") == 20_000_000.0
    assert extracted.get("max_price") == 30_000_000.0

    # Follow-up: budget alone keeps prior city/type/status
    stored = {
        "location": "Bangkok",
        "property_type": "Condominium",
        "project_status": "Ready",
        "active_filters": {
            "location": "Bangkok",
            "property_type": "Condominium",
            "status": "Ready",
        },
    }
    budget_only = extract_search_filters_deterministic("budget 20-30M")
    merged = merge_applied_filters(stored, budget_only, "budget 20-30M")
    assert merged.get("location") == "Bangkok"
    assert merged.get("property_type") == "Condominium"
    assert merged.get("status") == "Ready"
    assert merged.get("min_price") == 20_000_000.0
    assert merged.get("max_price") == 30_000_000.0

    params = PropertyFilterParams(
        location=merged["location"],
        property_type=merged["property_type"],
        status=merged["status"],
        min_price=Decimal(str(merged["min_price"])),
        max_price=Decimal(str(merged["max_price"])),
        limit=10,
    )
    cards = search_properties_unified(MagicMock(), params, limit=10)
    # Mid-priced ready Bangkok condo should match; off-plan / low price should not
    ids = {c.get("property_id") for c in cards}
    assert "bk-ready-mid" in ids
    assert "bk-offplan" not in ids
    assert "bk-ready-low" not in ids


def test_zero_results_explain_budget_filter(fake_fs, monkeypatch):
    """When budget removes all matches, reply names the budget filter and offers to relax."""
    import app.services.public_agent as pa
    agent = PublicAgentService.__new__(PublicAgentService)
    monkeypatch.setattr(pa, "append_conversation_turn", lambda **_k: None)
    conversation = SimpleNamespace(
        id="c2",
        session_id="s-budget",
        site_origin="tajlandia.com",
        preferences={
            "location": "Bangkok",
            "property_type": "Condominium",
            "project_status": "Ready",
            "active_filters": {
                "location": "Bangkok",
                "property_type": "Condominium",
                "status": "Ready",
            },
        },
        message_history=[],
        language="en",
    )
    # Narrow band with no listings (ready Bangkok fixtures are 12M or 25M)
    result = agent._handle_property_search(
        MagicMock(),
        conversation,
        "budget 13-14M",
        0.0,
        language="en",
    )
    text = (result.get("text") or "").lower()
    assert "budget" in text
    assert "relax" in text or "relaxed" in text
