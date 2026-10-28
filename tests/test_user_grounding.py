"""MODULE U1: query logging, answer_policy, lead capture, open bugs."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import get_public_config_status, settings
from app.services.answer_policy import (
    answer_policy,
    classify_question_kind,
    lookup_taught_answer,
)
from app.services.firestore_service import (
    infer_chat_fallback_type,
    mask_pii_for_chat_log,
    log_query_to_firestore,
)
from app.services.intent_router import extract_search_filters_deterministic
from app.services.lead_service import record_or_update_lead, validate_phone, validate_email
from app.services.public_agent import (
    PublicAgentService,
    _absolute_media_url,
    _beds_area_phrase,
)
from app.services.reply_validator import verify_claims
from app.services.i18n_service import build_amenity_none_match_reply
import app.services.public_agent as public_agent_mod


# ---------------------------------------------------------------------------
# 1. Query logging
# ---------------------------------------------------------------------------

def test_mask_pii_keeps_first_and_last_two():
    text = mask_pii_for_chat_log("Call me at +66812345678 or email john.doe@example.com please")
    assert "+6" in text and "78" in text
    assert "john.doe@example.com" not in text
    assert "jo" in text and "om" in text
    assert "*" in text


def test_infer_fallback_types():
    assert infer_chat_fallback_type(reply_text="Did you mean Lake View Villa?") == "did_you_mean"
    assert infer_chat_fallback_type(reply_text="I can't answer general knowledge, politics...") == "off_topic"
    assert infer_chat_fallback_type(reply_text="The price is not in our verified data.") == "no_data"
    assert infer_chat_fallback_type(error="boom") == "error"
    assert infer_chat_fallback_type(reply_text="Here are villas in Phuket.") == "none"


def test_log_query_row_fields_and_flagged(monkeypatch):
    captured = {}

    class FakeDoc:
        def set(self, payload):
            captured.update(payload)

    class FakeCollection:
        def document(self, _id=None):
            return FakeDoc()

    class FakeClient:
        def collection(self, name):
            assert name == "chat_query_logs"
            return FakeCollection()

    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda: FakeClient())
    monkeypatch.setattr(settings, "ENABLE_CHAT_LOGGING", True)
    monkeypatch.setattr(settings, "CHAT_LOG_MASK_PII", True)
    log_id = log_query_to_firestore(
        session_id="s1",
        user_question="my email is secret@example.com",
        ai_response="Thanks secret@example.com",
        matched_properties=["p1"],
        conversation_id="c1",
        site_origin="varsoviaestate.com",
        language="en",
        turn_index=2,
        response_type="text",
        intent="lead_capture",
        cards_count=1,
        quick_replies=["Connect me with an advisor"],
        used_llm=False,
        fallback_type="no_data",
        latency_ms=42,
    )
    assert log_id
    for key in (
        "log_id", "session_id", "conversation_id", "site_origin", "language",
        "created_at", "turn_index", "user_message", "bot_reply", "response_type",
        "intent", "matched_property_ids", "cards_count", "quick_replies",
        "used_llm", "fallback_type", "latency_ms", "flagged", "corrected", "correction_id",
    ):
        assert key in captured, key
    assert captured["flagged"] is True
    assert captured["corrected"] is False
    assert captured["correction_id"] is None
    assert "secret@example.com" not in captured["user_message"]
    assert captured["turn_index"] == 2


def test_log_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(settings, "ENABLE_CHAT_LOGGING", True)
    assert log_query_to_firestore(session_id="s", user_question="hi", ai_response="hello") is None


def test_public_config_status_exposes_logging_flags():
    status = get_public_config_status()
    assert "chat_logging" in status
    assert "chat_log_mask_pii" in status
    assert "lead_saving" in status
    assert status["fallback_types"] == ["none", "no_data", "off_topic", "did_you_mean", "error"]


# ---------------------------------------------------------------------------
# 2. answer_policy
# ---------------------------------------------------------------------------

def test_answer_policy_missing_field():
    out = answer_policy(
        "property_fact",
        {"name": "Lake Villa"},
        question="what is the ownership?",
        missing_field="ownership",
        advisor_email="advisor@varsoviaestate.com",
    )
    assert "ownership" in out["text"].lower()
    assert "not in our verified data" in out["text"].lower()
    assert "advisor@varsoviaestate.com" in out["text"]
    assert out["fallback_type"] == "no_data"


def test_answer_policy_general_finance_no_property():
    out = answer_policy(
        "general_advisory",
        {"name": "ShouldNotAppear", "price": "10M"},
        question="what is the rental yield?",
        advisor_email="advisor@varsoviaestate.com",
    )
    assert "advisor" in out["text"].lower()
    assert "ShouldNotAppear" not in out["text"]
    assert "Connect me with an advisor" in out["quick_replies"]
    assert classify_question_kind("is it a good investment") == "general_advisory"


def test_answer_policy_uncertain_name():
    out = answer_policy(
        "uncertain_name",
        {},
        candidate_names=["Alpha Villa", "Beta Condo", "Gamma Estate"],
    )
    assert out["text"].startswith("Did you mean")
    assert "Alpha Villa" in out["text"] and "Beta Condo" in out["text"]
    assert out["fallback_type"] == "did_you_mean"


def test_taught_answer_hook_called_first(monkeypatch):
    monkeypatch.setattr(
        "app.services.answer_policy.lookup_taught_answer",
        lambda q, site=None, language="en": "Taught: foreigners may buy condo freehold under the quota.",
    )
    out = answer_policy(
        "general_advisory",
        {},
        question="what is the rental yield?",
        site="varsoviaestate.com",
    )
    assert out["source"] == "taught"
    assert out["text"].startswith("Taught:")


def test_lookup_taught_answer_default_none():
    assert lookup_taught_answer("anything") is None


# ---------------------------------------------------------------------------
# 3. Claim verification
# ---------------------------------------------------------------------------

def test_verify_claims_rejects_invented_number_and_name():
    facts = "FACTS: name: Real Villa | price: 12M THB | bedrooms: 3"
    ok, reasons = verify_claims(
        "Fake Towers has 9 bedrooms and costs 99M THB with 8% yield.",
        cards=[{"name": "Real Villa", "price": "12M THB", "bedrooms": "3", "property_id": "real"}],
        facts_text=facts,
    )
    assert ok is False
    joined = " ".join(reasons)
    assert "invented_property_name" in joined or "price_not_in_facts" in joined or "yield" in joined


# ---------------------------------------------------------------------------
# 4. Lead capture
# ---------------------------------------------------------------------------

def test_lead_single_message_saves_without_cards(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(settings, "ENABLE_LEAD_SAVING", True)
    saved = {}

    def _save(**kwargs):
        saved.update(kwargs)
        return SimpleNamespace(id="lead1")

    monkeypatch.setattr(public_agent_mod, "record_or_update_lead", _save)
    monkeypatch.setattr(public_agent_mod, "enqueue_lead_save", lambda **_k: None)
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "lead-s"
    conv.id = "lead-c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    result = agent._handle_lead_capture(
        db=MagicMock(),
        conversation=conv,
        args={
            "name": "John Smith",
            "phone": "+66812345678",
            "email": None,
            "target_ref": "Embassy Central",
        },
        user_message="my name is John Smith, phone +66 81 234 5678, i am interested in Embassy Central",
        start_time=0,
        language="en",
    )
    assert result["lead_captured"] is True
    assert result["properties"] == []
    assert "Embassy Central" in result["text"] or "advisor@" in result["text"]
    assert saved.get("contact_name") == "John Smith"


def test_lead_multi_message_combines(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(public_agent_mod, "enqueue_lead_save", lambda **_k: None)
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "lead-multi"
    conv.id = "c-multi"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    first = agent._handle_lead_capture(
        db=MagicMock(), conversation=conv,
        args={"name": "Ada Lovelace", "partial_contact": True},
        user_message="my name is Ada Lovelace", start_time=0, language="en",
    )
    assert first["lead_captured"] is False
    assert "phone" in first["text"].lower() or "email" in first["text"].lower()
    second = agent._handle_lead_capture(
        db=MagicMock(), conversation=conv,
        args={"phone": "+66819998888", "name": None},
        user_message="my phone is +66819998888", start_time=0, language="en",
    )
    assert second["lead_captured"] is True
    assert "Ada" in second["text"] or "advisor@" in second["text"]


def test_lead_invalid_phone_asks_once(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "bad-phone"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    result = agent._handle_lead_capture(
        db=MagicMock(), conversation=conv,
        args={"name": "Sam", "phone": "12"},
        user_message="my name is Sam, phone 12", start_time=0, language="en",
    )
    assert result["lead_captured"] is False
    assert "valid" in result["text"].lower()
    assert validate_phone("12") is None
    assert validate_email("not-an-email") is None


def test_lead_duplicate_updates_same_session():
    conv = SimpleNamespace(session_id="dup-s", id="dup-c", message_history=[], agent_type="public")
    first = record_or_update_lead(
        conversation=conv, contact_name="Pat", contact_email="pat@example.com", contact_phone="+66811112222",
    )
    second = record_or_update_lead(
        conversation=conv, contact_name="Pat Updated", contact_email="pat@example.com", contact_phone="+66811112222",
    )
    assert first.id == second.id
    assert second.contact_name == "Pat Updated"


# ---------------------------------------------------------------------------
# 5. Open bugs
# ---------------------------------------------------------------------------

def test_private_pool_none_match_wording():
    text = build_amenity_none_match_reply(
        language="en",
        location="Phuket",
        amenity_label="private pool",
        property_type="Villa",
        alternatives=[{
            "name": "Kamala View",
            "location": "Phuket",
            "price": "18.00M THB",
        }],
    )
    assert "couldn't confirm a private pool" in text.lower()
    assert "closest alternatives" in text.lower()
    assert "pool not listed in our verified data of the verified listings" not in text.lower()
    assert "Searching:" not in text


def test_up_to_million_is_not_bedrooms():
    filters = extract_search_filters_deterministic("up to 5 million")
    assert filters.get("max_price") == 5_000_000
    assert filters.get("max_bedrooms") is None
    beds = extract_search_filters_deterministic("up to 5 bedrooms")
    assert beds.get("max_bedrooms") == 5


def test_absolute_image_url_and_beds_line(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_API_BASE_URL", "https://api.example.com")
    assert _absolute_media_url("/media/a.jpg") == "https://api.example.com/media/a.jpg"
    assert _absolute_media_url("https://cdn.example.com/x.jpg") == "https://cdn.example.com/x.jpg"
    assert _beds_area_phrase("Beds: on request", "92 sqm") == "Bedrooms: on request | Area: 92 sqm"


def test_named_freehold_reads_ownership(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        public_agent_mod,
        "lookup_property_by_slug_or_name",
        lambda name: {
            "property_id": "embassy-central",
            "name": "Embassy Central",
            "ownership": "Foreign freehold condo",
            "ownership_type": "Foreign freehold condo",
        },
    )
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "fh"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    result = agent.run_conversation(
        db=MagicMock(), conversation=conv,
        user_message="is Embassy Central freehold?", requested_language="en",
    )
    assert "Foreign freehold" in result["text"] or "ownership" in result["text"].lower()
    assert "not confirmed" not in result["text"].lower()


def test_budget_range_empty_explains_blockers(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(public_agent_mod, "search_properties_unified", lambda *a, **k: [])
    conv = MagicMock()
    conv.preferences = {
        "active_filters": {"location": "Bangkok", "property_type": "Villa"},
        "location": "Bangkok",
        "property_type": "Villa",
    }
    conv.session_id = "range"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    result = agent.run_conversation(
        db=MagicMock(), conversation=conv,
        user_message="budget 20M to 30M", requested_language="en",
    )
    text = result["text"].lower()
    assert "relax" in text or "between 20m and 30m" in text or "20m-30m" in text
    assert "location" in text or "type" in text or "bangkok" in text or "villa" in text
