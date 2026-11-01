"""
Regression tests for the E2E QA report (admin portal):

- Bug 1:  the bot disowned its own earlier, tool-grounded answers ("QA flag") because the LLM history
          carried only reply text, and the judge only saw this turn's tool results.
- Bug 2:  inventory breakdowns came from a 250-listing sample (130 Phuket vs 1,046 total), and search
          results reported the page length (10/100) instead of the number of matches.
- Bug 15: off-topic questions ("capital of India") were answered: the admin scope guard only ran in the
          deterministic fallback, never on the Claude route used in production.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.auth import AuthenticatedStaff
from app.services.admin_reply_helpers import (
    ADMIN_SCOPE_AND_GROUNDING_RULES,
    build_prior_grounding_note,
    collect_prior_tool_results,
    is_admin_off_topic,
)


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001", email="admin@tajlandia.com", role="super_admin", name="Alex"
    )


def _seed_projects(fs, n_phuket: int, n_samui: int, n_archived: int = 0):
    i = 0
    for city, n in (("phuket", n_phuket), ("koh-samui", n_samui)):
        for _ in range(n):
            i += 1
            fs.collection("projects").document(f"p{i:05d}").set({
                "isActive": True,
                "name": {"en": f"Listing {i}"},
                "cityId": city,
                "city": "Phuket" if city == "phuket" else "Koh Samui",
                "priceFrom": 1_000_000 + i * 1000,
                "currency": "THB",
                "type": "villa",
            })
    for _ in range(n_archived):
        i += 1
        fs.collection("projects").document(f"p{i:05d}").set({
            "isActive": False, "status": "Archived", "name": {"en": f"Old {i}"}, "cityId": "phuket",
        })


# --------------------------------------------------------------------------- Bug 2


def test_inventory_breakdown_covers_whole_catalog_not_a_sample(_default_fake_firestore, staff):
    from app.services.admin_agent import _execute_admin_tool

    _seed_projects(_default_fake_firestore, n_phuket=400, n_samui=200, n_archived=50)
    res, _, _ = _execute_admin_tool(MagicMock(), "get_database_inventory_stats", {}, staff, "c1")

    assert res["authoritative_total_properties"] == 650
    assert res["active_count"] == 600
    assert res["archived_count"] == 50
    # Old code: breakdown over 250 sampled listings → Phuket <= 250, sums != active
    assert res["location_breakdown"].get("Phuket") == 400
    assert res["location_breakdown"].get("Koh Samui") == 200
    assert sum(res["location_breakdown"].values()) == res["active_count"]
    assert sum(res["property_type_breakdown"].values()) == res["active_count"]


def test_inventory_reply_states_active_and_archived(_default_fake_firestore, staff):
    from app.services.admin_agent import _execute_admin_tool
    from app.services.admin_chat_quality import format_inventory_reply

    _seed_projects(_default_fake_firestore, n_phuket=260, n_samui=10, n_archived=5)
    res, _, _ = _execute_admin_tool(MagicMock(), "get_database_inventory_stats", {}, staff, "c1")
    text = format_inventory_reply(res, "en")
    assert "275" in text and "270 active" in text and "5 archived" in text


def test_search_reports_total_matches_beyond_page_limit(_default_fake_firestore, staff):
    from app.services.admin_agent import _execute_admin_tool, with_search_total

    _seed_projects(_default_fake_firestore, n_phuket=130, n_samui=20)
    page, _, _ = _execute_admin_tool(
        MagicMock(), "search_properties_full", {"location": "Phuket", "limit": 10}, staff, "c1"
    )
    # Internal callers still get a list
    assert isinstance(page, list) and len(page) == 10
    assert page.total_matches == 130

    llm_view = with_search_total(page)
    assert llm_view["total_matches"] == 130
    assert llm_view["returned"] == 10
    assert llm_view["results_capped"] is True
    assert len(llm_view["properties"]) == 10


def test_with_search_total_leaves_other_results_alone():
    from app.services.admin_agent import with_search_total

    assert with_search_total([{"a": 1}]) == [{"a": 1}]
    assert with_search_total({"x": 1}) == {"x": 1}


def test_verifier_accepts_total_matches_count():
    """A reply stating the true match count (not the page length) must not be flagged as a wrong count."""
    from app.services.admin_agent import CountedResultList, with_search_total
    from app.services.admin_claim_verifier import verify_admin_claims

    page = CountedResultList([{"property_id": f"P{i}", "name": f"Listing {i}"} for i in range(10)])
    page.total_matches = 130
    res = verify_admin_claims(
        "There are 130 properties in Phuket. Here are the first 5:", [{"result": with_search_total(page)}]
    )
    assert res.ok, res.reason


# --------------------------------------------------------------------------- Bug 15


@pytest.mark.parametrize("msg", [
    "What is the capital of India?",
    "what's the capital of India",
    "Who wrote Hamlet?",
    "tell me a joke",
    "how tall is mount everest",
])
def test_admin_off_topic_detected(msg):
    assert is_admin_off_topic(msg)


@pytest.mark.parametrize("msg", [
    "write me a listing description for Elite Chalong",
    "write me a follow-up email to the lead John",
    "who is the developer of Ozone Campus",
    "who is the agent assigned to lead 42",
    "sql query to export leads",
    "check google drive folder",
    "how many properties in Phuket",
    "show high intent leads",
    "what is the capital gains tax on a condo",
    "update the price of PROP-ABC-1 to 20M",
])
def test_admin_requests_are_not_off_topic(msg):
    assert not is_admin_off_topic(msg)


def _conv(history=None):
    return SimpleNamespace(
        id=uuid4(), session_id="s1", preferences={"language": "en"},
        message_history=list(history or []), site_origin="admin",
    )


def test_off_topic_is_steered_before_claude(staff, monkeypatch):
    """Production path (API key set): Claude must not be called and the answer must not be given."""
    from app.services.admin_agent import process_admin_chat

    conv = _conv()
    monkeypatch.setattr("app.services.admin_agent.get_or_create_conversation", lambda **kw: conv)
    saved = []
    monkeypatch.setattr(
        "app.services.admin_agent.append_conversation_turn", lambda *a, **k: saved.append(a)
    )
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    called = []
    monkeypatch.setattr(
        "app.services.admin_agent._call_anthropic_claude",
        lambda *a, **k: called.append("claude") or {"text": "The capital of India is New Delhi."},
    )
    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", "sk-test"):
        res = process_admin_chat(
            db=MagicMock(), session_id="s1", message="What is the capital of India?", staff_user=staff
        )
    assert not called
    text = (res.get("text") or "").lower()
    assert "new delhi" not in text
    assert "portfolio" in text and "general-knowledge" in text
    assert saved, "off-topic turn should still be stored in the conversation"


def test_off_topic_reply_polish():
    from app.services.admin_reply_helpers import admin_off_topic_reply

    assert "ogólne" in admin_off_topic_reply("pl")


def test_system_prompt_contains_scope_rule():
    assert "General-knowledge" in ADMIN_SCOPE_AND_GROUNDING_RULES
    assert "QA flag" in ADMIN_SCOPE_AND_GROUNDING_RULES


# --------------------------------------------------------------------------- Bug 1


_TURN1_HISTORY = [
    {"role": "user", "content": "how many properties do we have?"},
    {
        "role": "assistant",
        "content": "You have 1,046 properties: 1,000 active, 46 archived.",
        "tool_calls": [{"name": "get_database_inventory_stats", "args": {}}],
        "tool_results": [{"result": {
            "authoritative_total_properties": 1046, "active_count": 1000,
            "location_breakdown": {"Phuket": 600, "Koh Samui": 400},
        }}],
    },
]


def test_prior_grounding_note_lists_tool_backed_replies():
    note = build_prior_grounding_note(_TURN1_HISTORY)
    assert "EARLIER GROUNDED ANSWERS" in note
    assert "get_database_inventory_stats" in note
    assert "authoritative_total_properties=1046" in note
    assert "Phuket:600" in note


def test_prior_grounding_note_empty_without_tools():
    assert build_prior_grounding_note([
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello Alex"},
    ]) == ""


def test_collect_prior_tool_results():
    prior = collect_prior_tool_results(_TURN1_HISTORY)
    assert prior and prior[0]["result"]["authoritative_total_properties"] == 1046


class _CapturingClient:
    """Anthropic stand-in: records each create() call and answers with plain text."""

    def __init__(self, reply="Yes — we have 1,046 properties."):
        self.calls = []
        self.reply = reply
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def test_claude_sees_prior_grounding_and_scope_rules(staff, monkeypatch):
    """Turn 2 ("are you sure?") must carry the provenance of turn 1's figures into the model context."""
    from app.services.admin_agent import _call_anthropic_claude

    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    client = _CapturingClient()
    conv = _conv(_TURN1_HISTORY)
    res = _call_anthropic_claude(
        MagicMock(), conv, "are you sure about that number?", staff,
        raw_user_message="are you sure about that number?", client=client, language="en",
    )
    assert client.calls, "Claude should have been called"
    system = client.calls[0]["system"]
    system_text = "\n".join(b["text"] for b in system)
    assert "EARLIER GROUNDED ANSWERS" in system_text
    assert "authoritative_total_properties=1046" in system_text
    assert "NEVER describe them as fake" in system_text
    assert "1,046" in (res.get("text") or "")


