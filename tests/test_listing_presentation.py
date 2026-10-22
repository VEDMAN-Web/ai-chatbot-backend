"""
TASK 7–9: ownership from data only, marketing claim sanitization,
name/address/pool presentation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.approved_statements import DEVELOPER_MARKETING_CLAIM_LABEL
from app.services.listing_presentation import (
    apply_presentation_to_card,
    clean_highlights_for_display,
    extract_ownership_from_property,
    pool_display_label,
    resolve_display_name,
    sanitize_marketing_claims,
    shorten_location,
    slug_to_title,
)
from app.services.reply_validator import sanitize_listing_line_text, validate_reply
from app.services.public_agent import PublicAgentService


# --- TASK 7 ---

def test_ownership_from_data_field():
    card = {
        "ownership": "30-year registered leasehold with freehold building option",
        "description": "",
    }
    assert "30-year" in (extract_ownership_from_property(card) or "").lower()


def test_ownership_missing_omitted():
    card = {"name": "Bare", "description": "Nice villa near the beach.", "highlights": ["Parking"]}
    assert extract_ownership_from_property(card) is None


def test_details_reply_omits_hardcoded_ownership(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {"language": "en", "property_context": {}}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    card = {
        "property_id": "lease-1",
        "name": "Lease Villa",
        "location": "Bang Por, Koh Samui",
        "price": "8M THB",
        "bedrooms": "3",
        "area": "180",
        "status": "Available",
        "highlights": ["Parking"],
        "amenities": ["parking"],
        "tags": [],
        "description": "Quiet villa.",
        "has_pool": False,
        "beach_access": False,
    }

    monkeypatch.setattr("app.services.public_agent.lookup_property_by_slug_or_name", lambda tok: card)
    monkeypatch.setattr("app.services.public_agent.extract_explicit_property_token", lambda *a, **k: "lease-1")
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", lambda **_k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args={"property_id": "lease-1"},
        user_message="View details for lease-1",
        start_time=0.0,
        language="en",
    )
    text = result["text"] or ""
    assert "Foreign Freehold / Leasehold available" not in text
    assert "**Ownership:**" not in text


def test_details_reply_shows_ownership_from_data(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {"language": "en", "property_context": {}}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    card = {
        "property_id": "own-1",
        "name": "Own Villa",
        "location": "Phuket",
        "price": "12M",
        "bedrooms": "4",
        "area": "250",
        "status": "Available",
        "ownership": "30-year lease with freehold option",
        "highlights": [],
        "amenities": [],
        "tags": [],
        "description": "",
    }
    monkeypatch.setattr("app.services.public_agent.lookup_property_by_slug_or_name", lambda tok: card)
    monkeypatch.setattr("app.services.public_agent.extract_explicit_property_token", lambda *a, **k: "own-1")
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", lambda **_k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args={"property_id": "own-1"},
        user_message="details for own-1",
        start_time=0.0,
        language="en",
    )
    assert "Ownership:" in (result["text"] or "")
    assert "30-year lease" in (result["text"] or "")


# --- TASK 8 ---

def test_guaranteed_roi_stripped_from_description():
    raw = "Beautiful villa. Gwarantowane 8% ROI Netto for investors. Near the beach."
    cleaned = sanitize_marketing_claims(raw, mode="description")
    assert "8%" not in cleaned or DEVELOPER_MARKETING_CLAIM_LABEL in cleaned
    assert "Gwarantowane 8% ROI Netto" not in cleaned
    assert "Near the beach" in cleaned or "beach" in cleaned.lower()


def test_guaranteed_roi_not_bot_claim_in_validator():
    text = "This project offers guaranteed 8% ROI and zero construction risk."
    ok, reasons = validate_reply(text, cards=[{"name": "X", "price": "1M"}], applied_filters={})
    assert ok is False
    assert any("marketing" in r or "percentage" in r for r in reasons)

    labelled = sanitize_listing_line_text(text)
    assert DEVELOPER_MARKETING_CLAIM_LABEL in labelled or "guaranteed 8%" not in labelled.lower()


def test_factual_details_still_appear():
    raw = "3 bedrooms, 180 sqm, completed 2024, Bang Tao, Phuket. Starting from 9.5M THB."
    cleaned = sanitize_marketing_claims(raw, mode="description")
    assert "3 bedrooms" in cleaned
    assert "180 sqm" in cleaned
    assert "Bang Tao" in cleaned


# --- TASK 9 ---

def test_name_fallback_prefers_en_then_slug():
    data = {
        "name": {
            "en": "",
            "pl": "Gwarantowane 8% ROI Netto ekskluzywna inwestycja w Phuket z basenem i widokiem",
        },
        "slug": {"en": "sunset-bangtao-villas"},
    }
    name = resolve_display_name(data, doc_id="sunset-bangtao-villas", language="en")
    assert "Gwarantowane" not in name
    assert "Sunset" in name or "Bangtao" in name or "Villas" in name


def test_slug_to_title():
    assert slug_to_title("isla-kata-sea-view-villa") == "Isla Kata Sea View Villa"


def test_address_shortening():
    short = shorten_location(
        district="Bang Por",
        city="Bang Por",
        province="Thailand",
        location="123/45 Moo 1, Bang Por, Koh Samui, Surat Thani 84320, Thailand",
        address="123/45 Moo 1 ถนนใหญ่ Bang Por Koh Samui",
    )
    assert "Bang Por" in short
    assert "Koh Samui" in short
    assert "123/45" not in short
    assert "ถนน" not in short


def test_pool_label_private_vs_shared_condo():
    private = {
        "property_type": "Villa",
        "tags": ["private-pool"],
        "highlights": ["Private pool"],
        "amenities": [],
    }
    assert pool_display_label(private, "en") == "Private pool"

    condo = {
        "property_type": "Condominium",
        "tags": ["swimming-pool"],
        "highlights": ["Pool"],
        "amenities": ["pool"],
    }
    label = pool_display_label(condo, "en")
    assert label in ("Shared pool", "Pool")
    assert label != "Private pool"


def test_raw_slugs_hidden_in_highlights():
    card = {
        "property_type": "Condo",
        "tags": ["begovaya-dorozhka", "laundzh", "gym"],
        "highlights": ["begovaya-dorozhka", "plavatelnyy-basseyn"],
        "amenities": [],
        "description": "",
    }
    highs = clean_highlights_for_display(card["highlights"] + card["tags"], prop=card)
    joined = " ".join(highs).lower()
    assert "begovaya" not in joined
    assert "plavatelnyy" not in joined
    assert any("gym" in h.lower() or "running" in h.lower() or "pool" in h.lower() or "lounge" in h.lower() for h in highs)
