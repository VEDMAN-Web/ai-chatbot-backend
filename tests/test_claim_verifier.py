"""
TASK 3: Claim verifier catches invented amenities, yields, dates, names, ownership.
"""
from __future__ import annotations

from app.services.reply_validator import validate_reply, verify_claims


_FACTS_CARD = {
    "property_id": "bellaguna-lotus",
    "name": "Bellaguna Lake Residences Lotus",
    "location": "Phuket",
    "price": "from 12,500,000 THB",
    "price_numeric_min": 12_500_000,
    "property_type": "Villa",
    "bedrooms": "3 Beds",
    "bathrooms": "3",
    "area": "180 sqm",
    "status": "under_construction",
    "ownership": "Leasehold",
    "has_pool": True,
    "amenities": ["Pool", "Parking"],
    "highlights": ["Private pool"],
}


def test_verify_claims_catches_spa_yield_zorblax_freehold_completion():
    reply = (
        "Bellaguna Lake Residences Lotus is lovely. Spa: Yes. "
        "It offers 8% rental yield. Completion Q2 2027. "
        "Also consider Zorblax Towers nearby — freehold for foreigners."
    )
    ok, reasons = verify_claims(
        reply_text=reply,
        cards=[_FACTS_CARD],
        facts_text=(
            "name=Bellaguna Lake Residences Lotus; location=Phuket; "
            "price=12500000; bedrooms=3; ownership=Leasehold; amenities=pool,parking"
        ),
        allowed_property_ids={"bellaguna-lotus"},
        allowed_property_names={"bellaguna lake residences lotus"},
    )
    assert ok is False
    joined = " ".join(reasons).lower()
    assert "spa" in joined or "amenity" in joined
    assert "yield" in joined or "percentage" in joined or "8%" in joined
    assert "zorblax" in joined or "invented" in joined or "unknown_property" in joined
    assert "freehold" in joined or "ownership" in joined
    assert "2027" in joined or "completion" in joined or "date" in joined


def test_correct_reply_passes_unchanged():
    reply = (
        "Bellaguna Lake Residences Lotus in Phuket starts from 12.5M THB "
        "(12,500,000 THB). It has 3 bedrooms and a pool. Ownership: Leasehold."
    )
    ok, reasons = verify_claims(
        reply_text=reply,
        cards=[_FACTS_CARD],
        facts_text=(
            "name=Bellaguna Lake Residences Lotus; location=Phuket; "
            "price=12500000; price_display=12.5M THB; bedrooms=3; "
            "ownership=Leasehold; amenities=pool,parking,private pool"
        ),
        allowed_property_ids={"bellaguna-lotus"},
        allowed_property_names={"bellaguna lake residences lotus"},
    )
    assert ok is True, reasons


def test_number_formats_12_5m_match_12500000():
    reply = "Starting price is 12.5M THB for Bellaguna Lake Residences Lotus."
    ok, reasons = verify_claims(
        reply_text=reply,
        cards=[_FACTS_CARD],
        facts_text="name=Bellaguna Lake Residences Lotus; price=12500000; price_display=from 12,500,000 THB",
        allowed_property_ids={"bellaguna-lotus"},
        allowed_property_names={"bellaguna lake residences lotus"},
    )
    assert ok is True, reasons


def test_validate_reply_integrates_claim_checks():
    reply = "Zorblax Towers has Spa: Yes and 8% rental yield with freehold for foreigners."
    ok, reasons = validate_reply(
        reply_text=reply,
        cards=[_FACTS_CARD],
        applied_filters={},
        language="en",
    )
    assert ok is False
    assert reasons
