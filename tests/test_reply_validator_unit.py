"""TASK 9: reply_validator unit coverage."""

from __future__ import annotations

from app.services.approved_statements import DEVELOPER_MARKETING_CLAIM_LABEL
from app.services.reply_validator import sanitize_listing_line_text, validate_reply


def test_validate_rejects_over_budget_card():
    ok, reasons = validate_reply(
        "Here is a villa for 12M THB",
        cards=[{
            "property_id": "x",
            "name": "X",
            "location": "Phuket",
            "price": "from 12.00M THB",
            "price_numeric_min": 12_000_000,
        }],
        applied_filters={"max_price": 5_000_000, "location": "Phuket"},
    )
    assert ok is False
    assert any("over_max_price" in r for r in reasons)


def test_validate_rejects_unlabelled_guaranteed_roi():
    ok, reasons = validate_reply(
        "This project offers guaranteed 8% ROI.",
        cards=[{"property_id": "x", "name": "X", "price": "1M", "price_numeric_min": 1_000_000}],
        applied_filters={},
    )
    assert ok is False
    assert any("marketing" in r or "percentage" in r for r in reasons)


def test_sanitize_strips_guarantee_claims():
    cleaned = sanitize_listing_line_text("Villa with Gwarantowane 8% ROI Netto near the beach.")
    assert "Gwarantowane 8% ROI Netto" not in cleaned
    assert DEVELOPER_MARKETING_CLAIM_LABEL in cleaned or "8%" not in cleaned


def test_named_property_missing_flagged():
    ok, reasons = validate_reply(
        "Here are some villas",
        cards=[{
            "property_id": "other",
            "name": "Other Place",
            "price": "5M",
            "price_numeric_min": 5_000_000,
        }],
        applied_filters={"asked_property_name": "Anava Samui"},
    )
    assert ok is False
    assert any("named_property_missing" in r for r in reasons)
