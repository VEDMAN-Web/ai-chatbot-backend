"""
Admin speed Tasks 1–6: fast model helpers, classifier/judge skips,
tool cache, fast paths, max tool rounds, timeouts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.auth import AuthenticatedStaff
from app.core import config as config_mod
from app.services.admin_perf import (
    TurnTimer,
    admin_fast_model,
    admin_main_model,
    classify_obvious_intent,
    should_run_guardrail_judge,
    tool_cache_get,
    tool_cache_invalidate_all,
    tool_cache_set,
)
from app.services.guardrails import (
    AIIntentClassificationResult,
    AIIntentClassifier,
    ResponseConfidenceGuardrail,
)
from app.services.admin_agent import (
    _anthropic_timeout_sec,
    _safe_anthropic_create,
    process_admin_chat,
    try_admin_fast_path,
)
from app.services.admin_reply_helpers import (
    is_anthropic_connection_error,
    is_anthropic_timeout_error,
)


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
        name="Alex",
    )


# ---- TASK 2: models + classifier/judge skips ----

def test_helper_calls_use_fast_model(monkeypatch):
    assert "haiku" in admin_fast_model().lower()
    captured = {}

    def fake_create(*args, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["max_tokens"] = kwargs.get("max_tokens")
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="text",
                text='{"primary_intent":"general_chat","document_requested":false,'
                     '"negative_feedback":false,"requires_verification":false,'
                     '"confidence":0.9,"explanation":"llm"}',
            )]
        )

    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "sk-test")
    with patch("app.services.guardrails._safe_client_create", side_effect=fake_create):
        # Ambiguous message — not caught by rules
        AIIntentClassifier.classify(
            client=object(),
            user_message="compare investment upside of these two listings quietly",
            conversation_history=[],
        )
    assert captured.get("model") == admin_fast_model()
    assert captured.get("max_tokens") == 200


def test_classifier_skipped_for_obvious_intents():
    for msg in ("hello", "yes", "cancel", "undo last change", "Show me leads from this week",
                "How many properties do we have in Phuket?", "what can you do"):
        o = classify_obvious_intent(msg)
        assert o is not None and o.get("skipped_llm") is True, msg
        res = AIIntentClassifier.classify(client=None, user_message=msg)
        # Must not need an API key / client
        assert res.confidence >= 0.9


def test_guardrail_judge_skipped_for_clean_readonly():
    assert should_run_guardrail_judge(
        requires_confirmation=False,
        action_status=None,
        download_link=None,
        tool_calls=[{"name": "get_buyer_leads"}],
        claim_hard_mismatch=False,
        document_requested=False,
    ) is False

    classification = AIIntentClassificationResult(
        primary_intent="buyer_leads",
        document_requested=False,
    )
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("judge must not run")

    with patch("app.services.guardrails.settings") as st, patch(
        "app.services.guardrails._safe_client_create", side_effect=boom
    ):
        st.ANTHROPIC_API_KEY = "sk-test"
        res = ResponseConfidenceGuardrail.evaluate(
            client=object(),
            user_message="Show me leads from this week",
            classification=classification,
            response_text="Here are 2 leads including Ada.",
            tool_calls=[{"name": "get_buyer_leads"}],
            tool_results=[{"result": {"total": 2, "leads": [{"contact_name": "Ada"}]}}],
            run_llm_judge=False,
        )
    assert not called
    assert res.needs_requery is False
    assert "skipped" in (res.critique or "").lower() or res.is_confident


def test_guardrail_judge_uses_fast_model_when_enabled():
    classification = AIIntentClassificationResult(
        primary_intent="property_knowledge",
        document_requested=False,
    )
    captured = {}

    def fake_create(*args, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["max_tokens"] = kwargs.get("max_tokens")
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="text",
                text='{"is_confident":true,"confidence_score":0.9,"has_false_agreement":false,'
                     '"has_unsolicited_action":false,"has_concrete_contradiction":false,'
                     '"critique":"ok","correction_needed":""}',
            )]
        )

    with patch("app.services.guardrails.settings") as st, patch(
        "app.services.guardrails._safe_client_create", side_effect=fake_create
    ):
        st.ANTHROPIC_API_KEY = "sk-test"
        st.ADMIN_HELPER_TIMEOUT = 30.0
        ResponseConfidenceGuardrail.evaluate(
            client=object(),
            user_message="change price",
            classification=classification,
            response_text="Proposed price update to 5,000,000 THB for Pirak Villa.",
            tool_calls=[{"name": "create_or_update_listing"}],
            tool_results=[{"result": {"pending_action_id": "pa-1", "diff_table": "..."}}],
            run_llm_judge=True,
        )
    assert captured.get("model") == admin_fast_model()
    assert captured.get("max_tokens") == 300


# ---- TASK 3: fast path, cache, rounds ----

def test_fast_path_greeting_no_llm(staff, monkeypatch):
    conv = SimpleNamespace(
        id=uuid4(),
        session_id="s1",
        preferences={"language": "en"},
        message_history=[],
        site_origin="admin",
    )
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: conv,
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    called = []
    monkeypatch.setattr(
        "app.services.admin_agent._call_anthropic_claude",
        lambda *a, **k: called.append("claude") or {"text": "no"},
    )
    monkeypatch.setattr(
        "app.services.admin_agent._run_deterministic_admin",
        lambda *a, **k: called.append("det") or {"text": "no"},
    )
    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", "sk-test"):
        res = process_admin_chat(db=MagicMock(), session_id="s1", message="hello", staff_user=staff)
    assert not called
    assert res.get("text")


def test_fast_path_inventory_no_llm(staff, monkeypatch):
    conv = SimpleNamespace(
        id=uuid4(),
        session_id="s1",
        preferences={"language": "en"},
        message_history=[],
        site_origin="admin",
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    stats = {
        "authoritative_total_properties": 42,
        "location_breakdown": {"Phuket": 12, "Koh Samui": 8},
        "property_type_breakdown": {"Villa": 20},
        "status_breakdown": {},
        "amenities_breakdown": {},
        "price_range_thb": {"min": "1M", "max": "50M"},
    }
    monkeypatch.setattr(
        "app.services.admin_agent._execute_admin_tool",
        lambda *a, **k: (stats, False, None),
    )
    timer = TurnTimer()
    res = try_admin_fast_path(
        db=MagicMock(),
        conv=conv,
        message="How many properties do we have in Phuket?",
        staff_user=staff,
        language="en",
        timer=timer,
    )
    assert res is not None
    assert "12" in (res.get("text") or "")
    assert "claude" not in timer.skipped
    assert any("fast_path:inventory" in s for s in timer.skipped)


def test_tool_cache_hit_avoids_second_db_call():
    tool_cache_invalidate_all()
    tool_cache_set("get_database_inventory_stats", {}, ({"authoritative_total_properties": 9}, False, None))
    hit = tool_cache_get("get_database_inventory_stats", {}, 60.0)
    assert hit is not None
    assert hit[0]["authoritative_total_properties"] == 9
    tool_cache_invalidate_all()
    assert tool_cache_get("get_database_inventory_stats", {}, 60.0) is None


def test_cache_cleared_after_confirmed_write(staff, monkeypatch):
    tool_cache_invalidate_all()
    tool_cache_set("get_buyer_leads", {"days": 7}, ({"leads": []}, False, None))
    assert tool_cache_get("get_buyer_leads", {"days": 7}, 60.0) is not None

    conv = SimpleNamespace(
        id=uuid4(),
        session_id="s1",
        preferences={"language": "en"},
        message_history=[],
        site_origin="admin",
    )
    pending = SimpleNamespace(
        id="pa-write-1",
        tool_name="create_or_update_listing",
        status="PENDING",
        action_payload={"property_id": "pirak", "fields": {"price_numeric_min": 5_000_000}},
    )
    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: conv,
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))
    monkeypatch.setattr(
        "app.services.action_service.get_pending_action",
        lambda *a, **k: pending,
    )
    monkeypatch.setattr(
        "app.services.action_service.confirm_and_execute_action",
        lambda **k: {
            "status": "EXECUTED",
            "execution_result": {
                "success": True,
                "property_id": "pirak",
                "title": "Pirak Villa",
                "mode": "update",
                "current_status": "Active",
            },
            "audit_log_id": "aud-1",
            "operation_id": "op-1",
        },
    )
    # Force open_pending path via pending_action_id
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [pending]
    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", ""):
        res = process_admin_chat(
            db=db,
            session_id="s1",
            message="yes",
            staff_user=staff,
            action="confirm",
            pending_action_id="pa-write-1",
        )
    assert res.get("action_status") == "executed"
    assert tool_cache_get("get_buyer_leads", {"days": 7}, 60.0) is None


def test_max_tool_rounds_config():
    assert int(getattr(config_mod.settings, "ADMIN_MAX_TOOL_ROUNDS", 3)) == 3


# ---- TASK 6: timeouts ----

def test_helper_and_main_timeouts():
    assert _anthropic_timeout_sec(helper=True) == float(
        getattr(config_mod.settings, "ADMIN_HELPER_TIMEOUT", 30.0)
    )
    assert _anthropic_timeout_sec(helper=False) == float(
        getattr(config_mod.settings, "ADMIN_MAIN_TIMEOUT", 60.0)
    )


def test_timeout_not_retried_connection_is():
    assert is_anthropic_timeout_error(TimeoutError("timed out")) is True
    assert is_anthropic_connection_error(TimeoutError("timed out")) is False

    class ConnErr(Exception):
        pass

    assert is_anthropic_connection_error(ConnErr("connection reset by peer")) is True

    calls = {"n": 0}

    class _Messages:
        def create(self, **kwargs):
            calls["n"] += 1
            raise TimeoutError("request timed out")

    class FakeClient:
        def __init__(self):
            self.messages = _Messages()

    with pytest.raises(TimeoutError):
        _safe_anthropic_create(FakeClient(), model="m", messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 1  # no retry on timeout

    calls["n"] = 0

    class _Messages2:
        def create(self, **kwargs):
            calls["n"] += 1
            raise ConnectionError("connection reset")

    class FakeClient2:
        def __init__(self):
            self.messages = _Messages2()

    with pytest.raises(ConnectionError):
        _safe_anthropic_create(FakeClient2(), model="m", messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 2  # initial + one retry


def test_admin_main_model_env_override(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ADMIN_MAIN_MODEL", "claude-sonnet-4-5")
    assert admin_main_model() == "claude-sonnet-4-5"
    monkeypatch.setattr(config_mod.settings, "ADMIN_MAIN_MODEL", "")
    assert admin_main_model() == getattr(config_mod.settings, "ANTHROPIC_MODEL")


def test_turn_timer_summary_line():
    t = TurnTimer()
    t.classifier_ms = 12
    t.record_llm(label="main_round_0", model="m", ms=100.0, input_tokens=10, output_tokens=20)
    t.record_tool("get_buyer_leads", 5.0, cached=True)
    line = t.summary_line()
    assert line.startswith("ADMIN_TURN_TIMING")
    assert "llm_calls=1" in line
    assert "get_buyer_leads:5" in line and "c]" in line
