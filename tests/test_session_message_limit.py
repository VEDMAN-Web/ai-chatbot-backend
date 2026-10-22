"""
Per-session public chat message limit tests.
Fully mocked: no Firestore, PostgreSQL, OpenAI, or network.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.api.v1.chat import PublicChatRequest, handle_public_chat
from app.services.conversation_service import (
    clear_in_session_conversation,
    get_session_message_count,
    increment_session_message_count,
)
from app.services.i18n_service import build_session_limit_reply


EM_DASH = "\u2014"


def _make_conversation(session_id="sess_limit_test", preferences=None, history=None):
    return SimpleNamespace(
        id=f"conv-{session_id}",
        session_id=session_id,
        site_origin="thai.estate",
        agent_type="public",
        message_history=list(history or []),
        preferences=dict(preferences or {}),
        lead_score=0.0,
    )


def _agent_reply(conversation, user_message="hi"):
    return {
        "type": "text",
        "text": f"Answered: {user_message}",
        "properties": [],
        "quick_replies": ["Show me properties in Phuket"],
        "lead_captured": False,
        "preferences": dict(conversation.preferences or {}),
        "session_id": conversation.session_id,
        "conversation_id": str(conversation.id),
        "language": "en",
        "brochure_property_id": None,
    }


@pytest.fixture(autouse=True)
def _clear_session_cache():
    clear_in_session_conversation("sess_limit_test")
    clear_in_session_conversation("sess_limit_new")
    clear_in_session_conversation("sess_limit_trim")
    clear_in_session_conversation("sess_limit_contact")
    clear_in_session_conversation("sess_limit_zero")
    clear_in_session_conversation("sess_limit_tenant")
    yield
    clear_in_session_conversation("sess_limit_test")
    clear_in_session_conversation("sess_limit_new")
    clear_in_session_conversation("sess_limit_trim")
    clear_in_session_conversation("sess_limit_contact")
    clear_in_session_conversation("sess_limit_zero")
    clear_in_session_conversation("sess_limit_tenant")


def test_messages_within_limit_increment_counter(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.chat.settings.MAX_USER_MESSAGES_PER_SESSION", 3, raising=False
    )
    conv = _make_conversation()
    agent = MagicMock(side_effect=lambda db, conversation, user_message, requested_language=None: _agent_reply(conversation, user_message))
    persist = MagicMock()

    monkeypatch.setattr("app.api.v1.chat.get_or_create_conversation", lambda **kw: conv)
    monkeypatch.setattr("app.api.v1.chat.public_agent.run_conversation", agent)
    monkeypatch.setattr("app.api.v1.chat.persist_conversation_preferences", persist)

    db = MagicMock()
    for i in range(1, 4):
        req = PublicChatRequest.model_construct(
            session_id="sess_limit_test",
            message=f"message {i}",
            site_origin="thai.estate",
            language="en",
        )
        resp = handle_public_chat(req, db=db)
        assert resp["type"] == "text"
        assert resp.get("session_limit_reached") is False
        assert get_session_message_count(conv) == i

    assert agent.call_count == 3
    assert get_session_message_count(conv) == 3
    assert persist.call_count == 3


def test_fourth_message_blocked_without_agent(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.chat.settings.MAX_USER_MESSAGES_PER_SESSION", 3, raising=False
    )
    conv = _make_conversation(preferences={"session_message_count": 3})
    agent = MagicMock()
    append = MagicMock()

    monkeypatch.setattr("app.api.v1.chat.get_or_create_conversation", lambda **kw: conv)
    monkeypatch.setattr("app.api.v1.chat.public_agent.run_conversation", agent)
    monkeypatch.setattr("app.api.v1.chat.append_conversation_turn", append)

    req = PublicChatRequest.model_construct(
        session_id="sess_limit_test",
        message="blocked message",
        site_origin="thai.estate",
        language="en",
    )
    resp = handle_public_chat(req, db=MagicMock())

    assert resp["type"] == "session_limit"
    assert resp["session_limit_reached"] is True
    assert resp["message_limit"] == 3
    assert resp["quick_replies"] == []
    assert resp["properties"] == []
    assert resp["brochure_property_id"] is None
    assert resp["lead_captured"] is False
    assert "message limit" in resp["text"].lower()
    agent.assert_not_called()
    append.assert_not_called()
    assert get_session_message_count(conv) == 3
    assert len(conv.message_history) == 0


def test_counter_survives_trimmed_history(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.chat.settings.MAX_USER_MESSAGES_PER_SESSION", 3, raising=False
    )
    # Simulate Firestore trim: short history, high counter
    conv = _make_conversation(
        session_id="sess_limit_trim",
        preferences={"session_message_count": 3},
        history=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "ok"}],
    )
    agent = MagicMock()
    monkeypatch.setattr("app.api.v1.chat.get_or_create_conversation", lambda **kw: conv)
    monkeypatch.setattr("app.api.v1.chat.public_agent.run_conversation", agent)

    req = PublicChatRequest.model_construct(
        session_id="sess_limit_trim",
        message="should be blocked",
        site_origin="thai.estate",
        language="en",
    )
    resp = handle_public_chat(req, db=MagicMock())
    assert resp["type"] == "session_limit"
    assert resp["session_limit_reached"] is True
    agent.assert_not_called()
    assert len(conv.message_history) == 2  # unchanged
    assert get_session_message_count(conv) == 3


def test_contact_form_only_does_not_increment_or_block(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.chat.settings.MAX_USER_MESSAGES_PER_SESSION", 3, raising=False
    )
    conv = _make_conversation(
        session_id="sess_limit_contact",
        preferences={"session_message_count": 3},
    )
    agent = MagicMock()
    append = MagicMock(side_effect=lambda **kw: conv)

    monkeypatch.setattr("app.api.v1.chat.get_or_create_conversation", lambda **kw: conv)
    monkeypatch.setattr("app.api.v1.chat.public_agent.run_conversation", agent)
    monkeypatch.setattr("app.api.v1.chat.append_conversation_turn", append)
    monkeypatch.setattr("app.api.v1.chat.settings.ENABLE_LEAD_SAVING", False, raising=False)

    req = PublicChatRequest.model_construct(
        session_id="sess_limit_contact",
        message=None,
        contact_name="Alex",
        contact_email="alex@example.com",
        site_origin="thai.estate",
        language="en",
    )
    resp = handle_public_chat(req, db=MagicMock())

    assert resp["type"] == "text"
    assert resp.get("session_limit_reached") is False
    assert resp["lead_captured"] is True
    agent.assert_not_called()
    assert get_session_message_count(conv) == 3  # unchanged


def test_limit_zero_disables_check(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.chat.settings.MAX_USER_MESSAGES_PER_SESSION", 0, raising=False
    )
    conv = _make_conversation(session_id="sess_limit_zero")
    agent = MagicMock(
        side_effect=lambda db, conversation, user_message, requested_language=None: _agent_reply(
            conversation, user_message
        )
    )
    monkeypatch.setattr("app.api.v1.chat.get_or_create_conversation", lambda **kw: conv)
    monkeypatch.setattr("app.api.v1.chat.public_agent.run_conversation", agent)
    monkeypatch.setattr("app.api.v1.chat.persist_conversation_preferences", MagicMock())

    db = MagicMock()
    for i in range(8):
        req = PublicChatRequest.model_construct(
            session_id="sess_limit_zero",
            message=f"msg {i}",
            site_origin="thai.estate",
            language="en",
        )
        resp = handle_public_chat(req, db=db)
        assert resp["type"] == "text"
        assert resp.get("session_limit_reached") is False

    assert agent.call_count == 8
    assert get_session_message_count(conv) == 8


def test_new_session_starts_at_zero(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.chat.settings.MAX_USER_MESSAGES_PER_SESSION", 3, raising=False
    )
    conv = _make_conversation(session_id="sess_limit_new", preferences={})
    agent = MagicMock(
        side_effect=lambda db, conversation, user_message, requested_language=None: _agent_reply(
            conversation, user_message
        )
    )
    monkeypatch.setattr("app.api.v1.chat.get_or_create_conversation", lambda **kw: conv)
    monkeypatch.setattr("app.api.v1.chat.public_agent.run_conversation", agent)
    monkeypatch.setattr("app.api.v1.chat.persist_conversation_preferences", MagicMock())

    assert get_session_message_count(conv) == 0
    req = PublicChatRequest.model_construct(
        session_id="sess_limit_new",
        message="first",
        site_origin="thai.estate",
        language="en",
    )
    resp = handle_public_chat(req, db=MagicMock())
    assert resp["type"] == "text"
    assert resp.get("session_limit_reached") is False
    assert get_session_message_count(conv) == 1
    agent.assert_called_once()


def test_session_limit_tenant_emails_never_varsovia():
    for origin, expected in (
        ("tajlandia.com", "advisor@tajlandia.com"),
        ("thai.estate", "advisor@thai.estate"),
    ):
        text = build_session_limit_reply("en", origin)
        assert expected in text
        assert "advisor@varsoviaestate.com" not in text


def test_session_limit_endpoint_tenant_emails(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.chat.settings.MAX_USER_MESSAGES_PER_SESSION", 1, raising=False
    )
    agent = MagicMock()
    monkeypatch.setattr("app.api.v1.chat.public_agent.run_conversation", agent)
    monkeypatch.setattr("app.api.v1.chat.append_conversation_turn", MagicMock())

    for origin, expected in (
        ("tajlandia.com", "advisor@tajlandia.com"),
        ("thai.estate", "advisor@thai.estate"),
    ):
        conv = _make_conversation(
            session_id="sess_limit_tenant",
            preferences={"session_message_count": 1},
        )
        conv.site_origin = origin
        monkeypatch.setattr("app.api.v1.chat.get_or_create_conversation", lambda **kw: conv)
        req = PublicChatRequest.model_construct(
            session_id="sess_limit_tenant",
            message="blocked",
            site_origin=origin,
            language="en",
        )
        resp = handle_public_chat(req, db=MagicMock())
        assert resp["type"] == "session_limit"
        assert expected in resp["text"]
        assert "advisor@varsoviaestate.com" not in resp["text"]
    agent.assert_not_called()


def test_session_limit_i18n_en_pl_th_no_emdash():
    en = build_session_limit_reply("en", "thai.estate")
    pl = build_session_limit_reply("pl", "thai.estate")
    th = build_session_limit_reply("th", "thai.estate")

    assert en and pl and th
    assert en != pl and en != th and pl != th
    for text in (en, pl, th):
        assert EM_DASH not in text
        assert "advisor@thai.estate" in text
        assert "advisor@varsoviaestate.com" not in text

    assert "message limit" in en.lower() or "new chat" in en.lower()
    assert "limit" in pl.lower() or "czat" in pl.lower()
    assert "แชท" in th or "ข้อความ" in th


def test_counter_helpers_default_and_increment():
    conv = _make_conversation(preferences={})
    assert get_session_message_count(conv) == 0
    assert increment_session_message_count(conv) == 1
    assert increment_session_message_count(conv) == 2
    assert conv.preferences["session_message_count"] == 2