def test_guardrail_judge_receives_earlier_turn_results():
    from app.services.guardrails import ResponseConfidenceGuardrail
    import json

    captured = {}

    class JudgeClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            captured.update(kwargs)
            body = json.dumps({"is_confident": True, "confidence_score": 0.95,
                               "has_concrete_contradiction": False, "critique": ""})
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])

    classification = SimpleNamespace(
        primary_intent="inventory", document_requested=False, requires_verification=False,
    )
    with patch("app.services.guardrails.settings.ANTHROPIC_API_KEY", "sk-test"):
        out = ResponseConfidenceGuardrail.evaluate(
            client=JudgeClient(),
            user_message="are you sure?",
            classification=classification,
            response_text="Yes, the database holds 1,046 properties as reported earlier.",
            tool_calls=[{"name": "search_properties_full"}],
            tool_results=[{"result": [{"property_id": "P1"}]}],
            prior_tool_results=collect_prior_tool_results(_TURN1_HISTORY),
        )
    user_prompt = captured["messages"][0]["content"]
    assert "EARLIER TURN TOOL RESULTS" in user_prompt and "1046" in user_prompt
    assert not out.needs_requery


# --------------------------------------------------------------------------- Bug 4 (admin side)


def test_high_intent_all_time_leads_sorted_by_score(_default_fake_firestore, staff):
    """get_buyer_leads(min_score>0, days=0) must list the best leads first, not the newest."""
    from app.repositories import leads_read_repo
    from app.services.admin_agent import _execute_admin_tool

    col = leads_read_repo.COLLECTION
    rows = [
        ("old-hot", 95.0, "2026-01-01T00:00:00+00:00"),
        ("new-warm", 75.0, "2026-10-30T00:00:00+00:00"),
        ("mid-hot", 85.0, "2026-06-01T00:00:00+00:00"),
        ("new-cold", 20.0, "2026-10-31T00:00:00+00:00"),
    ]
    for lid, score, ts in rows:
        _default_fake_firestore.collection(col).document(lid).set({
            "contact_name": lid, "contact_email": f"{lid}@buyer.io", "lead_score": score,
            "created_at": ts, "status": "new",
        })
    res, _, _ = _execute_admin_tool(
        MagicMock(), "get_buyer_leads", {"days": 0, "min_score": 70}, staff, "c1"
    )
    names = [ld.get("contact_name") for ld in res.get("leads", [])]
    assert names[:3] == ["old-hot", "mid-hot", "new-warm"], names
    assert "new-cold" not in names
