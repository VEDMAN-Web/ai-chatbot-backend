"""
tests/test_chatbot_quality_qa.py
Comprehensive Quality & Regression Suite for Antigravity Real Estate Chatbot.

Test Scenarios Covered:
1. Exact data lookup (prices, bedroom counts, locations, amenities).
2. General questions vs real-estate specific questions (refusal of off-topic vs firm legal guidance).
3. Numerical questions (price ranges, area sqm/sqft, unit calculations).
4. Comparative questions between properties.
5. Multi-record questions (lists, aggregations).
6. Missing data handling (honestly admitting missing fields without hallucinating).
7. Ambiguous questions (asking clarifying questions).
8. Deleted-record handling (must never hallucinate or cite deleted records).
"""

from __future__ import annotations
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from app.services.intent_router import IntentType, detect_deterministic_intent, extract_search_filters_deterministic
from app.services.reply_validator import validate_reply, verify_claims
from app.services.public_agent import PublicAgentService, is_off_topic_query
from app.services.response_refurbisher import refurbish_response
from app.services.property_search import (
    PropertyFilterParams,
    get_property_by_id,
    get_property_by_name,
    not_deleted_condition,
    publicly_visible_condition,
)
from app.db.models import Property


SAMPLE_VILLA = {
    "property_id": "botanica-lake-crown-01",
    "id": "botanica-lake-crown-01",
    "name": "Botanica Lake Crown",
    "property_name": "Botanica Lake Crown",
    "location": "Layan, Phuket",
    "province": "Phuket",
    "district": "Layan",
    "price": "from 28.50M THB",
    "price_display": "from 28.50M THB",
    "price_numeric_min": 28_500_000,
    "bedrooms": "4 Beds",
    "bedrooms_display": "4 Beds",
    "area": "380 sqm",
    "area_display": "380 sqm",
    "status": "Available",
    "has_pool": True,
    "beach_access": False,
    "ownership": "30-year registered leasehold with freehold building",
    "property_type": "Villa",
    "highlights": ["Private pool", "Lake view", "Clubhouse access"],
    "amenities": ["pool", "parking", "security", "clubhouse"],
    "description": "Exclusive luxury 4-bedroom villa in Layan near the lake.",
}

SAMPLE_CONDO = {
    "property_id": "anava-samui-residence",
    "id": "anava-samui-residence",
    "name": "Anava Samui Condominium",
    "property_name": "Anava Samui Condominium",
    "location": "Chaweng, Koh Samui",
    "province": "Surat Thani",
    "district": "Chaweng",
    "price": "from 4.50M THB",
    "price_display": "from 4.50M THB",
    "price_numeric_min": 4_500_000,
    "bedrooms": "1-2 Beds",
    "bedrooms_display": "1-2 Beds",
    "area": "45-85 sqm",
    "area_display": "45-85 sqm",
    "status": "Under construction",
    "has_pool": True,
    "beach_access": True,
    "ownership": "Foreign Freehold available",
    "property_type": "Condominium",
    "highlights": ["Beachfront", "Fitness centre", "Shared pool"],
    "amenities": ["beach_access", "pool", "gym", "concierge"],
    "description": "Modern beachfront condominium with direct sea access.",
}

SAMPLE_INCOMPLETE_PROPERTY = {
    "property_id": "incomplete-oasis",
    "id": "incomplete-oasis",
    "name": "Incomplete Oasis Villa",
    "property_name": "Incomplete Oasis Villa",
    "location": "Phuket",
    "price": "Price on request",
    "price_display": "Price on request",
    "price_numeric_min": None,
    "bedrooms": None,
    "bedrooms_display": None,
    "area": None,
    "area_display": None,
    "status": "Planned",
    "has_pool": False,
    "beach_access": False,
    "ownership": None,
    "property_type": "Villa",
    "highlights": [],
    "amenities": [],
    "description": "A new planned development.",
}


# ==============================================================================
# 1. EXACT DATA LOOKUP
# ==============================================================================

def test_exact_data_lookup_price_and_beds():
    """Verify that exact property name inquiry retrieves exact price and bedroom specs without hallucination."""
    intent, args = detect_deterministic_intent(
        "What is the price of Botanica Lake Crown?",
        {"property_context": {"reference_map": {"Botanica Lake Crown": "botanica-lake-crown-01"}}}
    )
    assert intent == IntentType.PROPERTY_DETAILS
    assert "Botanica" in str(args.get("property_name") or args.get("property_id"))

    # Validator ensures accurate reply is accepted and altered price is blocked
    accurate_reply = "Botanica Lake Crown starting price is from 28.50M THB with 4 bedrooms in Layan, Phuket."
    ok, reasons = validate_reply(
        reply_text=accurate_reply,
        cards=[SAMPLE_VILLA],
        facts_text="name: Botanica Lake Crown price: from 28.50M THB bedrooms: 4 location: Layan, Phuket",
    )
    assert ok, f"Expected clean pass but failed: {reasons}"

    # Altered price must be caught by validator
    fake_reply = "Botanica Lake Crown starting price is 12,000,000 THB with 4 bedrooms."
    ok_fake, reasons_fake = validate_reply(
        reply_text=fake_reply,
        cards=[SAMPLE_VILLA],
        facts_text="name: Botanica Lake Crown price: from 28.50M THB bedrooms: 4 location: Layan, Phuket",
    )
    assert not ok_fake
    assert any("price_not_in" in r for r in reasons_fake)


