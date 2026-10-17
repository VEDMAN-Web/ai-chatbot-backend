"""Calls without tools must not use the slow default reasoning effort."""
from types import SimpleNamespace
import app.services.public_agent as pa


class _FakeCompletions:
    def __init__(self): self.kwargs = None
    def create(self, **kw):
        self.kwargs = kw
        return SimpleNamespace(choices=[])


def _client():
    comp = _FakeCompletions()
    return SimpleNamespace(chat=SimpleNamespace(completions=comp)), comp


def test_plain_call_gets_low_reasoning_effort():
    client, comp = _client()
    pa._safe_openai_create(client, model="gpt-5.5", messages=[], max_tokens=10)
    assert comp.kwargs["reasoning_effort"] == "low"
    assert "max_completion_tokens" in comp.kwargs


def test_tool_call_keeps_none_and_explicit_value_is_respected():
    client, comp = _client()
    pa._safe_openai_create(client, model="gpt-5.5", messages=[], tools=[{"type": "function"}])
    assert comp.kwargs["reasoning_effort"] == "none"
    pa._safe_openai_create(client, model="gpt-5.5", messages=[], reasoning_effort="high")
    assert comp.kwargs["reasoning_effort"] == "high"


def test_non_reasoning_models_untouched():
    client, comp = _client()
    pa._safe_openai_create(client, model="gpt-4o", messages=[])
    assert "reasoning_effort" not in comp.kwargs
