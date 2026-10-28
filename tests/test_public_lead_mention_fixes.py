"""
Public chatbot fixes: lead vs mention, invalid phone, Did-you-mean short names,
mortgage vs ownership canned, lead confirmation wording, lead after N turns.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import intent_router as ir
from app.services import public_agent as pa
from app.services.answer_policy import answer_policy, classify_question_kind, did_you_mean_reply
from app.services.conversation_controls import is_foreign_ownership_question
from app.services.intent_router import IntentType
from app.services.property_name_index import PropertyNameIndex
from app.services.public_agent import PublicAgentService


ANAVA_ID = "MIijGwytZdYL1EiGumjV"


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    idx = PropertyNameIndex()
    idx.load_from_rows([
        {"doc_id": ANAVA_ID, "name": {"en": "Anava Condominium"}, "cityId": "samui"},
        {"doc_id": "sea-view", "name": {"en": "Sea View Villa"}, "cityId": "phuket"},
        {"doc_id": "sea-breeze", "name": {"en": "Sea Breeze Condo"}, "cityId": "samui"},
        {"doc_id": "sea-shell", "name": {"en": "Sea Shell Residence"}, "cityId": "phuket"},
        {"doc_id": "aura", "name": {"en": "Aura Condominium"}, "cityId": "phuket"},
    ])
    idx._loaded_at = 10**12
    monkeypatch.setattr("app.services.property_name_index.get_property_name_index", lambda: idx)
    monkeypatch.setattr("app.services.property_name_index.match_property_name", idx.match)


# ---------------------------------------------------------------------------
# 1. Lead wins over mention; name from I'm / this is / my name is / I am
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg,expect_name", [
    ("I'm John, phone +66812345678, interested in Anava", "John"),
    ("I am John Smith, phone +66812345678, interested in Anava", "John Smith"),
    ("this is Mary, email mary@test.com about Anava", "Mary"),
    ("my name is Ada Lovelace, email ada@test.com, Anava", "Ada Lovelace"),
])
def test_lead_wins_over_mention_and_extracts_name(msg, expect_name):
    intent, params = ir.detect_deterministic_intent(msg)
    assert intent == IntentType.LEAD_CAPTURE
    assert params.get("name") == expect_name
    assert params.get("email") or params.get("phone")


def test_mention_skipped_when_valid_phone_present():
    intent, params = ir.detect_deterministic_intent(
        "Tell me about Anava Condominium, call me on +66 81 234 5678"
    )
    assert intent == IntentType.LEAD_CAPTURE
    assert params.get("phone")


@pytest.mark.parametrize("prior_turns", [1, 3, 10])
def test_lead_capture_after_earlier_property_turns(prior_turns):
    state = {
        "last_result_ids": [ANAVA_ID],
        "property_context": {
            "active_property_id": ANAVA_ID,
            "active_property_reference": "Anava Condominium",
            "reference_map": {"first property": ANAVA_ID, "Anava Condominium": ANAVA_ID},
            "recently_discussed_ids": [ANAVA_ID],
        },
    }
    # Simulate prior property talk in stored state (turn count does not block lead)
    for i in range(prior_turns):
        state[f"_prior_{i}"] = f"turn-{i}"
    intent, params = ir.detect_deterministic_intent(
        "I'm Alex, phone +66819990000, interested in Anava",
        stored_state=state,
    )
    assert intent == IntentType.LEAD_CAPTURE
    assert params.get("name") == "Alex"
    assert params.get("phone")


def test_lead_reply_does_not_repeat_name(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    monkeypatch.setattr(pa, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(pa, "enqueue_lead_save", lambda **_k: None)
    monkeypatch.setattr(pa, "record_or_update_lead", lambda **_k: SimpleNamespace(id="L1"))
    monkeypatch.setattr(pa.token_tracker, "log_turn", lambda *a, **k: None)
    conv = SimpleNamespace(
        site_origin="varsoviaestate.com", session_id="s", id="c",
        preferences={}, message_history=[],
    )
    out = agent._handle_lead_capture(
        None, conv,
        {"name": "John", "phone": "+66812345678", "email": None, "is_viewing": False},
        "I'm John, phone +66812345678", 0.0, language="en",
    )
    text = out["text"]
    assert text.count("John") == 1
    assert "Thank you, John" in text
    assert "I have John" not in text


# ---------------------------------------------------------------------------
# 2. Invalid phone — exact ask once, never off-topic; then accept valid
# ---------------------------------------------------------------------------

def test_invalid_phone_intent_is_lead_not_off_topic():
    for msg in ("my phone is 12", "call me on 123"):
        intent, params = ir.detect_deterministic_intent(msg)
        assert intent == IntentType.LEAD_CAPTURE, msg
        assert params.get("phone") in {"12", "123"}


def test_invalid_phone_exact_ask_then_accept_valid(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    monkeypatch.setattr(pa, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(pa, "enqueue_lead_save", lambda **_k: None)
    monkeypatch.setattr(pa, "record_or_update_lead", lambda **_k: SimpleNamespace(id="L2"))
    monkeypatch.setattr(pa.token_tracker, "log_turn", lambda *a, **k: None)
    conv = SimpleNamespace(
        site_origin="varsoviaestate.com", session_id="bad-ph", id="c",
        preferences={}, message_history=[],
    )
    ask = agent._handle_lead_capture(
        None, conv, {"name": "Sam", "phone": "12"}, "my phone is 12", 0.0, language="en",
    )
    assert ask["lead_captured"] is False
    assert ask["text"] == (
        "Please share a valid phone number or email so an advisor can reach you."
    )
    assert "off-topic" not in ask["text"].lower()
    assert conv.preferences.get("lead_invalid_asked") is True

    ok = agent._handle_lead_capture(
        None, conv, {"phone": "+66819998888", "name": None},
        "my phone is +66819998888", 0.0, language="en",
    )
    assert ok["lead_captured"] is True
    assert "+66819998888" in ok["text"] or "advisor@" in ok["text"]


# ---------------------------------------------------------------------------
# 3. Short partial name → Did you mean A, B or C?
# ---------------------------------------------------------------------------

def test_tell_me_about_sea_did_you_mean():
    intent, params = ir.detect_deterministic_intent("tell me about Sea")
    assert intent == IntentType.PROPERTY_DETAILS
    assert params.get("name_uncertain") is True
    cands = params.get("name_candidates") or []
    assert 2 <= len(cands) <= 3
    reply = did_you_mean_reply(cands)
    assert reply.startswith("Did you mean")
    assert " or " in reply
    assert "?" in reply


def test_partial_sea_handler_reply(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    monkeypatch.setattr(pa, "append_conversation_turn", lambda **_k: None)
    conv = SimpleNamespace(
        site_origin="varsoviaestate.com", session_id="sea", id="c",
        preferences={}, message_history=[],
    )
    out = agent._handle_property_details(
        None, conv,
        {
            "property_name": "Sea",
            "name_uncertain": True,
            "name_candidates": ["Sea View Villa", "Sea Breeze Condo", "Sea Shell Residence"],
        },
        "tell me about Sea", 0.0, language="en",
    )
    assert out["fallback_type"] == "did_you_mean"
    assert out["text"] == "Did you mean Sea View Villa, Sea Breeze Condo or Sea Shell Residence?"
    assert "off-topic" not in out["text"].lower()


# ---------------------------------------------------------------------------
# 4. Mortgage / buying process → answer_policy; ownership canned only for ownership
# ---------------------------------------------------------------------------

def test_mortgage_foreigner_is_general_advisory_not_ownership_canned():
    q = "Can I get a mortgage as a foreigner?"
    assert is_foreign_ownership_question(q) is False
    assert classify_question_kind(q) == "general_advisory"
    out = answer_policy(classify_question_kind(q), {}, question=q)
    assert out["source"] == "answer_policy"
    assert "mortgage" in out["text"].lower() or "advisor" in out["text"].lower()
    assert out["source"] != "foreign_ownership_canned"


def test_buying_process_is_general_advisory():
    q = "how does buying property in Thailand work?"
    assert classify_question_kind(q) == "general_advisory"
    out = answer_policy("general_advisory", {}, question=q)
    assert out["source"] == "answer_policy"
    assert "advisor" in out["text"].lower()


def test_foreign_ownership_canned_still_for_ownership():
    q = "Can a foreigner buy property in Thailand?"
    assert is_foreign_ownership_question(q) is True
    assert classify_question_kind(q) == "foreign_ownership"
    out = answer_policy("foreign_ownership", {}, question=q)
    assert out["source"] == "foreign_ownership_canned"


# ---------------------------------------------------------------------------
# 5. Lead confirmation restates viewing / specs / callback + property
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,must", [
    (
        {"is_viewing": True, "target_ref": ANAVA_ID, "email": "a@b.com", "name": "QA"},
        ["private viewing of", "Anava Condominium"],
    ),
    (
        {"wants_specs": True, "target_ref": ANAVA_ID, "email": "a@b.com", "name": "QA"},
        ["the specifications of", "Anava Condominium"],
    ),
    (
        {"target_ref": ANAVA_ID, "email": "a@b.com", "name": "QA", "is_viewing": False},
        ["a call back about", "Anava Condominium"],
    ),
])
def test_lead_confirmation_mentions_asked_for(monkeypatch, kwargs, must):
    monkeypatch.setattr(pa, "record_or_update_lead", lambda **k: SimpleNamespace(id="lead-1"))
    monkeypatch.setattr(pa, "append_conversation_turn", lambda **k: None)
    monkeypatch.setattr(pa, "enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(pa.token_tracker, "log_turn", lambda *a, **k: None)
    conv = SimpleNamespace(
        site_origin="varsoviaestate.com", session_id="s", id="c",
        preferences={}, message_history=[],
    )
    out = pa.public_agent._handle_lead_capture(None, conv, dict(kwargs), "msg", 0.0, language="en")
    assert out["lead_captured"] is True
    for s in must:
        assert s in out["text"], out["text"]
