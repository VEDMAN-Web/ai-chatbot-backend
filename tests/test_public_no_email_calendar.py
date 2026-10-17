"""
TASK 1: Public agent must not email or create calendar events.
schedule_viewing records a viewing request as a lead only.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.public_agent import PUBLIC_TOOLS, PUBLIC_SYSTEM_PROMPT, PublicAgentService


def _tool_names():
    return [t["function"]["name"] for t in PUBLIC_TOOLS]


def test_public_tools_has_no_send_property_brochure():
    names = _tool_names()
    assert "send_property_brochure" not in names
    assert "schedule_viewing" in names
    assert "capture_lead" in names
    assert "generate_property_brochure" in names


def test_system_prompt_item_7_no_email_calendar_claims():
    text = PUBLIC_SYSTEM_PROMPT.lower()
    assert "capture_lead" in text
    assert "schedule_viewing" in text
    # Must not instruct the model to email or book calendar
    assert "gmail" not in text
    assert "google calendar" not in text
    assert "send_property_brochure" not in text
    assert "cannot send email" in text or "cannot send emails" in text


def test_schedule_viewing_records_lead_never_calls_google(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"

    conversation = MagicMock()
    conversation.preferences = {}
    conversation.session_id = "sess_view_1"
    conversation.id = "conv_view_1"
    conversation.site_origin = "tajlandia.com"

    prop = SimpleNamespace(
        property_id="above-element-villas",
        property_name="Above Element Villas",
        district="Phuket",
        province="Phuket",
        latitude=7.9,
        longitude=98.3,
        property_type="Villa",
        price_display="39M THB",
        price_numeric_min=None,
        bedrooms_display="4",
        bedrooms_min=4,
        area_display="300",
        images=[],
        detail_page_url="https://tajlandia.com/p/above-element-villas",
        address="",
        nearby_places=None,
    )

    leads = []

    def fake_lead(**kwargs):
        leads.append(kwargs)
        return MagicMock(id="lead_1")

    def boom_email(*a, **k):
        raise AssertionError("google_manager.send_email must not be called from public path")

    def boom_cal(*a, **k):
        raise AssertionError("google_manager.create_calendar_event must not be called from public path")

    monkeypatch.setattr("app.services.public_agent.get_property_by_id", lambda db, pid: prop)
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", fake_lead)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", fake_lead)
    monkeypatch.setattr("app.services.google_services.google_manager.send_email", boom_email)
    monkeypatch.setattr(
        "app.services.google_services.google_manager.create_calendar_event", boom_cal
    )
    monkeypatch.setattr(
        "app.services.public_agent.format_property_card",
        lambda p: {"property_id": p.property_id, "name": p.property_name},
    )

    result, cards, lead_flag = agent.execute_tool(
        db=MagicMock(),
        conversation=conversation,
        tool_name="schedule_viewing",
        args={
            "property_id": "above-element-villas",
            "customer_name": "Alex",
            "customer_email": "alex@example.com",
            "customer_phone": "+48111111111",
            "preferred_date": "2026-11-01",
            "preferred_time": "15:00",
            "notes": "Prefer afternoon",
        },
    )

    assert lead_flag is True
    assert len(leads) == 1
    lead = leads[0]
    assert lead.get("contact_name") == "Alex"
    assert lead.get("contact_email") == "alex@example.com"
    assert lead.get("contact_phone") == "+48111111111"
    notes = (lead.get("notes") or "") + " " + (lead.get("buying_signal") or "")
    assert "Above Element" in notes or "above-element" in notes.lower()
    assert "2026-11-01" in notes
    assert "15:00" in notes

    msg = (result.get("message") or "").lower()
    for banned in ("confirmed", "booked", "emailed", "email sent", "calendar"):
        assert banned not in msg, f"banned word {banned!r} in: {msg}"
    assert "recorded" in msg
    assert "advisory" in msg or "team" in msg
    assert result.get("appointment_confirmed") is not True
    assert not result.get("google_calendar_url")
    assert not result.get("event_id")


def test_send_property_brochure_tool_not_dispatched(monkeypatch):
    """Even if a stale model emits the old tool name, do not email."""
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"

    def boom_email(*a, **k):
        raise AssertionError("send_email must not be called")

    monkeypatch.setattr("app.services.google_services.google_manager.send_email", boom_email)

    result, cards, lead_flag = agent.execute_tool(
        db=MagicMock(),
        conversation=MagicMock(preferences={}, site_origin="tajlandia.com"),
        tool_name="send_property_brochure",
        args={
            "property_id": "x",
            "recipient_email": "x@example.com",
            "customer_name": "X",
        },
    )
    msg = (result.get("message") or result.get("error") or "").lower()
    assert "email" in msg or "unknown tool" in msg or "cannot" in msg
    for banned in ("emailed", "sent via gmail", "have been emailed"):
        assert banned not in msg
