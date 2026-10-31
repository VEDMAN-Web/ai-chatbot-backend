"""
TASK 2: Reused session must brand by THIS request's site_origin, not the cached one.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from app.core.tenant import resolve_tenant
from app.services.conversation_service import (
    _IN_SESSION_CONVERSATIONS,
    get_or_create_conversation,
)
from app.services.i18n_service import (
    build_amenity_none_match_reply,
    build_name_not_found_reply,
    build_no_match_reply,
    build_seen_all_reply,
    build_session_limit_reply,
    get_brochure_unavailable_text,
    get_data_unavailable_fallback,
    get_greeting_text,
    get_off_topic_text,
    get_zero_match_text,
)


@pytest.fixture(autouse=True)
def _clear_session_cache():
    _IN_SESSION_CONVERSATIONS.clear()
    yield
    _IN_SESSION_CONVERSATIONS.clear()


def test_cached_conversation_rebinds_to_tajlandia(monkeypatch):
    monkeypatch.setattr(
        "app.services.conversation_service._firestore_sync_enabled",
        lambda agent_type: False,
    )
    # First visit: varsovia
    conv1 = get_or_create_conversation(
        db=None,
        session_id="sess_shared_1",
        site_origin="varsoviaestate.com",
        agent_type="public",
    )
    assert "varsovia" in (conv1.site_origin or "")

    # Same session on tajlandia.com — must rebind
    conv2 = get_or_create_conversation(
        db=None,
        session_id="sess_shared_1",
        site_origin="tajlandia.com",
        agent_type="public",
    )
    assert conv2 is conv1 or conv2.session_id == conv1.session_id
    assert conv2.site_origin == "tajlandia.com"

    tenant = resolve_tenant(conv2.site_origin)
    reply = build_no_match_reply("en", site_origin=conv2.site_origin, criteria_summary="Phuket")
    assert tenant.advisor_email == "advisor@tajlandia.com"
    assert "advisor@tajlandia.com" in reply
    assert "varsoviaestate.com" not in reply.lower()
    assert "advisor@varsoviaestate.com" not in reply


def test_cached_conversation_rebinds_to_thai_estate(monkeypatch):
    monkeypatch.setattr(
        "app.services.conversation_service._firestore_sync_enabled",
        lambda agent_type: False,
    )
    get_or_create_conversation(
        db=None, session_id="sess_shared_2", site_origin="varsoviaestate.com", agent_type="public"
    )
    conv = get_or_create_conversation(
        db=None, session_id="sess_shared_2", site_origin="thai.estate", agent_type="public"
    )
    assert conv.site_origin == "thai.estate"
    tenant = resolve_tenant(conv.site_origin)
    assert tenant.advisor_email == "advisor@thai.estate"
    reply = build_no_match_reply("en", site_origin=conv.site_origin, criteria_summary="Phuket")
    assert "advisor@thai.estate" in reply
    assert "Thai Estate" in reply or "thai.estate" in reply.lower()
    assert "varsoviaestate.com" not in reply.lower()


@pytest.mark.parametrize("origin", ["tajlandia.com", "thai.estate"])
@pytest.mark.parametrize("lang", ["en", "pl", "th"])
def test_i18n_builders_never_leak_varsovia(origin, lang):
    banned = "varsoviaestate"
    builders = [
        lambda: build_no_match_reply(lang, site_origin=origin, criteria_summary="Phuket"),
        lambda: build_name_not_found_reply(lang, site_origin=origin, asked_name="Zorblax"),
        lambda: build_seen_all_reply(lang, site_origin=origin, total_matching=3, criteria_summary="Phuket"),
        lambda: build_session_limit_reply(lang, site_origin=origin),
        lambda: get_off_topic_text(lang, site_origin=origin, user_message="capital of india"),
        lambda: get_greeting_text(lang, user_message="hello", site_origin=origin),
        lambda: get_data_unavailable_fallback(lang, site_origin=origin),
        lambda: get_brochure_unavailable_text(lang, site_origin=origin),
        lambda: get_zero_match_text(lang, site_origin=origin),
        lambda: build_amenity_none_match_reply(
            lang, site_origin=origin, location="Phuket", amenity_label="Gym", alternatives=[]
        ),
    ]
    for fn in builders:
        text = fn() or ""
        assert banned not in text.lower(), f"{fn} leaked varsovia for {origin}/{lang}: {text[:200]}"


def test_unknown_origin_logs_warning_and_uses_default(caplog):
    with caplog.at_level(logging.WARNING):
        t = resolve_tenant("some-random-vercel.app")
    assert any("unknown" in r.message.lower() or "site_origin" in r.message.lower() for r in caplog.records) or any(
        "some-random-vercel.app" in r.message for r in caplog.records
    )
    # Default brand from settings — must not crash; typically varsovia or env default
    assert t.advisor_email
    assert t.brand_name


def test_handle_public_chat_uses_request_origin(monkeypatch):
    """End-to-end-ish: after load, chat handler must force request site_origin onto conversation."""
    from app.api.v1 import chat as chat_mod
    from app.api.v1.chat import PublicChatRequest, handle_public_chat

    monkeypatch.setattr(
        "app.services.conversation_service._firestore_sync_enabled",
        lambda agent_type: False,
    )
    # Seed cache as varsovia
    get_or_create_conversation(
        db=None, session_id="sess_e2e", site_origin="varsoviaestate.com", agent_type="public"
    )

    captured = {}

    def fake_run(**kwargs):
        conv = kwargs["conversation"]
        captured["site_origin"] = conv.site_origin
        tenant = resolve_tenant(conv.site_origin)
        return {
            "type": "text",
            "text": f"Contact us at {tenant.advisor_email}",
            "properties": [],
            "quick_replies": [],
            "lead_captured": False,
            "preferences": {},
            "session_id": conv.session_id,
            "conversation_id": str(conv.id),
            "language": "en",
        }

    monkeypatch.setattr(chat_mod.public_agent, "run_conversation", fake_run)
    monkeypatch.setattr(chat_mod, "get_session_message_count", lambda c: 0)
    monkeypatch.setattr(chat_mod, "increment_session_message_count", lambda c: 1)
    monkeypatch.setattr(chat_mod, "persist_conversation_preferences", lambda c: None)

    req = PublicChatRequest(
        session_id="sess_e2e",
        message="Show me villas in Phuket",
        site_origin="tajlandia.com",
        language="en",
    )
    resp = handle_public_chat(req, db=MagicMock())
    assert captured["site_origin"] == "tajlandia.com"
    assert "advisor@tajlandia.com" in resp["text"]
    assert "varsoviaestate.com" not in resp["text"]


def test_tajlandia_sales_email_not_varsovia():
    from app.core.config import settings
    email = getattr(settings, "TAJLANDIA_SALES_EMAIL", "") or ""
    assert "varsoviaestate.com" not in email.lower()
    assert "tajlandia" in email.lower()
