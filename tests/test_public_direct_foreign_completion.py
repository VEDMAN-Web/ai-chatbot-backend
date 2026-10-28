"""
Public chatbot: direct follow-up answers, how-are-you, foreign location,
unknown listing, text/cards sync, stale completion, amenity chip helper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.public_agent as public_agent_mod
from app.core.tenant import resolve_tenant
from app.services.amenity_service import collect_property_amenities, enrich_card_amenity_flags
from app.services.i18n_service import (
    get_greeting_text,
    get_thailand_only_foreign_reply,
)
from app.services.listing_presentation import clean_highlights_for_display
from app.services.property_context_service import PropertyContext, is_property_followup_message
from app.services.public_agent import (
    PublicAgentService,
    format_stale_completion_note,
)


TENANTS = [
    ("tajlandia.com", "advisor@tajlandia.com"),
    ("thai.estate", "advisor@thai.estate"),
    ("varsoviaestate.com", "advisor@varsoviaestate.com"),
]


def _agent():
    a = PublicAgentService.__new__(PublicAgentService)
    a.model = "test"
    a.client = None
    a.api_key = None
    return a


def _quiet(monkeypatch, agent):
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(public_agent_mod, "enqueue_lead_save", lambda **_k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)
    monkeypatch.setattr(public_agent_mod.settings, "ENABLE_FIRESTORE_SYNC", False)


# ---------------------------------------------------------------------------
# 2. Direct answers
# ---------------------------------------------------------------------------


def test_is_property_followup_detects_bedroom_and_location():
    ctx = PropertyContext(active_property_id="active-villa")
    assert is_property_followup_message("how many bedrooms in this?", ctx) is True
    assert is_property_followup_message("where is it located?", ctx) is True


def test_direct_bedroom_answer_short_no_full_spec(monkeypatch):
    agent = _agent()
    _quiet(monkeypatch, agent)
    card = {
        "property_id": "active-villa",
        "name": "Active Villa",
        "location": "Bang Tao, Phuket",
        "price": "12M THB",
        "bedrooms": "3",
        "area": "200 sqm",
        "status": "Available",
        "amenities": ["pool"],
        "highlights": ["Private pool", "Garden", "Security"],
        "has_pool": True,
        "description": "Available in 2, 3 and 4 bedroom configurations.",
        "latitude": 7.99,
        "longitude": 98.29,
        "maps_url": "https://maps.google.com/?q=7.99,98.29",
    }
    conv = MagicMock()
    conv.preferences = {
        "language": "en",
        "property_context": {"active_property_id": "active-villa"},
        "last_result_ids": ["active-villa"],
    }
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "tajlandia.com"

    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: card,
    )

    res = agent._handle_property_details(
        db=MagicMock(),
        conversation=conv,
        args={"property_id": "active-villa", "focus_question": "how many bedrooms in this?"},
        user_message="how many bedrooms in this?",
        start_time=0.0,
        language="en",
    )
    text = res["text"]
    assert "None" not in text
    assert "3" in text or "bedroom" in text.lower()
    # Must not dump the full multi-bullet spec block
    assert text.count("•") <= 2
    assert "Starting Price" not in text and "Key Highlights" not in text

    loc = agent._handle_property_details(
        db=MagicMock(),
        conversation=conv,
        args={"property_id": "active-villa", "focus_question": "where is it located?"},
        user_message="where is it located?",
        start_time=0.0,
        language="en",
    )
    assert "Bang Tao" in loc["text"] or "Phuket" in loc["text"]
    assert "maps.google" in loc["text"].lower() or "http" in loc["text"].lower()
    assert loc["text"].count("•") <= 2


def test_missing_bedrooms_mentions_description_configs(monkeypatch):
    agent = _agent()
    _quiet(monkeypatch, agent)
    card = {
        "property_id": "cfg-villa",
        "name": "Config Villa",
        "location": "Phuket",
        "price": "10M",
        "bedrooms": None,
        "amenities": [],
        "highlights": [],
        "description": "Units offered in 2–4 bedroom configurations with private pools.",
        "status": "Available",
    }
    conv = MagicMock()
    conv.preferences = {"language": "en", "last_result_ids": ["cfg-villa"]}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "tajlandia.com"
    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: card,
    )
    res = agent._handle_property_details(
        db=MagicMock(),
        conversation=conv,
        args={"property_id": "cfg-villa", "focus_question": "how many bedrooms in this?"},
        user_message="how many bedrooms in this?",
        start_time=0.0,
        language="en",
    )
    low = res["text"].lower()
    assert "none" not in low
    assert "not listed" in low or "configuration" in low or "2" in res["text"]


# ---------------------------------------------------------------------------
# 3. How are you
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang,msg", [("en", "how are you?"), ("pl", "jak się masz?"), ("th", "สบายดีไหม")])
def test_how_are_you_short_not_welcome(lang, msg):
    text = get_greeting_text(lang, msg, site_origin="tajlandia.com")
    low = text.lower()
    assert "welcome to" not in low
    assert "witamy w" not in low
    assert "ยินดีต้อนรับสู่" not in text
    assert len(text) < 280


# ---------------------------------------------------------------------------
# 4. Foreign location
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("origin,advisor", TENANTS)
@pytest.mark.parametrize("lang", ["en", "pl", "th"])
@pytest.mark.parametrize("place", ["India", "Dubai", "Spain"])
def test_foreign_location_thailand_only(origin, advisor, lang, place):
    text = get_thailand_only_foreign_reply(lang, site_origin=origin, foreign_place=place)
    assert advisor in text
    assert resolve_tenant(origin).advisor_email == advisor
    low = text.lower()
    for banned in ("yield", "roi", "%", "rental return", "zwrot", "ผลตอบแทน"):
        assert banned not in low
    assert "thailand" in low or "tajland" in low or "ไทย" in text


def test_foreign_search_returns_no_cards(monkeypatch):
    agent = _agent()
    _quiet(monkeypatch, agent)
    conv = MagicMock()
    conv.preferences = {"language": "en"}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "thai.estate"

    res = agent._handle_property_search(
        db=MagicMock(),
        conversation=conv,
        user_message="Show me villas in Dubai under 2 million",
        start_time=0.0,
        language="en",
    )
    assert res.get("properties") in ([], None) or not res.get("properties")
    assert "advisor@thai.estate" in res["text"]
    assert "yield" not in res["text"].lower()


# ---------------------------------------------------------------------------
# 5. Unknown property
# ---------------------------------------------------------------------------


def test_unknown_name_honest_not_found(monkeypatch):
    agent = _agent()
    _quiet(monkeypatch, agent)
    conv = MagicMock()
    conv.preferences = {"language": "en"}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "varsoviaestate.com"
    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: None,
    )
    monkeypatch.setattr(public_agent_mod, "get_property_by_id", lambda db, pid: None)
    res = agent._handle_property_details(
        db=MagicMock(),
        conversation=conv,
        args={"property_id": "NoSuchEstate-XYZ", "property_name": "NoSuchEstate XYZ"},
        user_message="Tell me about NoSuchEstate XYZ",
        start_time=0.0,
        language="en",
    )
    assert not res.get("properties")
    assert "advisor@varsoviaestate.com" in res["text"]
    assert "not" in res["text"].lower()


# ---------------------------------------------------------------------------
# 6. Text list matches cards
# ---------------------------------------------------------------------------


def test_search_text_lists_same_ids_as_cards(monkeypatch):
    agent = _agent()
    _quiet(monkeypatch, agent)
    cards = [
        {
            "property_id": "alpha-villa",
            "name": "Alpha Villa",
            "location": "Phuket",
            "price": "10M THB",
            "bedrooms": "3",
            "property_type": "Villa",
            "has_pool": True,
            "status": "Available",
        },
        {
            "property_id": "beta-condo",
            "name": "Beta Condo",
            "location": "Phuket",
            "price": "8M THB",
            "bedrooms": "2",
            "property_type": "Condominium",
            "has_pool": False,
            "status": "Available",
        },
    ]
    monkeypatch.setattr(
        public_agent_mod,
        "search_properties_unified",
        lambda *a, **k: list(cards),
    )
    monkeypatch.setattr(public_agent_mod, "get_session_seen_property_ids", lambda c: [])
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "tajlandia.com"

    res = agent._handle_property_search(
        db=MagicMock(),
        conversation=conv,
        user_message="Show me 2-bed properties in Phuket",
        start_time=0.0,
        language="en",
    )
    props = res.get("properties") or []
    assert [p["property_id"] for p in props] == ["alpha-villa", "beta-condo"]
    text = res["text"]
    assert text.lower().index("alpha") < text.lower().index("beta")
    assert "alpha villa" in text.lower()
    assert "beta condo" in text.lower()


# ---------------------------------------------------------------------------
# 7. Stale completion
# ---------------------------------------------------------------------------


def test_stale_completion_date_note():
    note = format_stale_completion_note("Q4 2025", language="en", now=datetime(2026, 10, 17, tzinfo=timezone.utc))
    assert note
    assert "passed" in note.lower()
    assert "confirm" in note.lower() or "advisory" in note.lower()
    assert format_stale_completion_note("Q4 2028", language="en", now=datetime(2026, 10, 17, tzinfo=timezone.utc)) is None


# ---------------------------------------------------------------------------
# 8. Amenity helper / chip dedupe
# ---------------------------------------------------------------------------


def test_amenity_chips_deduped_via_helper():
    card = {
        "property_id": "x",
        "name": "X",
        "amenities": ["private-pool", "pool", "Private Pool", "parking", "Parking"],
        "highlights": ["Private pool", "private pool", "Parking", "Sea view", "sea-view"],
        "has_pool": True,
    }
    enrich_card_amenity_flags(card)
    chips = clean_highlights_for_display(card.get("highlights") or [], prop=card, language="en")
    low = [c.lower() for c in chips]
    assert low.count("private pool") <= 1
    assert sum(1 for c in low if "parking" in c) <= 1
    facts = collect_property_amenities(card)
    assert facts["pool_private"].present or facts["pool"].present
