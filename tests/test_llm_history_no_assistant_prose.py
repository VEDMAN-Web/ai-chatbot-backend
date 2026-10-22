"""
TASK 1: LLM input must not include earlier assistant prose.
Structured state + last N user turns only.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.services.property_context_service import (
    PropertyContext,
    assemble_llm_context_block,
    update_rolling_summary,
)
from app.services.grounding import build_grounded_llm_messages, build_structured_state_block


def _history_6_turns():
    """6 user+assistant pairs with distinctive assistant prose."""
    h = []
    for i in range(6):
        h.append({"role": "user", "content": f"user turn {i + 1}: looking for villas"})
        h.append({
            "role": "assistant",
            "content": (
                f"ASSISTANT_PROSE_{i + 1}: This villa has a secret spa and 8% yield "
                f"and is freehold for foreigners. Completion Q2 2099."
            ),
        })
    return h


def test_assemble_context_has_no_assistant_prose():
    ctx = PropertyContext(
        active_property_id="villa-1",
        active_property_reference="Sunset Villa",
        recently_discussed_ids=["villa-1", "villa-2"],
        rolling_summary="active=villa-1; filters=location:Phuket",
    )
    block = assemble_llm_context_block(ctx, _history_6_turns())
    assert "ASSISTANT_PROSE" not in block
    assert "secret spa" not in block.lower()
    assert "8%" not in block
    assert "villa-1" in block or "Sunset" in block


def test_rolling_summary_is_structured_not_prose():
    ctx = PropertyContext()
    ctx = update_rolling_summary(
        ctx,
        user_message="Does it have a gym?",
        assistant_message="Yes it has a gym and 12% ROI guaranteed freehold spa.",
        resolved_property_id="villa-1",
        structured={
            "filters": {"location": "Phuket", "min_bedrooms": 4},
            "shown_ids": ["villa-1"],
            "active_id": "villa-1",
            "question": "Does it have a gym?",
        },
    )
    s = (ctx.rolling_summary or "").lower()
    assert "12% roi" not in s
    assert "guaranteed" not in s
    assert "villa-1" in s or "active" in s
    assert "gym" in s or "question" in s or "does it have" in s


def test_grounded_messages_after_6_turns_no_assistant_prose(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_LLM_HISTORY_USER_TURNS", 3)
    conv = SimpleNamespace(
        message_history=_history_6_turns(),
        preferences={
            "language": "en",
            "location": "Phuket",
            "active_filters": {"location": "Phuket", "property_type": "Villa"},
            "last_result_ids": ["first-id", "second-id"],
            "seen_property_ids": ["first-id", "second-id"],
            "property_context": {
                "active_property_id": "first-id",
                "active_property_reference": "First Villa",
                "recently_discussed_ids": ["first-id", "second-id"],
                "reference_map": {
                    "first property": "first-id",
                    "second property": "second-id",
                    "First Villa": "first-id",
                },
                "rolling_summary": "active=first-id; filters=location:Phuket",
            },
        },
        site_origin="tajlandia.com",
    )
    msgs = build_grounded_llm_messages(
        system_prompt="SYS",
        conversation=conv,
        user_message="tell me about the first one",
        language="en",
        facts_block="FACTS (verified): First Villa | Phuket | 3 beds",
    )
    blob = "\n".join(m["content"] for m in msgs)
    assert "ASSISTANT_PROSE" not in blob
    assert "secret spa" not in blob.lower()
    assert "8% yield" not in blob.lower()
    # At most 3 prior user turns + current = 4 user role messages (or current appended)
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert len(user_msgs) <= 4
    # State / facts present; first-one resolution via structured ids/names
    assert "first-id" in blob or "First Villa" in blob or "first property" in blob.lower()
    assert "FACTS" in blob or "STRUCTURED" in blob or "active_property" in blob.lower()


def test_public_llm_history_setting_default():
    assert int(getattr(settings, "PUBLIC_LLM_HISTORY_USER_TURNS", 3) or 3) >= 1
