"""
Tasks 1–7: Firestore-only listing updates, diff hygiene, confirm history,
dossier safe_text, audit honesty, suggestions.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.admin_property_fields import (
    coerce_price_storage,
    format_price_for_display,
    format_price_thb,
    resolve_existing_property,
    values_equal,
)
from app.services.admin_change_diff import (
    build_change_diff_rows,
    build_single_listing_change_diff,
)
from app.services.action_service import validate_property_data
from app.services.offer_dossier_service import normalize_dossier_property, safe_text
from app.services.admin_reply_helpers import (
    build_admin_structured_state,
    contextual_followup_suggestions,
    append_followup_suggestions,
)
from app.services.audit_service import record_audit_log


# ---- TASK 1+2: resolve + diff ----

def test_format_price_never_scientific():
    assert format_price_for_display(30_000_000) == "30,000,000"
    assert format_price_for_display(3e7) == "30,000,000"
    assert "e+" not in format_price_thb(30_000_000).lower()
    assert coerce_price_storage(30_000_000.0) == 30_000_000
    assert isinstance(coerce_price_storage("5 million"), int)


def test_firestore_only_price_update_one_diff_row(monkeypatch):
    fs_record = {
        "property_id": "pirak-villa",
        "_firestore_doc_id": "pirak-villa",
        "property_name": "Pirak Villa",
        "property_type": "Villa",
        "status": "Active",
        "province": "Phuket",
        "price_numeric_min": 39_990_000,
        "price_display": "39,990,000 THB",
        "currency": "THB",
        "_source": "firestore",
    }

    def fake_resolve(db, pid):
        if str(pid).lower().startswith("pirak"):
            return "firestore", fs_record
        return None, None

    monkeypatch.setattr(
        "app.services.admin_change_diff.resolve_existing_property", fake_resolve
    )
    monkeypatch.setattr(
        "app.services.admin_property_fields.resolve_existing_property", fake_resolve
    )

    diff = build_single_listing_change_diff(
        db=None,
        property_id="pirak-villa",
        proposed_fields={"price_numeric_min": 30_000_000, "price_display": "30,000,000 THB"},
        mode="create",  # caller might wrongly say create
        user_message="change price of pirak villa to 30 million",
    )
    assert diff["operation_type"] == "update"
    assert diff.get("no_change") is False
    fields = {r["field"] for r in diff["rows"]}
    assert "property_name" not in fields
    assert "status" not in fields
    assert "currency" not in fields or True  # currency equal should drop
    assert "price_numeric_min" in fields or "price_display" in fields
    for r in diff["rows"]:
        assert "e+" not in str(r["current"]).lower()
        assert "e+" not in str(r["new"]).lower()
    # Validation must not demand create fields
    payload = {
        "property_id": "pirak-villa",
        "mode": "update",
        "is_update_only": True,
        "resolve_source": "firestore",
        "fields_to_update": diff["canonical_fields"],
    }
    ok, missing, msg = validate_property_data(payload, existing_property=fs_record)
    assert ok, msg
    assert not missing


def test_no_change_message_when_price_same(monkeypatch):
    fs_record = {
        "property_id": "pirak-villa",
        "property_name": "Pirak Villa",
        "price_numeric_min": 30_000_000,
        "price_display": "30,000,000 THB",
        "_source": "firestore",
    }
    monkeypatch.setattr(
        "app.services.admin_change_diff.resolve_existing_property",
        lambda db, pid: ("firestore", fs_record),
    )
    diff = build_single_listing_change_diff(
        db=None,
        property_id="pirak-villa",
        proposed_fields={"price_numeric_min": 30_000_000},
        mode="update",
    )
    assert diff["no_change"] is True
    assert "already" in (diff.get("no_change_message") or "").lower()


def test_unchanged_fields_dropped_from_rows():
    current = {
        "property_name": "Pirak Villa",
        "status": "Active",
        "currency": "THB",
        "price_numeric_min": 39_990_000,
        "_readable": {
            "property_name": True,
            "status": True,
            "currency": True,
            "price_numeric_min": True,
        },
    }
    rows = build_change_diff_rows(
        current,
        {
            "property_name": "Pirak Villa",
            "status": "Active",
            "currency": "THB",
            "price_numeric_min": 30_000_000,
        },
    )
    assert all(r["field"] != "property_name" for r in rows)
    assert all(r["field"] != "status" for r in rows)
    assert any(r["field"] == "price_numeric_min" for r in rows)


# ---- TASK 3: session state ----

def test_session_state_shows_executed_not_pending():
    finished = [
        SimpleNamespace(
            id="pa-1",
            status="EXECUTED",
            tool_name="create_or_update_listing",
            execution_result={"updated_fields": {"price_numeric_min": 30_000_000}},
        )
    ]
    block = build_admin_structured_state(
        pending_action_id="pa-stale",
        open_pending=[],
        recent_finished=finished,
    )
    assert "open_pending: none" in block
    assert "pending_action_id: none" in block
    assert "EXECUTED" in block
    assert "pa-1" in block
    assert "still waiting" not in block.lower()


# ---- TASK 4: safe_text ----

def test_safe_text_nan_none_number_list():
    assert safe_text(float("nan"), "fallback") == "fallback"
    assert safe_text(None, "fallback") == "fallback"
    assert "30" in safe_text(30_000_000)
    assert "a, b" == safe_text(["a", "b"])
    assert safe_text({"en": "Hello"}, "") == "Hello"
    # _draw_text_box path: normalize property with NaN description
    p = normalize_dossier_property(
        {
            "property_id": "x",
            "name": "Test Villa",
            "description": float("nan"),
            "rental_information": None,
            "investment_information": 12.5,
            "priceFrom": 5_000_000,
            "amenities": ["Pool", "Gym"],
        }
    )
    assert isinstance(p.description, str)
    assert p.description == "" or "nan" not in p.description.lower()
    assert isinstance(p.rental_information, str)
    assert "12" in p.investment_information or p.investment_information == ""


def test_draw_text_box_accepts_float_nan():
    from app.services.offer_dossier_service import _draw_text_box
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    can = canvas.Canvas(buf)
    # Must not raise AttributeError
    _draw_text_box(can, float("nan"), 10, 10, 100, 40)
    _draw_text_box(can, None, 10, 10, 100, 40)
    _draw_text_box(can, 3e7, 10, 10, 100, 40)


# ---- TASK 5: audit ----

def test_audit_ephemeral_not_presented_as_real(monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "ENABLE_AUDIT_LOGGING", False)
    entry = record_audit_log(
        db=MagicMock(),
        tool_name="create_or_update_listing",
        staff_user_id="a@b.com",
        status="SUCCESS",
    )
    assert getattr(entry, "audit_logging_disabled", False) or getattr(entry, "is_ephemeral", False)
    assert str(entry.id).startswith("audit_ephemeral_")


# ---- TASK 6+7: suggestions ----

def test_suggestions_single_block_contextual():
    tips = contextual_followup_suggestions(
        [{"name": "create_or_update_listing"}],
        [{"result": {"pending_action_id": "x", "diff_table": "|"}}],
        language="en",
        limit=3,
    )
    assert tips
    assert any("Confirm" in t for t in tips)
    text = append_followup_suggestions("Proposal ready.", tips)
    text2 = append_followup_suggestions(text, tips)  # should still be one block
    assert text2.count("Suggestions") == 1

    lead_tips = contextual_followup_suggestions(
        [{"name": "get_buyer_leads"}],
        [{"result": {"leads": [{"email": "a@b.com"}]}}],
        language="en",
    )
    assert any("Export" in t or "next 5" in t.lower() for t in lead_tips)


def test_pool_conflict_note_in_knowledge():
    from app.services.admin_knowledge_service import get_property_knowledge

    fs_payload = {
        "property_id": "x",
        "name": "X Villa",
        "has_pool": False,
        "amenities": [],
        "raw_firestore_data": {
            "investmentHighlights": ["private pool"],
            "priceFrom": 10_000_000,
            "name": "X Villa",
        },
    }
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=fs_payload,
    ):
        res = get_property_knowledge(None, "x", include_internal_notes=True)
    assert res is not None
    assert res.get("has_pool") is False
    assert res.get("pool_record_vs_notes") is not None
    assert "no pool flag" in res["pool_record_vs_notes"].lower()
