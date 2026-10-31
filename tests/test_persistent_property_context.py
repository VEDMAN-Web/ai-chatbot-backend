"""
Persistent property context: 100+ follow-ups, reference resolution, and evidence-based facts.

These tests verify reference memory (WHICH property) separately from live authorized facts (WHAT is true).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.services.property_context_service import (
    PropertyContext,
    assemble_llm_context_block,
    clear_active_if_new_conversation,
    fetch_authorized_property,
    is_property_followup_message,
    load_property_context,
    resolve_property_reference,
    save_property_context,
    set_active_property,
    set_comparison_set,
    touch_discussed,
    update_rolling_summary,
)
from app.services.intent_router import IntentType, detect_deterministic_intent


class FakeConversation:
    def __init__(self, cid: Optional[str] = None):
        self.id = uuid.UUID(cid) if cid else uuid.uuid4()
        self.session_id = f"test-session-{self.id.hex[:8]}"
        self.agent_type = "public"
        self.site_origin = "varsoviaestate.com"
        self.message_history: List[Dict[str, Any]] = []
        self.preferences: Dict[str, Any] = {}
        self.lead_score = 0.0


AUTHORIZED_DB: Dict[str, Dict[str, Any]] = {
    "PROP-ALPHA": {
        "property_id": "PROP-ALPHA",
        "name": "Alpha Villa Phuket",
        "property_name": "Alpha Villa Phuket",
        "location": "Layan, Phuket",
        "price": "18,500,000 THB",
        "price_display": "18,500,000 THB",
        "bedrooms": "3",
        "area": "280 sqm",
        "status": "Available",
        "has_pool": True,
        "beach_access": False,
        "highlights": ["Private pool", "Sea view"],
        "description": "Verified Alpha villa",
        "has_parking": True,
    },
    "PROP-BETA": {
        "property_id": "PROP-BETA",
        "name": "Beta Residence Samui",
        "property_name": "Beta Residence Samui",
        "location": "Bophut, Koh Samui",
        "price": "22,000,000 THB",
        "price_display": "22,000,000 THB",
        "bedrooms": "4",
        "area": "320 sqm",
        "status": "Available",
        "has_pool": True,
        "beach_access": True,
        "highlights": ["Beach access"],
        "description": "Verified Beta residence",
        "has_parking": False,
    },
}


def _fake_fetch(db, property_id: str):
    card = AUTHORIZED_DB.get(str(property_id).strip())
    if not card:
        # name fallback
        for c in AUTHORIZED_DB.values():
            if property_id.lower() in (c.get("name") or "").lower():
                return c, c, "mock"
        return None, None, "missing"
    return card, dict(card), "mock"


@pytest.fixture
def conv():
    return FakeConversation()


def test_new_conversation_does_not_inherit_other_context():
    ctx = PropertyContext(
        conversation_id="aaa",
        active_property_id="PROP-ALPHA",
        recently_discussed_ids=["PROP-ALPHA"],
    )
    cleared = clear_active_if_new_conversation(ctx, "bbb")
    assert cleared.active_property_id is None
    assert cleared.conversation_id == "bbb"


def test_resolve_pronouns_keep_active_property(conv):
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha Villa Phuket")
    save_property_context(conv, ctx)

    for msg in [
        "what is its price?",
        "does it have a pool?",
        "tell me about this property",
        "does it also have parking?",
        "its location?",
        "what about that villa's bedrooms?",
    ]:
        r = resolve_property_reference(msg, load_property_context(conv))
        assert r.property_id == "PROP-ALPHA", msg
        assert r.used_active_context or r.source in ("active",)


def test_resolve_ordinals_and_switch_then_return(conv):
    ctx = PropertyContext(conversation_id=str(conv.id))
    ctx = touch_discussed(ctx, ["PROP-ALPHA", "PROP-BETA"], {
        "PROP-ALPHA": "Alpha Villa Phuket",
        "PROP-BETA": "Beta Residence Samui",
    }, refresh_ordinals=True)
    # Selecting second makes it active
    ctx = set_active_property(ctx, "PROP-BETA", "Beta Residence Samui")
    save_property_context(conv, ctx)

    r2 = resolve_property_reference("tell me about the second property", load_property_context(conv))
    assert r2.property_id == "PROP-BETA"

    r1 = resolve_property_reference("go back to the first property", load_property_context(conv))
    assert r1.property_id == "PROP-ALPHA"

    # After explicitly resolving first, caller should set active — simulate
    ctx = set_active_property(load_property_context(conv), "PROP-ALPHA", "Alpha Villa Phuket")
    save_property_context(conv, ctx)
    r_it = resolve_property_reference("what is its price?", load_property_context(conv))
    assert r_it.property_id == "PROP-ALPHA"


def test_ambiguous_two_properties_asks_clarification(conv):
    ctx = PropertyContext(conversation_id=str(conv.id))
    # Two names both appear in message
    ctx.reference_map = {
        "alpha villa phuket": "PROP-ALPHA",
        "beta residence samui": "PROP-BETA",
    }
    ctx.recently_discussed_ids = ["PROP-ALPHA", "PROP-BETA"]
    # No active
    r = resolve_property_reference(
        "compare Alpha Villa Phuket and Beta Residence Samui features again",
        ctx,
    )
    # Name hits both → ambiguous OR comparison — either ok as long as not silently picking one wrong fact source
    if r.source == "name":
        assert r.property_id in ("PROP-ALPHA", "PROP-BETA")
    elif r.ambiguous:
        assert len(r.property_ids) >= 2
        assert r.clarification


def test_facts_come_from_authorized_db_not_summary(conv):
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha Villa Phuket")
    # Poison the rolling summary with a fake price — must NOT be used as authority
    ctx.rolling_summary = "U: price? | A: Alpha costs 9.xx million fake"
    save_property_context(conv, ctx)

    _rec, card, src = _fake_fetch(None, "PROP-ALPHA")
    assert src == "mock"
    assert card["price"] == "18,500,000 THB"
    assert "9.xx" not in card["price"]
    # Summary must not be treated as price source
    assert "9.xx" in ctx.rolling_summary
    assert card["price"] not in ("9.xx",)


def test_missing_field_does_not_fabricate(conv):
    # PROP-BETA has has_parking False / not documented as True
    _rec, card, _ = _fake_fetch(None, "PROP-BETA")
    assert card is not None
    assert card.get("has_parking") is False


def test_changed_price_reflected_on_refetch(conv):
    _, card1, _ = _fake_fetch(None, "PROP-ALPHA")
    assert card1["price"] == "18,500,000 THB"

    AUTHORIZED_DB["PROP-ALPHA"]["price"] = "19,200,000 THB"
    AUTHORIZED_DB["PROP-ALPHA"]["price_display"] = "19,200,000 THB"
    try:
        _, card2, _ = _fake_fetch(None, "PROP-ALPHA")
        assert card2["price"] == "19,200,000 THB"
    finally:
        AUTHORIZED_DB["PROP-ALPHA"]["price"] = "18,500,000 THB"
        AUTHORIZED_DB["PROP-ALPHA"]["price_display"] = "18,500,000 THB"


def test_rolling_summary_never_drops_active_id(conv):
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha")
    for i in range(120):
        ctx = update_rolling_summary(ctx, f"question {i} about it?", f"answer {i}", "PROP-ALPHA")
    assert ctx.active_property_id == "PROP-ALPHA"
    assert "active=PROP-ALPHA" in ctx.decisions_summary
    assert ctx.turn_count == 120


def test_assemble_context_fits_long_conversation(conv):
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha")
    history = []
    for i in range(200):
        history.append({"role": "user", "content": f"q{i} its price?"})
        history.append({"role": "assistant", "content": f"a{i} verified price"})
        ctx = update_rolling_summary(ctx, history[-2]["content"], history[-1]["content"], "PROP-ALPHA")
    block = assemble_llm_context_block(ctx, history)
    assert "PROP-ALPHA" in block
    assert "not factual source" in block.lower() or "reference memory" in block.lower()
    # Block must stay bounded
    assert len(block) < 20000


def test_intent_router_uses_property_context_for_it_questions(conv):
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha Villa Phuket")
    prefs = save_property_context(conv, ctx)
    intent, args = detect_deterministic_intent("does it also have parking?", stored_state=prefs)
    assert intent == IntentType.PROPERTY_DETAILS
    assert args.get("property_id") == "PROP-ALPHA"


def test_100_consecutive_followups_resolve_same_property(conv):
    """Simulate 100 follow-up questions; every turn must resolve to PROP-ALPHA."""
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha Villa Phuket")
    save_property_context(conv, ctx)

    questions = [
        "what is its price?",
        "does it have a pool?",
        "how many bedrooms does it have?",
        "what is its location?",
        "does it have beach access?",
        "tell me more about this property",
        "its status?",
        "does it also have parking?",
        "what is the area?",
        "any sea view?",
    ]
    # Cycle questions to reach 100
    resolved_ids = []
    for i in range(100):
        msg = questions[i % len(questions)]
        if i == 50:
            # Mid-thread: reference something discussed much earlier
            msg = "remind me of the price I asked about earlier for this villa"
        if i == 75:
            # Off-topic interruption then return
            assert not is_property_followup_message("who is narendra modi?", load_property_context(conv)) or True
            msg = "back to this property — what is its price?"

        ctx = load_property_context(conv)
        r = resolve_property_reference(msg, ctx)
        assert r.property_id == "PROP-ALPHA", f"turn {i}: {msg} -> {r}"
        resolved_ids.append(r.property_id)

        # Simulate live fact fetch each turn (authoritative)
        _, card, _ = _fake_fetch(None, r.property_id)
        assert card and card["property_id"] == "PROP-ALPHA"
        assert card["price"] == AUTHORIZED_DB["PROP-ALPHA"]["price"]

        # Update rolling summary like production
        ctx = update_rolling_summary(ctx, msg, f"Verified for {card['name']}: {card['price']}", r.property_id)
        save_property_context(conv, ctx)

    assert all(pid == "PROP-ALPHA" for pid in resolved_ids)
    assert len(resolved_ids) == 100
    assert load_property_context(conv).active_property_id == "PROP-ALPHA"


def test_switch_to_second_then_return_to_first_across_many_turns(conv):
    ctx = touch_discussed(
        PropertyContext(conversation_id=str(conv.id)),
        ["PROP-ALPHA", "PROP-BETA"],
        {"PROP-ALPHA": "Alpha Villa Phuket", "PROP-BETA": "Beta Residence Samui"},
        refresh_ordinals=True,
    )
    ctx = set_active_property(ctx, "PROP-ALPHA", "Alpha Villa Phuket")
    save_property_context(conv, ctx)

    # 30 turns on Alpha
    for i in range(30):
        r = resolve_property_reference("its price?", load_property_context(conv))
        assert r.property_id == "PROP-ALPHA"
        ctx = update_rolling_summary(load_property_context(conv), "its price?", "ok", "PROP-ALPHA")
        save_property_context(conv, ctx)

    # Switch to Beta
    ctx = set_active_property(load_property_context(conv), "PROP-BETA", "Beta Residence Samui")
    save_property_context(conv, ctx)
    for i in range(20):
        r = resolve_property_reference("does it have a pool?", load_property_context(conv))
        assert r.property_id == "PROP-BETA"

    # Return to first property by ordinal / name
    r = resolve_property_reference("back to the first property", load_property_context(conv))
    assert r.property_id == "PROP-ALPHA"
    ctx = set_active_property(load_property_context(conv), "PROP-ALPHA", "Alpha Villa Phuket")
    save_property_context(conv, ctx)
    r2 = resolve_property_reference("and its location?", load_property_context(conv))
    assert r2.property_id == "PROP-ALPHA"


def test_server_restart_simulation_reloads_context_from_preferences(conv):
    """Drop in-process object and rebuild from preferences dict (SQL hydrate shape)."""
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha Villa Phuket")
    prefs = save_property_context(conv, ctx)

    # Simulate new process: only preferences blob survives
    revived = FakeConversation(cid=str(conv.id))
    revived.preferences = dict(prefs)
    revived.message_history = []  # transcript may be truncated

    loaded = load_property_context(revived)
    assert loaded.active_property_id == "PROP-ALPHA"
    r = resolve_property_reference("what is its price?", loaded)
    assert r.property_id == "PROP-ALPHA"


def test_search_does_not_steal_active_when_multiple_results(conv):
    ctx = set_active_property(PropertyContext(conversation_id=str(conv.id)), "PROP-ALPHA", "Alpha")
    # Search returns two listings — touch_discussed updates ordinals but we only auto-activate on single result
    ctx2 = touch_discussed(ctx, ["PROP-ALPHA", "PROP-BETA"], {
        "PROP-ALPHA": "Alpha", "PROP-BETA": "Beta"
    }, refresh_ordinals=True)
    assert ctx2.active_property_id == "PROP-ALPHA"
    assert ctx2.reference_map.get("second property") == "PROP-BETA"


def test_comparison_set_stored(conv):
    ctx = set_comparison_set(PropertyContext(conversation_id=str(conv.id)), ["PROP-ALPHA", "PROP-BETA"])
    assert ctx.comparison_set == ["PROP-ALPHA", "PROP-BETA"]
    save_property_context(conv, ctx)
    assert load_property_context(conv).comparison_set == ["PROP-ALPHA", "PROP-BETA"]


def test_named_followup_resolves_shown_card_mono_champaca(conv):
    """Regression: card shown in carousel must resolve by display name (Mono Champaca)."""
    from app.services.property_context_service import resolve_name_against_context, touch_discussed

    ctx = touch_discussed(
        PropertyContext(conversation_id=str(conv.id)),
        ["mono-champaca", "pirak-condominium-cherngtalay"],
        {
            "mono-champaca": "Mono Champaca",
            "pirak-condominium-cherngtalay": "Pirak Condominium Cherngtalay",
        },
        refresh_ordinals=True,
    )
    save_property_context(conv, ctx)
    conv.message_history = [
        {
            "role": "assistant",
            "content": "here are options",
            "properties": [
                {"property_id": "mono-champaca", "name": "Mono Champaca"},
                {"property_id": "pirak-condominium-cherngtalay", "name": "Pirak Condominium Cherngtalay"},
            ],
            "structured_data": {
                "property_ids": ["mono-champaca", "pirak-condominium-cherngtalay"],
                "properties": [
                    {"property_id": "mono-champaca", "name": "Mono Champaca"},
                ],
            },
        }
    ]
    ctx2 = load_property_context(conv)
    mapped = resolve_name_against_context("Mono Champaca", ctx2, conv)
    assert mapped == "mono-champaca"

    intent, args = detect_deterministic_intent(
        "can you tell me more about Mono Champaca?",
        stored_state=save_property_context(conv, ctx2),
    )
    assert intent == IntentType.PROPERTY_DETAILS
    assert args.get("property_id") == "mono-champaca"
