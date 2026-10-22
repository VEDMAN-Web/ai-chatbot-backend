"""
TASK 4: Public path must not import or call Google Workspace APIs
(send_email, create_calendar_event, or any Drive function).
Admin / google_services remain untouched for staff use.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.google_services as google_services
from app.services import public_agent as public_agent_mod
from app.services.public_agent import PUBLIC_TOOLS, PublicAgentService, format_property_card


_FORBIDDEN_CALLS = (
    "send_email",
    "create_calendar_event",
    "upload_to_drive",
    "download_from_drive",
    "list_drive_files",
    "get_drive_file",
    "create_drive_file",
    "search_drive",
)


def _boom(name: str):
    def _fn(*a, **k):
        raise AssertionError(f"public path must not call google_manager.{name}")

    return _fn


@pytest.fixture
def block_google_workspace(monkeypatch):
    """Any Google Workspace side-effect from the public path must fail the test."""
    gm = google_services.google_manager
    for name in _FORBIDDEN_CALLS:
        if hasattr(gm, name):
            monkeypatch.setattr(gm, name, _boom(name))
        else:
            # Guard against future Drive helpers being added and reached
            monkeypatch.setattr(gm, name, _boom(name), raising=False)
    # Also block generate_* that hits live APIs if ever wired
    if hasattr(gm, "get_credentials"):
        monkeypatch.setattr(
            gm,
            "get_credentials",
            _boom("get_credentials"),
        )
    yield gm


def test_public_agent_module_does_not_import_google_services():
    assert not hasattr(public_agent_mod, "google_manager")
    src = Path(inspect.getfile(public_agent_mod)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "google_services" not in node.module, (
                f"public_agent must not import google_services (found: {node.module})"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "google_services" not in alias.name


def test_public_tools_have_no_email_calendar_drive_tools():
    names = [t["function"]["name"] for t in PUBLIC_TOOLS]
    for banned in (
        "send_email",
        "send_property_brochure",
        "create_calendar_event",
        "upload_to_drive",
        "list_drive_files",
    ):
        assert banned not in names


def test_format_property_card_calendar_url_without_google_manager(block_google_workspace):
    prop = SimpleNamespace(
        property_id="above-element-villas",
        property_name="Above Element Villas",
        property_type="Villa",
        price_display="39M THB",
        price_numeric_min=None,
        bedrooms_display="4",
        bedrooms_min=4,
        area_display="300",
        district="Phuket",
        province="Phuket",
        area=None,
        latitude=7.9,
        longitude=98.3,
        status="Available",
        has_pool=True,
        beach_access=False,
        images=[],
        detail_page_url="https://tajlandia.com/p/above-element-villas",
        address="",
        nearby_places=None,
        ownership_type=None,
        description="",
        amenities=[],
        highlights=["Pool", "Sea view"],
        slug="above-element-villas",
    )
    card = format_property_card(prop)
    assert card["calendar_url"].startswith("https://calendar.google.com/calendar/render?")
    assert "Above Element" in card["calendar_url"] or "Viewing" in card["calendar_url"]


def test_schedule_viewing_never_calls_google_workspace(monkeypatch, block_google_workspace):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"

    conversation = MagicMock()
    conversation.preferences = {}
    conversation.session_id = "sess_no_gws"
    conversation.id = "conv_no_gws"
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
        detail_page_url="https://tajlandia.com/p/x",
        address="",
        nearby_places=None,
    )

    monkeypatch.setattr(public_agent_mod, "get_property_by_id", lambda db, pid: prop)
    monkeypatch.setattr(public_agent_mod, "enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(public_agent_mod, "record_or_update_lead", lambda **k: MagicMock(id="l1"))
    monkeypatch.setattr(
        public_agent_mod,
        "format_property_card",
        lambda p: {"property_id": p.property_id, "name": p.property_name},
    )

    result, _cards, lead_flag = agent.execute_tool(
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
        },
    )
    assert lead_flag is True
    assert result.get("appointment_confirmed") is not True
    assert not result.get("event_id")


def test_stale_send_property_brochure_never_calls_send_email(monkeypatch, block_google_workspace):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    result, _cards, _lead = agent.execute_tool(
        db=MagicMock(),
        conversation=MagicMock(preferences={}, site_origin="tajlandia.com"),
        tool_name="send_property_brochure",
        args={"property_id": "x", "recipient_email": "x@example.com"},
    )
    msg = (result.get("message") or result.get("error") or "").lower()
    assert "email" in msg or "unknown tool" in msg or "cannot" in msg
