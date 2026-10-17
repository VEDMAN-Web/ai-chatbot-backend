"""
TASK 3: Honest reply when visitor asks the bot to email or add calendar events.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.tenant import resolve_tenant
from app.services.i18n_service import get_cannot_email_or_calendar_text
from app.services.public_agent import PublicAgentService, message_asks_email_or_calendar_action


TENANTS = [
    ("tajlandia.com", "advisor@tajlandia.com"),
    ("thai.estate", "advisor@thai.estate"),
    ("varsoviaestate.com", "advisor@varsoviaestate.com"),
]


@pytest.mark.parametrize("origin,advisor", TENANTS)
def test_i18n_en_includes_tenant_advisor_email(origin, advisor):
    text = get_cannot_email_or_calendar_text("en", site_origin=origin, visitor_email="a@b.com")
    assert advisor in text
    assert "can't send emails" in text.lower() or "cannot send emails" in text.lower()
    assert "calendar" in text.lower()
    assert "noted" in text.lower() or "details" in text.lower()


@pytest.mark.parametrize("origin,advisor", TENANTS)
def test_i18n_pl_th_include_tenant_advisor(origin, advisor):
    pl = get_cannot_email_or_calendar_text("pl", site_origin=origin, visitor_email="a@b.com")
    th = get_cannot_email_or_calendar_text("th", site_origin=origin, visitor_email="a@b.com")
    assert advisor in pl and advisor in th
    assert "e-mail" in pl.lower() or "email" in pl.lower() or "meili" in pl.lower()
    assert "ปฏิทิน" in th or "อีเมล" in th


@pytest.mark.parametrize(
    "msg",
    [
        "email me the brochure at a@b.com",
        "please send me the PDF by email: a@b.com",
        "add this viewing to my calendar",
        "wyślij mi broszurę na a@b.com",
        "โปรดส่งโบรชัวร์ทางอีเมลไปที่ a@b.com",
    ],
)
def test_detector_flags_email_or_calendar_asks(msg):
    assert message_asks_email_or_calendar_action(msg) is True


def test_detector_ignores_normal_search():
    assert message_asks_email_or_calendar_action("show me villas in Phuket under 20 million") is False


@pytest.mark.parametrize("origin,advisor", TENANTS)
def test_email_me_brochure_honest_reply_and_lead(monkeypatch, origin, advisor):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    agent.api_key = None

    conversation = MagicMock()
    conversation.preferences = {"language": "en"}
    conversation.session_id = "sess_no_mail"
    conversation.id = "c_no_mail"
    conversation.message_history = []
    conversation.site_origin = origin

    leads = []

    def fake_lead(**kwargs):
        leads.append(kwargs)
        return MagicMock(id="lead_x")

    def boom_email(*a, **k):
        raise AssertionError("send_email must not be called")

    def boom_cal(*a, **k):
        raise AssertionError("create_calendar_event must not be called")

    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", fake_lead)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", fake_lead)
    monkeypatch.setattr("app.services.google_services.google_manager.send_email", boom_email)
    monkeypatch.setattr(
        "app.services.google_services.google_manager.create_calendar_event", boom_cal
    )
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)
    monkeypatch.setattr(agent, "_maybe_resolve_pending_name_candidates", lambda *a, **k: None)

    result = agent.run_conversation(
        db=MagicMock(),
        conversation=conversation,
        user_message="email me the brochure at a@b.com",
        requested_language="en",
    )

    text = (result.get("text") or "").lower()
    assert advisor.lower() in text
    assert "can't send emails" in text or "cannot send emails" in text
    assert "calendar" in text
    assert "emailed" not in text
    assert "sent the brochure" not in text
    assert len(leads) == 1
    assert leads[0].get("contact_email") == "a@b.com"
    assert result.get("lead_captured") is True
    assert resolve_tenant(origin).advisor_email == advisor


@pytest.mark.parametrize(
    "lang,msg",
    [
        ("pl", "wyślij mi broszurę e-mailem na a@b.com"),
        ("th", "ส่งโบรชัวร์ทางอีเมลไปที่ a@b.com"),
    ],
)
def test_pl_th_email_ask_uses_i18n_and_saves_lead(monkeypatch, lang, msg):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    agent.api_key = None

    conversation = MagicMock()
    conversation.preferences = {"language": lang}
    conversation.session_id = f"sess_{lang}"
    conversation.id = f"c_{lang}"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    leads = []

    def fake_lead(**kw):
        leads.append(kw)
        return MagicMock(id="1")

    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", fake_lead)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", fake_lead)
    monkeypatch.setattr(
        "app.services.google_services.google_manager.send_email",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no email")),
    )
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)
    monkeypatch.setattr(agent, "_maybe_resolve_pending_name_candidates", lambda *a, **k: None)

    result = agent.run_conversation(
        db=MagicMock(),
        conversation=conversation,
        user_message=msg,
        requested_language=lang,
    )
    assert "advisor@tajlandia.com" in (result.get("text") or "")
    assert leads and leads[0].get("contact_email") == "a@b.com"
