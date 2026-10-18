"""TASK 4 — get_property_knowledge returns price fields."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.admin_knowledge_service import get_property_knowledge


def test_knowledge_includes_price_fields_from_pg():
    prop = SimpleNamespace(
        property_id="pirak-villa",
        property_name="Pirak Villa",
        status="Available",
        province="Phuket",
        district="Kata",
        area="Phuket",
        price_numeric_min=39_990_000,
        price_numeric_max=None,
        price_display="39,990,000 THB",
        currency="THB",
        bedrooms_min=3,
        bedrooms_max=4,
        bedrooms_display="3-4",
        bathrooms=3,
        area_display="250 sqm",
        property_type="Villa",
        has_pool=True,
        developer_name="Dev Co",
    )
    db = MagicMock()
    q_prop = MagicMock()
    q_prop.filter.return_value.first.return_value = prop
    q_spec = MagicMock()
    q_spec.filter.return_value.first.return_value = None

    def query_side(model):
        name = getattr(model, "__name__", str(model))
        if "Spec" in name:
            return q_spec
        return q_prop

    db.query.side_effect = query_side

    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=None,
    ):
        res = get_property_knowledge(db, "pirak-villa")

    assert res is not None
    assert res["price_numeric_min"] == 39_990_000
    assert "39,990,000" in str(res["price_display"])
    assert res["currency"] == "THB"
    assert res["bedrooms_display"] in ("3-4", "3")
    assert res.get("area_display") == "250 sqm"
    assert "internal_notes" not in res
    assert "developer_contact" not in (res.get("specifications") or {})


def test_knowledge_hides_internal_unless_asked():
    prop = SimpleNamespace(
        property_id="p1",
        property_name="A",
        status="Available",
        province="Phuket",
        district=None,
        area=None,
        price_numeric_min=1,
        price_numeric_max=None,
        price_display="1",
        currency="THB",
        bedrooms_min=1,
        bedrooms_max=1,
        bedrooms_display="1",
        bathrooms=1,
        area_display=None,
        property_type="Villa",
        has_pool=False,
        developer_name="Secret Dev",
    )
    spec = SimpleNamespace(
        roof_material=None,
        roof_colour=None,
        flooring=None,
        walls=None,
        doors_windows=None,
        exterior_materials=None,
        interior_materials=None,
        architectural_style=None,
        construction_details={},
        internal_notes="CONFIDENTIAL ROI 9.7%",
    )
    db = MagicMock()
    q_prop = MagicMock()
    q_prop.filter.return_value.first.return_value = prop
    q_spec = MagicMock()
    q_spec.filter.return_value.first.return_value = spec

    def query_side(model):
        name = getattr(model, "__name__", str(model))
        if "Spec" in name:
            return q_spec
        return q_prop

    db.query.side_effect = query_side

    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=None,
    ):
        hidden = get_property_knowledge(db, "p1", include_internal_notes=False)
        shown = get_property_knowledge(db, "p1", include_internal_notes=True)

    assert "internal_notes" not in hidden
    assert shown.get("internal_notes") and "CONFIDENTIAL" in shown["internal_notes"]
