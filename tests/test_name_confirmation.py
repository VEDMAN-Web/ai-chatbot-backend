"""
Did-you-mean confirmation follow-up: pending_name_candidates resolve/clear.
No network / no Firestore.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.name_confirmation import (
    PREF_CANDIDATES,
    PREF_QUERY,
    clear_pending_name_candidates,
    is_affirmative_message,
    store_pending_name_candidates,
    try_resolve_pending_name,
)


CAND_ONE = [{"property_id": "anava-1", "name": "Anava"}]
CAND_MULTI = [
    {"property_id": "sat-1", "name": "Satoshi's Paradise"},
    {"property_id": "sat-2", "name": "Satoshi Residence"},
    {"property_id": "sat-3", "name": "Satoshi Bay"},
]


def test_store_and_clear_pending():
    prefs = store_pending_name_candidates({}, query="Satoshi", candidates=CAND_MULTI)
    assert len(prefs[PREF_CANDIDATES]) == 3
    assert prefs[PREF_QUERY] == "Satoshi"
    cleared = clear_pending_name_candidates(prefs)
    assert PREF_CANDIDATES not in cleared
    assert PREF_QUERY not in cleared


@pytest.mark.parametrize(
    "msg,lang",
    [
        ("yes", "en"),
        ("yeah", "en"),
        ("correct", "en"),
        ("tak", "pl"),
        ("zgadza sie", "pl"),
        ("ใช่", "th"),
        ("ครับ", "th"),
        ("haa", "en"),
    ],
)
def test_affirmative_single_candidate_resolves(msg, lang):
    prefs = store_pending_name_candidates({}, query="Anava Condo", candidates=CAND_ONE)
    out = try_resolve_pending_name(msg, prefs, language=lang)
    assert out.kind == "resolved"
    assert out.property_id == "anava-1"
    assert out.name == "Anava"


@pytest.mark.parametrize(
    "msg,expected_id",
    [
        ("the first one", "sat-1"),
        ("2", "sat-2"),
        ("pierwszy", "sat-1"),
        ("drugi", "sat-2"),
        ("Satoshi Bay", "sat-3"),
        ("Satoshi's Paradise", "sat-1"),
    ],
)
def test_multi_candidate_ordinal_or_name(msg, expected_id):
    prefs = store_pending_name_candidates({}, query="Satoshi", candidates=CAND_MULTI)
    out = try_resolve_pending_name(msg, prefs, language="en")
    assert out.kind == "resolved"
    assert out.property_id == expected_id


def test_ambiguous_yes_with_three_candidates_asks_which_en():
    prefs = store_pending_name_candidates({}, query="Satoshi", candidates=CAND_MULTI)
    out = try_resolve_pending_name("yes", prefs, language="en")
    assert out.kind == "ask_which"
    assert out.ask_text
    assert "Which one" in out.ask_text
    assert "Satoshi's Paradise" in out.ask_text
    # Pending must remain until caller clears after use
    assert prefs.get(PREF_CANDIDATES)


def test_ambiguous_yes_asks_which_pl():
    prefs = store_pending_name_candidates({}, query="Satoshi", candidates=CAND_MULTI)
    out = try_resolve_pending_name("tak", prefs, language="pl")
    assert out.kind == "ask_which"
    assert "Którą" in (out.ask_text or "")


def test_ambiguous_yes_asks_which_th():
    prefs = store_pending_name_candidates({}, query="Satoshi", candidates=CAND_MULTI)
    out = try_resolve_pending_name("ใช่", prefs, language="th")
    assert out.kind == "ask_which"
    assert "หมายถึง" in (out.ask_text or "")


def test_new_question_clears_continue():
    prefs = store_pending_name_candidates({}, query="Satoshi", candidates=CAND_MULTI)
    out = try_resolve_pending_name("show me villas in Phuket", prefs, language="en")
    assert out.kind == "clear_continue"


def test_different_property_name_clears_continue():
    prefs = store_pending_name_candidates({}, query="Satoshi", candidates=CAND_MULTI)
    out = try_resolve_pending_name("Anava", prefs, language="en")
    assert out.kind == "clear_continue"


def test_yes_with_no_pending_does_not_pick_property():
    out = try_resolve_pending_name("yes", {}, language="en")
    assert out.kind == "none"
    assert out.property_id is None

    out2 = try_resolve_pending_name("yes", {PREF_CANDIDATES: []}, language="en")
    assert out2.kind == "none"
    assert out2.property_id is None


def test_is_affirmative_multilang():
    assert is_affirmative_message("yes")
    assert is_affirmative_message("tak")
    assert is_affirmative_message("ใช่")
    assert is_affirmative_message("ครับ")
    assert is_affirmative_message("haa")
    assert not is_affirmative_message("show me Anava")


def test_public_agent_yes_no_pending_does_not_pick(monkeypatch):
    """Integration: yes with empty prefs must not route to property details."""
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    called = {"details": False}

    def _fail_details(*_a, **_k):
        called["details"] = True
        return {"type": "text", "text": "picked"}

    monkeypatch.setattr(agent, "_handle_property_details", _fail_details)
    result = agent._maybe_resolve_pending_name_candidates(
        db=MagicMock(),
        conversation=conversation,
        user_message="yes",
        start_time=0.0,
        language="en",
    )
    assert result is None
    assert called["details"] is False


def test_public_agent_yes_single_candidate_resolves(monkeypatch):
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = store_pending_name_candidates(
        {}, query="Anava", candidates=CAND_ONE
    )
    conversation.session_id = "s1"
    conversation.id = "c1"

    captured = {}

    def _details(db, conversation, args, user_message, start_time, language="en"):
        captured["args"] = args
        return {
            "type": "property_cards",
            "text": "Anava details",
            "properties": [{"property_id": "anava-1", "name": "Anava"}],
            "preferences": conversation.preferences,
        }

    monkeypatch.setattr(agent, "_handle_property_details", _details)
    result = agent._maybe_resolve_pending_name_candidates(
        db=MagicMock(),
        conversation=conversation,
        user_message="yes",
        start_time=0.0,
        language="en",
    )
    assert result is not None
    assert captured["args"]["property_id"] == "anava-1"
    assert PREF_CANDIDATES not in (conversation.preferences or {})


def test_public_agent_ambiguous_yes_asks_once(monkeypatch):
    from app.services.public_agent import PublicAgentService
    from app.services import public_agent as pa_mod

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = store_pending_name_candidates(
        {}, query="Satoshi", candidates=CAND_MULTI
    )
    conversation.session_id = "s1"
    conversation.id = "c1"

    monkeypatch.setattr(pa_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(pa_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])

    result = agent._maybe_resolve_pending_name_candidates(
        db=MagicMock(),
        conversation=conversation,
        user_message="yes",
        start_time=0.0,
        language="en",
    )
    assert result is not None
    assert result["type"] == "text"
    assert "Which one" in result["text"]
    # Still pending so user can answer with a number
    assert conversation.preferences.get(PREF_CANDIDATES)