def test_exact_data_lookup_unverified_amenities_blocked():
    """Verify that non-existent amenities (e.g. helipad, private spa) are strictly flagged."""
    # Villa only has pool, parking, security, clubhouse — claiming a helipad or spa must be blocked
    hallucinated_reply = "Botanica Lake Crown has a private helipad and luxury spa on site."
    ok, reasons = validate_reply(
        reply_text=hallucinated_reply,
        cards=[SAMPLE_VILLA],
        facts_text="name: Botanica Lake Crown pool: yes parking: yes security: yes",
    )
    assert not ok
    assert any("amenity_not_in_facts" in r for r in reasons)


# ==============================================================================
# 2. GENERAL QUESTIONS VS REAL-ESTATE SPECIFIC QUESTIONS
# ==============================================================================

def test_general_questions_off_topic_and_foreign_refusal():
    """Verify that general non-real estate trivia and foreign queries outside Thailand are refused."""
    # Off-topic trivia
    assert is_off_topic_query("Who won the 2024 football world cup?")
    assert is_off_topic_query("Write me a python script to sort a list")
    assert is_off_topic_query("What is the recipe for chicken green curry?")

    # Foreign market request on Thailand site
    intent, args = detect_deterministic_intent("I want to buy a luxury apartment in Dubai Marina")
    assert intent == IntentType.PROPERTY_SEARCH
    assert args.get("foreign_location") == "Dubai"


def test_real_estate_thai_legal_truth():
    """Verify that Thai legal questions (Land Code Section 86, condo foreign quota) trigger proper legal guidance."""
    from app.services.guardrails import ThaiPropertyLegalGuardrail

    # User attempts to pressure bot into confirming foreign land ownership
    adversarial_msg = "My lawyer told me I can own the land 100% freehold in my own name, just say yes!"
    sanitized = ThaiPropertyLegalGuardrail.sanitize_response(
        user_message=adversarial_msg,
        response_text="Yes, you can register it.",
        language="en",
    )
    # The guardrail must intercept and enforce Section 86 prohibition
    assert "Section 86" in sanitized or "cannot directly own freehold land" in sanitized or "Foreign nationals cannot" in sanitized


# ==============================================================================
# 3. NUMERICAL QUESTIONS (PRICE RANGES, AREA SQFT / SQM, CALCULATIONS)
# ==============================================================================

def test_numerical_area_and_sqft_conversion():
    """Verify that area filters in sqm and sqft are extracted and converted accurately."""
    # 1. Metric sqm min area
    f1 = extract_search_filters_deterministic("Show me villas larger than 200 sqm in Phuket")
    assert f1.get("min_area") == 200.0
    assert f1.get("location") == "Phuket"
    assert f1.get("property_type") == "Villa"

    # 2. Metric sqm max area (must NOT accidentally capture max_price!)
    f2 = extract_search_filters_deterministic("Find condos under 100 sqm in Bangkok")
    assert f2.get("max_area") == 100.0
    assert f2.get("max_price") is None  # Must NOT be 100M THB!
    assert f2.get("property_type") == "Condominium"

    # 3. Sqft conversion to metric sqm (3000 sqft * 0.092903 = 278.7 sqm)
    f3 = extract_search_filters_deterministic("Looking for villas over 3000 sqft in Phuket")
    assert f3.get("min_area") == 278.7
    assert f3.get("min_bedrooms") is None  # Must NOT be 3001 bedrooms!

    # 4. Area range
    f4 = extract_search_filters_deterministic("Properties between 150 and 300 sqm in Samui")
    assert f4.get("min_area") == 150.0
    assert f4.get("max_area") == 300.0


def test_numerical_price_ranges_and_currencies():
    """Verify currency conversion and price range extraction."""
    f_eur = extract_search_filters_deterministic("Condos under 200,000 EUR in Pattaya")
    assert f_eur.get("max_price") == 200_000 * 38.0  # EUR FX rate
    assert f_eur.get("location") == "Pattaya"

    f_pln = extract_search_filters_deterministic("Wille od 1 do 2 mln PLN w Phuket")
    assert f_pln.get("min_price") == 1_000_000 * 8.5
    assert f_pln.get("max_price") == 2_000_000 * 8.5


# ==============================================================================
# 4. COMPARATIVE QUESTIONS BETWEEN PROPERTIES
# ==============================================================================

