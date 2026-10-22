"""
PRIORITY 2: site_origin must stay real; replies use that tenant's email/brand.
"""

from __future__ import annotations

import pytest

from app.api.v1.chat import PublicChatRequest
from app.core.tenant import resolve_tenant
from app.services.i18n_service import build_no_match_reply, get_greeting_text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("tajlandia.com", "tajlandia.com"),
        ("https://tajlandia.com/", "tajlandia.com"),
        ("tajlandia.pl", "tajlandia.pl"),
        ("https://www.tajlandia.pl/chat", "tajlandia.pl"),
        ("thai.estate", "thai.estate"),
        ("https://thai.estate", "thai.estate"),
        ("varsoviaestate.com", "varsoviaestate.com"),
        ("localhost", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_validate_origin_keeps_real_site(raw, expected):
    req = PublicChatRequest(session_id="s1", message="hi", site_origin=raw)
    assert req.site_origin == expected
    # Must never collapse tajlandia → varsovia
    if "tajlandia" in raw.lower():
        assert "varsovia" not in req.site_origin


def test_tajlandia_origin_not_collapsed_to_varsovia():
    req = PublicChatRequest(session_id="s1", message="hi", site_origin="tajlandia.com")
    assert req.site_origin == "tajlandia.com"
    assert req.site_origin != "varsoviaestate.com"


@pytest.mark.parametrize(
    "origin,email_substr,brand_substr",
    [
        ("tajlandia.com", "tajlandia.com", "Tajlandia"),
        ("tajlandia.pl", "tajlandia.com", "Tajlandia"),
        ("thai.estate", "thai.estate", "Thai Estate"),
        ("varsoviaestate.com", "varsoviaestate.com", "Varsovia"),
    ],
)
def test_resolve_tenant_brand_and_email(origin, email_substr, brand_substr):
    t = resolve_tenant(origin)
    assert email_substr in t.advisor_email
    assert brand_substr.lower() in t.brand_name.lower() or brand_substr.lower() in t.bot_name.lower()
    if "tajlandia" in origin:
        assert "varsoviaestate.com" not in t.advisor_email
        assert "Varsovia" not in t.bot_name


def test_no_match_reply_uses_tenant_email():
    pl = build_no_match_reply(language="pl", site_origin="tajlandia.com", criteria_summary="Phuket")
    assert "advisor@tajlandia.com" in pl
    assert "advisor@varsoviaestate.com" not in pl

    th = build_no_match_reply(language="en", site_origin="thai.estate", criteria_summary="Phuket")
    assert "advisor@thai.estate" in th
    assert "advisor@varsoviaestate.com" not in th

    vs = build_no_match_reply(language="en", site_origin="varsoviaestate.com", criteria_summary="Phuket")
    assert "advisor@varsoviaestate.com" in vs


def test_greeting_bot_name_matches_tenant():
    g = get_greeting_text(language="en", user_message="hello", site_origin="tajlandia.com")
    assert "Varsovia Estate Advisor" not in g
    assert "advisor@varsoviaestate.com" not in g
    t = resolve_tenant("tajlandia.com")
    assert t.bot_name != "Varsovia Estate Advisor"
    assert "tajlandia.com" in t.advisor_email
