"""TASK 2: Factual turns force tool_choice=required (or skip model when deterministic)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.grounding import is_factual_listing_query
from app.services.public_agent import PublicAgentService


def test_factual_queries_detected():
    assert is_factual_listing_query("Does it have a pool?")
    assert is_factual_listing_query("Show me villas in Phuket")
    assert is_factual_listing_query("Is it freehold?")
    assert not is_factual_listing_query("hello")
    assert not is_factual_listing_query("thanks")


def test_call_openai_forces_tool_choice_for_factual(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test-model"
    agent.client = MagicMock()
    agent.temperature = 0.7

    captured = {}

    class _Msg:
        content = "ok"
        tool_calls = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)

    def fake_create(client, **kwargs):
        captured.update(kwargs)
        return _Resp()

    conv = SimpleNamespace(
        id="c1",
        session_id="s1",
        site_origin="tajlandia.com",
        preferences={"language": "en"},
        message_history=[],
    )

    with patch("app.services.public_agent._safe_openai_create", side_effect=fake_create), \
         patch("app.services.public_agent.append_conversation_turn"), \
         patch("app.services.public_agent.token_tracker"), \
         patch("app.services.public_agent.extract_user_preferences", return_value={}), \
         patch("app.services.public_agent.ensure_structured_response", side_effect=lambda t, **k: t), \
         patch("app.services.public_agent.ThaiPropertyLegalGuardrail.sanitize_response",
               side_effect=lambda **k: k["response_text"]), \
         patch.object(agent, "_validate_or_fallback", side_effect=lambda text, cards, **k: (text, cards or [])), \
         patch.object(agent, "_generate_quick_replies", return_value=[]), \
         patch("app.services.public_agent.fetch_authorized_property", return_value=(None, None, "none")):
        agent._call_openai(
            db=MagicMock(),
            conversation=conv,
            user_message="Does this villa have a gym?",
            raw_user_message="Does this villa have a gym?",
            start_time=0.0,
            language="en",
        )

    assert captured.get("tool_choice") == "required"
    assert float(captured.get("temperature", 1)) <= 0.2
    # No prior assistant prose in messages
    blob = "\n".join(str(m.get("content") or "") for m in captured.get("messages") or [])
    assert "FACTS" in blob or "STRUCTURED STATE" in blob
