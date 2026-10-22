"""Pure unit tests for validate_reply (fake listings only, no network)."""
import pytest

from app.services.reply_validator import validate_reply
import app.services.reply_validator as rv_mod
import app.services.approved_statements as approved_mod


CARD_OK = {
    "property_id": "ocean-vista",
    "name": "Ocean Vista Villa",
    "location": "Phuket, Layan",
    "price": "from 12.00M THB",
    "price_numeric_min": 12_000_000,
    "property_type": "Villa",
    "bedrooms": 3,
}

CARD_OVER = {
    "property_id": "pricey",
    "name": "High Price Estate",
    "location": "Phuket",
    "price": "from 30.69M THB",
    "price_numeric_min": 30_690_000,
    "property_type": "Villa",
    "bedrooms": 4,
}

CARD_CHEAP = {
    "property_id": "cheap",
    "name": "Budget Studio",
    "location": "Bangkok",
    "price": "from 3.00M THB",
    "price_numeric_min": 3_000_000,
    "property_type": "Condo",
    "bedrooms": 1,
}

CARD_POR = {
    "property_id": "por-1",
    "name": "Mystery Condo",
    "location": "Phuket",
    "price": "Price on request",
    "price_numeric_min": None,
    "property_type": "Condo",
    "bedrooms": 2,
}

CARD_PL = {
    "property_id": "pl-willa",
    "name": "Willa Larimar",
    "location": "Phuket",
    "price": "from 13.65M THB",
    "price_numeric_min": 13_650_000,
    "property_type": "Villa",
    "bedrooms": 3,
}


def test_1_price_above_max_price():
    ok, reasons = validate_reply(
        reply_text="High Price Estate from 30.69M THB",
        cards=[CARD_OVER],
        applied_filters={"max_price": 15_000_000},
        tenant_advisor_email="advisor@thai.estate",
    )
    assert ok is False
    assert any(r.startswith("over_max_price") for r in reasons)


def test_2_price_below_min_price():
    ok, reasons = validate_reply(
        reply_text="Budget Studio from 3.00M THB",
        cards=[CARD_CHEAP],
        applied_filters={"min_price": 8_000_000},
        tenant_advisor_email="advisor@thai.estate",
    )
    assert ok is False
    assert any(r.startswith("under_min_price") for r in reasons)


def test_3_price_on_request_labelling():
    ok_bad, reasons_bad = validate_reply(
        reply_text="Here is Mystery Condo in Phuket.",
        cards=[CARD_POR],
        applied_filters={"max_price": 15_000_000},
        tenant_advisor_email="advisor@thai.estate",
    )
    assert ok_bad is False
    assert any(r.startswith("price_on_request_unlabelled") for r in reasons_bad)

    ok_good, reasons_good = validate_reply(
        reply_text="Mystery Condo is Price on request.",
        cards=[CARD_POR],
        applied_filters={"max_price": 15_000_000},
        tenant_advisor_email="advisor@thai.estate",
    )
    assert not any(r.startswith("price_on_request_unlabelled") for r in reasons_good)
    assert ok_good is True


def test_4_unapproved_percentage_then_approved(monkeypatch):
    reply = "Typically 6%-10% net yield for Ocean Vista Villa from 12.00M THB."
    ok, reasons = validate_reply(
        reply_text=reply,
        cards=[CARD_OK],
        applied_filters={"location": "Phuket"},
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is False
    assert any(r.startswith("unapproved_percentage") for r in reasons)

    monkeypatch.setattr(approved_mod, "APPROVED_STATEMENTS", ["Typically 6%-10% net yield"])
    monkeypatch.setattr(rv_mod, "APPROVED_STATEMENTS", ["Typically 6%-10% net yield"])
    ok2, reasons2 = validate_reply(
        reply_text=reply,
        cards=[CARD_OK],
        applied_filters={"location": "Phuket"},
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert not any(r.startswith("unapproved_percentage") for r in reasons2)


def test_5_price_not_in_data():
    ok, reasons = validate_reply(
        reply_text="Ocean Vista Villa costs THB 99,000,000 in Phuket.",
        cards=[CARD_OK],
        applied_filters={"location": "Phuket"},
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is False
    assert any(r.startswith("price_not_in_data") for r in reasons)


def test_6_invented_property_name():
    ok, reasons = validate_reply(
        reply_text="Consider Sunset Paradise Villas from 20M THB.",
        cards=[],
        applied_filters={},
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is False
    assert any("invented_property_name" in r or "unknown_property_name" in r for r in reasons)


def test_7_luxury_pool_villas_not_flagged():
    ok, reasons = validate_reply(
        reply_text="Luxury Pool Villas in Phuket include Ocean Vista Villa from 12.00M THB.",
        cards=[CARD_OK],
        applied_filters={"location": "Phuket", "max_price": 20_000_000},
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is True, reasons
    assert not any("property_name" in r for r in reasons)


def test_8_phuket_villas_not_flagged():
    ok, reasons = validate_reply(
        reply_text="Phuket Villas: Ocean Vista Villa from 12.00M THB.",
        cards=[CARD_OK],
        applied_filters={"location": "Phuket"},
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is True, reasons
    assert not any("property_name" in r for r in reasons)


def test_9_wrong_and_correct_advisor_email():
    ok_bad, reasons_bad = validate_reply(
        reply_text="Ocean Vista Villa from 12.00M THB. Contact advisor@varsoviaestate.com",
        cards=[CARD_OK],
        applied_filters={"location": "Phuket"},
        tenant_advisor_email="advisor@thai.estate",
    )
    assert ok_bad is False
    assert any(r.startswith("wrong_advisor_email") for r in reasons_bad)

    ok_good, reasons_good = validate_reply(
        reply_text="Ocean Vista Villa from 12.00M THB. Contact advisor@thai.estate",
        cards=[CARD_OK],
        applied_filters={"location": "Phuket"},
        tenant_advisor_email="advisor@thai.estate",
    )
    assert ok_good is True, reasons_good


def test_10_location_type_bedrooms_mismatch():
    mismatched = {
        "property_id": "mm-1",
        "name": "Wrong Fit Condo",
        "location": "Bangkok",
        "price": "from 10M THB",
        "price_numeric_min": 10_000_000,
        "property_type": "Condo",
        "bedrooms": 1,
    }
    ok, reasons = validate_reply(
        reply_text="Wrong Fit Condo from 10M THB",
        cards=[mismatched],
        applied_filters={
            "location": "Phuket",
            "property_type": "Villa",
            "bedrooms": 4,
            "max_price": 50_000_000,
        },
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is False
    assert any(r.startswith("location_mismatch") for r in reasons)
    assert any(r.startswith("type_mismatch") for r in reasons)
    assert any(r.startswith("bedrooms_mismatch") for r in reasons)


def test_11_clean_reply_passes():
    ok, reasons = validate_reply(
        reply_text="Ocean Vista Villa (Phuket, Layan): from 12.00M THB. Advisor: advisor@tajlandia.com",
        cards=[CARD_OK],
        applied_filters={
            "max_price": 15_000_000,
            "location": "Phuket",
            "property_type": "Villa",
            "bedrooms": 3,
        },
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is True
    assert reasons == []


def test_12_polish_price_13_65m():
    ok, reasons = validate_reply(
        reply_text="Willa za 13,65 mln THB: Willa Larimar from 13.65M THB.",
        cards=[CARD_PL],
        applied_filters={"max_price": 20_000_000, "location": "Phuket"},
        tenant_advisor_email="advisor@tajlandia.com",
        language="pl",
    )
    assert ok is True, reasons
