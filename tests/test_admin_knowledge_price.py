"""TASK 4 — get_property_knowledge returns price fields (from the Firestore listing)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.admin_knowledge_service import get_property_knowledge


def _card(**extra):
    card = {
        "property_id": "pirak-villa",
        "name": "Pirak Villa",
        "property_name": "Pirak Villa",
        "status": "Available",
        "province": "Phuket",
        "district": "Kata",
        "area": "Phuket",
        "price_numeric_min": 39_990_000,
        "price_numeric_max": None,
        "price_display": "39,990,000 THB",
        "price": "39,990,000 THB",
        "currency": "THB",
        "bedrooms_min": 3,
        "bedrooms_max": 4,
        "bedrooms_display": "3-4",
        "bedrooms": "3-4",
        "bathrooms": 3,
        "area_display": "250 sqm",
        "property_type": "Villa",
        "has_pool": True,
        "developer_name": "Dev Co",
        "raw_firestore_data": {},
    }
    card.update(extra)
    return card


def test_knowledge_includes_price_fields_from_firestore():
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=_card(),
    ):
        res = get_property_knowledge(MagicMock(), "pirak-villa")

    assert res is not None
    assert res["price_numeric_min"] == 39_990_000
    assert "39,990,000" in str(res["price_display"])
    assert res["currency"] == "THB"
    assert res["bedrooms_display"] in ("3-4", "3")
    assert res.get("area_display") == "250 sqm"
    assert "internal_notes" not in res
    assert "developer_contact" not in (res.get("specifications") or {})


def test_knowledge_hides_internal_unless_asked():
    card = _card(
        property_id="p1", name="A", property_name="A", price_numeric_min=1, price_display="1",
        developer_name="Secret Dev",
        raw_firestore_data={"investmentHighlights": ["CONFIDENTIAL ROI 9.7%"]},
    )
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=card,
    ):
        hidden = get_property_knowledge(MagicMock(), "p1", include_internal_notes=False)
        shown = get_property_knowledge(MagicMock(), "p1", include_internal_notes=True)

    assert "internal_notes" not in hidden
    assert shown.get("internal_notes") and "CONFIDENTIAL" in shown["internal_notes"]


def test_knowledge_unknown_property_returns_none():
    with patch("app.services.firestore_service.get_property_by_id_from_firestore", return_value=None):
        assert get_property_knowledge(MagicMock(), "nope") is None
