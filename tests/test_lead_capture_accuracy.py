"""
TASK 2: Lead capture — validate contacts, upsert per session, interest level,
non-blocking save that never breaks the chat reply.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.services import lead_service
from app.services.lead_service import (
    clear_session_leads,
    enqueue_lead_save,
    infer_interest_level,
    record_or_update_lead,
    validate_email,
    validate_phone,
)


@pytest.fixture(autouse=True)
def _reset_leads(monkeypatch):
    clear_session_leads()
    monkeypatch.setattr(lead_service.settings, "ENABLE_LEAD_SAVING", True)
    yield
    clear_session_leads()


def _conv(session_id: str = "sess_lead_1"):
    c = MagicMock()
    c.id = f"fs_{session_id}"
    c.session_id = session_id
    c.message_history = []
    c.preferences = {}
    c.site_origin = "tajlandia.com"
    c.lead_score = 0.0
    c.agent_type = "public"
    return c


def test_validate_email_and_phone():
    assert validate_email("a@b.com") == "a@b.com"
    assert validate_email("not-an-email") is None
    assert validate_email("") is None
    assert validate_email(None) is None
    assert validate_phone("+48 501 234 567") is not None
    assert validate_phone("123") is None
    assert validate_phone("abcdef") is None


def test_contact_creates_exactly_one_lead_second_message_updates():
    conv = _conv("sess_one")
    db = MagicMock()

    lead1 = record_or_update_lead(
        db=db,
        conversation=conv,
        contact_email="alex@example.com",
        contact_name="Alex",
        buying_signal="Direct customer lead capture: alex@example.com",
    )
    lead2 = record_or_update_lead(
        db=db,
        conversation=conv,
        contact_phone="+48111222333",
        buying_signal="Viewing request for prop-1",
    )
    assert lead1.id == lead2.id
    assert lead2.contact_email == "alex@example.com"
    assert lead2.contact_phone is not None
    assert lead2.contact_name == "Alex"
    assert len(lead_service.list_session_leads()) == 1
    assert lead2.interest_level == "hot"
    assert any("Viewing request" in str(s) for s in lead2.buying_signals)


def test_invalid_email_not_stored():
    conv = _conv("sess_bad")
    lead = record_or_update_lead(
        db=MagicMock(),
        conversation=conv,
        contact_email="not-an-email",
        contact_name="Bob",
        buying_signal="Contact details submitted",
    )
    assert lead.contact_email is None
    assert lead.contact_name == "Bob"


def test_interest_levels_from_clear_signals():
    assert infer_interest_level(
        buying_signal="Searched properties with filters: {}", has_contact=False
    ) == "browsing"
    assert infer_interest_level(
        buying_signal="Requested brochure for x", has_contact=False
    ) == "interested"
    assert infer_interest_level(
        buying_signal="Requested details for x", has_contact=False
    ) == "interested"
    assert infer_interest_level(
        buying_signal="Asked about ownership", has_contact=False
    ) == "interested"
    assert infer_interest_level(
        buying_signal="Viewing request for x", has_contact=True
    ) == "hot"
    assert infer_interest_level(
        buying_signal="Direct customer lead capture: a@b.com", has_contact=True
    ) == "hot"


def test_lead_write_failure_does_not_break_reply(monkeypatch):
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    agent.api_key = None

    conversation = _conv("sess_fail")
    conversation.preferences = {"language": "en"}
    conversation.message_history = []

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.lead_service.record_or_update_lead", boom)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)
    if hasattr(agent, "_maybe_resolve_pending_name_candidates"):
        monkeypatch.setattr(agent, "_maybe_resolve_pending_name_candidates", lambda *a, **k: None)

    # Prefer cannot-email path if present; else capture_lead-style text reply
    msg = "email me the brochure at alex@example.com"
    try:
        from app.services.public_agent import message_asks_email_or_calendar_action
        assert message_asks_email_or_calendar_action(msg)
    except ImportError:
        msg = "My email is alex@example.com please call me"

    result = agent.run_conversation(
        db=MagicMock(),
        conversation=conversation,
        user_message=msg,
        requested_language="en",
    )
    assert result.get("text")
    time.sleep(0.25)


def test_enqueue_lead_save_background_and_swallows_errors(monkeypatch):
    started = time.monotonic()
    calls = []

    def slow_fail(**kwargs):
        time.sleep(0.15)
        calls.append(kwargs)
        raise RuntimeError("persist failed")

    monkeypatch.setattr(lead_service, "record_or_update_lead", slow_fail)
    enqueue_lead_save(
        db=None,
        conversation=_conv("sess_bg"),
        contact_email="a@b.com",
        buying_signal="Direct customer lead capture: a@b.com",
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.1, "enqueue must not block on slow lead save"
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.05)
    assert calls, "background save should still attempt the write"


def test_pure_search_creates_at_most_browsing_lead():
    """Existing behaviour: search may create a low-interest (browsing) lead, never hot."""
    conv = _conv("sess_search")
    lead = record_or_update_lead(
        db=MagicMock(),
        conversation=conv,
        preferred_location="Phuket",
        preferred_type="Villa",
        buying_signal="Searched properties with filters: {'location': 'Phuket'}",
    )
    assert lead.interest_level == "browsing"
    assert not lead.contact_email
    assert not lead.contact_phone
    assert any("Searched properties" in str(s) for s in lead.buying_signals)
