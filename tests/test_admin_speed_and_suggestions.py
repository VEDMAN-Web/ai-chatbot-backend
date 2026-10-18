"""
TASK 7 — Admin speed: 90s timeout+retry, deterministic shortcuts, history trim,
follow-up suggestions, stable dossier template hash.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.auth import AuthenticatedStaff
from app.core import config as config_mod
from app.services.admin_reply_helpers import (
    ADMIN_HISTORY_MESSAGE_LIMIT,
    ADMIN_LLM_SLOW_MESSAGE,
    append_followup_suggestions,
    build_admin_structured_state,
    choose_dossier_template_variant,
    contextual_followup_suggestions,
    is_capabilities_question,
    is_undo_request,
    trim_admin_history_for_llm,
)
from app.services.admin_agent import (
    _anthropic_timeout_sec,
    _handle_admin_greeting,
    _safe_anthropic_create,
    process_admin_chat,
)


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
        name="Alex",
    )


def test_anthropic_timeout_is_90_seconds():
    assert float(getattr(config_mod.settings, "ANTHROPIC_TIMEOUT", 0)) == 90.0
    assert _anthropic_timeout_sec() == 90.0


def test_safe_anthropic_retries_once_then_raises():
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
    assert calls["n"] == 2  # initial + one retry


def test_dossier_template_stable_for_same_property():
    a = choose_dossier_template_variant(property_ids=["pirak-villa-phuket"])
    b = choose_dossier_template_variant(property_ids=["pirak-villa-phuket"])
    c = choose_dossier_template_variant(property_ids=["anava-samui"])
    assert a == b
    assert 1 <= a <= 7
    # Different id may differ (not required, but stability for same id is required)
    assert choose_dossier_template_variant(property_ids=["pirak-villa-phuket"], explicit=3) == 3
    assert choose_dossier_template_variant(property_ids=["pirak-villa-phuket"], explicit=99) == a


def test_offer_dossier_service_imports_stable_chooser():
    """offer_dossier_service must not use random.randint for templates."""
    import inspect
    from app.services import offer_dossier_service as ods

    src = inspect.getsource(ods.generate_offer_dossier_pdf)
    assert "random.randint" not in src
    assert "choose_dossier_template_variant" in src
    # Same property always same variant through the shared helper
    assert choose_dossier_template_variant(["abc-villa"]) == choose_dossier_template_variant(["abc-villa"])


def test_history_trim_keeps_last_12():
    hist = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    trimmed = trim_admin_history_for_llm(hist)
    assert len(trimmed) == ADMIN_HISTORY_MESSAGE_LIMIT
    assert trimmed[0]["content"] == "m18"
    assert trimmed[-1]["content"] == "m29"


def test_structured_state_block():
    block = build_admin_structured_state(
        pending_action_id="pa-1",
        last_resolved_property_id="pirak-villa",
        last_report_file="report.pdf",
    )
    assert "pending_action_id: pa-1" in block
    assert "pirak-villa" in block
    assert "report.pdf" in block


def test_capabilities_skips_llm(staff, monkeypatch):
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
        lambda *a, **k: called.append("claude") or {"text": "should not"},
    )
    monkeypatch.setattr(
        "app.services.admin_agent._run_deterministic_admin",
        lambda *a, **k: called.append("det") or {"text": "should not"},
    )
    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", "sk-test"):
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message="what can you do",
            staff_user=staff,
        )
    assert not called
    assert "Buyer leads" in (res.get("text") or "") or "leads" in (res.get("text") or "").lower()


def test_undo_not_treated_as_simple_skip():
    assert is_undo_request("please undo the last price change") is True
    assert is_capabilities_question("what can you do") is True
    assert is_undo_request("what can you do") is False


def test_followup_suggestions_for_leads():
    tips = contextual_followup_suggestions(
        [{"name": "list_buyer_leads"}],
        [{"result": {"leads": [{"email": "a@b.com"}]}}],
        language="en",
    )
    assert 1 <= len(tips) <= 3
    assert any("Email" in t or "Export" in t for t in tips)
    text = append_followup_suggestions("Here are the leads.", tips)
    assert "Suggestions" in text
    assert "do not run an action" in text.lower()


def test_timeout_returns_slow_message(staff, monkeypatch):
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
    monkeypatch.setattr("app.services.admin_agent.is_greeting_or_chit_chat", lambda m: False)
    monkeypatch.setattr(
        "app.services.admin_agent.AIIntentClassifier.classify",
        lambda **k: SimpleNamespace(
            primary_intent="general",
            document_requested=False,
            negative_feedback=False,
            requires_verification=False,
            confidence=0.9,
        ),
    )
    monkeypatch.setattr(
        "app.services.admin_agent.rewrite_admin_query",
        lambda **k: k.get("current_message") or "show inventory",
    )

    class Boom:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr("anthropic.Anthropic", Boom)
    monkeypatch.setattr(
        "app.services.admin_agent._call_anthropic_claude",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    with patch("app.services.admin_agent.settings.ANTHROPIC_API_KEY", "sk-test"):
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message="show me a detailed inventory analysis of everything",
            staff_user=staff,
        )
    assert res.get("text") == ADMIN_LLM_SLOW_MESSAGE
    assert res.get("action_status") == "timeout"
