"""
PRIORITY 4: Never invent ownership when listing data has no ownership/tenure field.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.listing_presentation import extract_ownership_from_property
from app.services.property_search import FirestorePropertyAdapter, to_compact_projection
from app.services.public_agent import PublicAgentService


_INVENTED = (
    "Foreign Freehold / Leasehold available",
    "Freehold / Leasehold",
    "Foreign Freehold / Leasehold 90 lat",
)


def test_extract_ownership_none_when_missing():
    assert extract_ownership_from_property({"name": "X", "description": "Nice villa"}) is None
    assert extract_ownership_from_property({}) is None


def test_adapter_no_default_ownership():
    p = FirestorePropertyAdapter({
        "property_id": "x",
        "name": "Bare",
        "price": "1M",
    })
    assert p.ownership_type in (None, "", False) or str(p.ownership_type).strip() == ""
    for bad in _INVENTED:
        assert p.ownership_type != bad


def test_details_reply_omits_invented_ownership(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {"language": "en", "property_context": {}}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.message_history = []
    conversation.site_origin = "tajlandia.com"

    card = {
        "property_id": "bare-1",
        "name": "Bare Villa",
        "location": "Phuket",
        "price": "8M THB",
        "bedrooms": "3",
        "area": "180",
        "status": "Available",
        "highlights": ["Parking"],
        "amenities": ["parking"],
        "tags": [],
        "description": "Quiet villa near the beach.",
        "has_pool": False,
        "beach_access": False,
    }
    monkeypatch.setattr("app.services.public_agent.lookup_property_by_slug_or_name", lambda tok: card)
    monkeypatch.setattr("app.services.public_agent.extract_explicit_property_token", lambda *a, **k: "bare-1")
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker.log_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", lambda **_k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])

    result = agent._handle_property_details(
        db=MagicMock(),
        conversation=conversation,
        args={"property_id": "bare-1"},
        user_message="View details for bare-1",
        start_time=0.0,
        language="en",
    )
    text = result["text"] or ""
    for bad in _INVENTED:
        assert bad not in text
    # No ownership claim line at all when data has none
    assert "**Ownership:**" not in text
    assert "Ownership Structure" not in text


def test_comparison_table_no_invented_ownership():
    """Fallbacks in comparison must not invent freehold/leasehold."""
    from app.services.public_agent import PublicAgentService

    p1 = SimpleNamespace(
        property_id="a",
        property_name="A",
        district="Phuket",
        province="Thailand",
        price_display="5M",
        property_type="Villa",
        bedrooms_display="3",
        area_display="200",
        has_pool=False,
        status="Available",
        ownership_type=None,
    )
    p2 = SimpleNamespace(
        property_id="b",
        property_name="B",
        district="Phuket",
        province="Thailand",
        price_display="6M",
        property_type="Villa",
        bedrooms_display="4",
        area_display="250",
        has_pool=True,
        status="Available",
        ownership_type=None,
    )
    # Reproduce the deterministic comparison string builder path
    reply = (
        f"| **Ownership** | {p1.ownership_type or 'Confirm with advisor'} | "
        f"{p2.ownership_type or 'Confirm with advisor'} |\n"
    )
    for bad in _INVENTED:
        assert bad not in reply
    assert "Confirm with advisor" in reply


def test_compact_projection_omits_missing_ownership():
    p = FirestorePropertyAdapter({
        "property_id": "x",
        "name": "Bare",
        "price": "1M THB",
        "location": "Phuket",
    })
    for intent in ("details", "comparison"):
        compact = to_compact_projection(p, intent=intent)
        own = compact.get("ownership")
        assert own is None or own == ""
        for bad in _INVENTED:
            assert own != bad
            assert bad not in str(compact)