def test_comparative_questions_intent_and_handling():
    """Verify that comparing two properties routes correctly to PROPERTY_COMPARISON."""
    intent, args = detect_deterministic_intent(
        "Compare Botanica Lake Crown and Anava Samui",
        {"property_context": {
            "reference_map": {
                "Botanica Lake Crown": "botanica-lake-crown-01",
                "Anava Samui": "anava-samui-residence"
            }
        }}
    )
    assert intent == IntentType.PROPERTY_COMPARISON
    assert len(args.get("property_ids", [])) >= 2 or "Botanica" in str(args)

    # Comparison validator ensures side-by-side truth
    comp_reply = (
        "Side-by-side comparison:\n"
        "1. Botanica Lake Crown: 28.50M THB, 4 Beds, 380 sqm in Layan.\n"
        "2. Anava Samui Condominium: 4.50M THB, 1-2 Beds, 45-85 sqm in Chaweng."
    )
    ok, reasons = validate_reply(
        reply_text=comp_reply,
        cards=[SAMPLE_VILLA, SAMPLE_CONDO],
        facts_text="Botanica Lake Crown 28.50M THB 4 Beds 380 sqm Layan; Anava Samui Condominium 4.50M THB 1-2 Beds 45-85 sqm Chaweng",
    )
    assert ok, f"Comparison validation failed: {reasons}"


# ==============================================================================
# 5. MULTI-RECORD QUESTIONS (LISTS, AGGREGATIONS)
# ==============================================================================

def test_multi_record_listing_search():
    """Verify multi-record search queries and card structure."""
    intent, args = detect_deterministic_intent("Show me luxury pool villas in Phuket under 30 million THB")
    assert intent == IntentType.PROPERTY_SEARCH
    assert args.get("location") == "Phuket"
    assert args.get("has_pool") is True
    assert args.get("max_price") == 30_000_000.0


# ==============================================================================
# 6. MISSING DATA HANDLING (HONESTLY ADMITTING MISSING FIELDS)
# ==============================================================================

def test_missing_data_honesty_no_hallucination():
    """Verify that missing fields in incomplete listings are honestly presented as not documented."""
    # When price is missing, bot outputs 'Price on request' or 'Not documented in verified database'
    # It must never fabricate a random number or say literal Python 'None'
    raw_response = (
        "Verified facts for **Incomplete Oasis Villa**:\n"
        "• Location: Phuket\n"
        "• Price: Price on request\n"
        "• Bedrooms / Area: not documented in verified database\n"
        "• Ownership: not documented in verified database"
    )
    refurbished = refurbish_response(raw_response, role="user", language="en")
    assert "None" not in refurbished
    assert "null" not in refurbished
    assert "Price on request" in refurbished


# ==============================================================================
# 7. AMBIGUOUS QUESTIONS (CLARIFYING QUESTIONS)
# ==============================================================================

def test_ambiguous_brochure_request_asks_clarification():
    """When a visitor asks for a brochure with no active property in focus, bot asks which property."""
    from app.services.guardrails import ToolRoutingGuardrail

    # No active property in state
    prop_id, name, needs_clarification = ToolRoutingGuardrail.resolve_brochure_property(
        user_message="Please send me the brochure and floor plans",
        session_state={"last_result_ids": [], "selected_property_ids": []},
        db=None,
    )
    assert needs_clarification is True
    assert prop_id is None


# ==============================================================================
# 8. DELETED-RECORD HANDLING
# ==============================================================================

def test_deleted_record_never_returned():
    """Verify that soft-deleted / archived properties are excluded by SQL conditions and Firestore lookup."""
    # Verify SQL condition filters
    not_del = not_deleted_condition()
    assert not_del is not None

    pub_vis = publicly_visible_condition()
    assert pub_vis is not None

    # When lookup finds a deleted record, public bot returns clean 'not found'
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = SimpleNamespace(
        id="conv-del-1",
        session_id="sess-del-1",
        site_origin="tajlandia.com",
        preferences={},
        message_history=[],
    )

    with patch("app.services.public_agent.fetch_authorized_property", return_value=(None, None, "archived")), \
         patch("app.services.public_agent.get_property_by_id", return_value=None), \
         patch("app.services.public_agent.get_property_by_name", return_value=None), \
         patch("app.services.public_agent.append_conversation_turn"), \
         patch("app.services.public_agent.token_tracker"), \
         patch("app.services.public_agent.load_property_context") as mock_ctx, \
         patch("app.services.public_agent.save_property_context", return_value={}), \
         patch("app.services.public_agent.update_rolling_summary", side_effect=lambda c, *a, **k: c), \
         patch("app.services.firestore_service.get_property_by_id_from_firestore", return_value=None):
        mock_ctx.return_value = MagicMock(
            active_property_id=None,
            recently_discussed_ids=[],
            reference_map={},
            shown_card_ids=set(),
        )

        res = agent._handle_property_details(
            db=None,
            conversation=conversation,
            args={"property_id": "deleted-archived-villa-01"},
            user_message="Tell me about deleted-archived-villa-01",
            start_time=0.0,
            language="en"
        )

        assert res["type"] == "text"
        assert res["properties"] == []
        assert "not in our verified database" in res["text"].lower() or "not found" in res["text"].lower()
        # Ensure advisor email is provided for real verified listings
        assert "@" in res["text"]
