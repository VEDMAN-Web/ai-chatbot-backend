import pytest
from app.services.property_context_service import PropertyContext, touch_discussed
from app.services.intent_router import (
    detect_deterministic_intent,
    IntentType,
    extract_search_filters_deterministic,
)
from app.services.amenity_service import collect_property_amenities
from app.services.public_agent import is_off_topic_query, _listing_fact_reply

def test_u04_single_card_turn_does_not_overwrite_ordinals():
    ctx = PropertyContext()
    # Batch of 3 properties
    ctx = touch_discussed(ctx, ["prop-1", "prop-2", "prop-3"], refresh_ordinals=True)
    assert ctx.reference_map.get("1st") == "prop-1"
    assert ctx.reference_map.get("2nd") == "prop-2"
    assert ctx.reference_map.get("3rd") == "prop-3"

    # Now a single-card turn (e.g. details lookup of prop-1)
    ctx = touch_discussed(ctx, ["prop-1"], refresh_ordinals=True)
    # The ordinals for second and third MUST NOT be erased
    assert ctx.reference_map.get("1st") == "prop-1"
    assert ctx.reference_map.get("2nd") == "prop-2"
    assert ctx.reference_map.get("3rd") == "prop-3"

def test_u05_compare_side_by_side_routing():
    msg = "Compare Botanica Four Seasons and The Line Phahol side by side"
    res = detect_deterministic_intent(msg, stored_state={})
    assert res is not None
    intent, data = res
    assert intent == IntentType.PROPERTY_COMPARISON
    pids = [str(p).lower() for p in data.get("property_ids", [])]
    assert len(pids) == 2
    assert any("botanica" in p for p in pids)
    assert any("phahol" in p or "line" in p for p in pids)

def test_u06_beach_access_not_falsely_matched_on_general_beach_mentions():
    # Description mentions distance to beach or driving to beach
    prop = {
        "property_id": "test-villa",
        "description": "Located in a quiet area, just a 15-minute drive to the beach. Close to shops."
    }
    facts = collect_property_amenities(prop)
    # Bare "beach" in description must NOT trigger beach_access
    assert facts["beach_access"].present is False

def test_u07_the_title_property_name_does_not_trigger_ownership_non_answer():
    card = {
        "property_id": "the-title-heritage",
        "name": "The Title Heritage",
        "price": "6,500,000 THB",
        "location": "Bang Tao, Phuket",
    }
    # Asking about The Title Heritage without asking about ownership/title deeds
    reply = _listing_fact_reply(
        focus_q="Tell me more about The Title Heritage",
        card=card,
        language="en",
        conversation=None,
        p_owner=None,
    )
    # Should be None (not handled as a single-topic ownership answer) so it falls back to full details
    assert reply is None

def test_u08_property_in_does_not_extract_in_as_id():
    msg = "Show me property in Phuket"
    res = detect_deterministic_intent(msg, stored_state={})
    if res:
        intent, data = res
        assert data.get("property_id") != "IN"

def test_u12_contact_questions_are_on_topic():
    assert is_off_topic_query("What is your phone number?") is False
    assert is_off_topic_query("How can I contact your office?") is False
    assert is_off_topic_query("What is your email address?") is False
    assert is_off_topic_query("Jaki jest wasz numer telefonu?") is False

def test_u13_castle_in_paris_is_foreign_location():
    filters = extract_search_filters_deterministic("Castle in Paris")
    loc = filters.get("foreign_location")
    assert loc is not None
    assert str(loc).lower() in ("france", "paris")
