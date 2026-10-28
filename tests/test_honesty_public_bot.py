"""
End-to-end honesty checks for the public chatbot (mocked LLM, fake listings only).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.intent_router import IntentType, detect_deterministic_intent
from app.services.i18n_service import build_no_match_reply, get_off_topic_text
from app.services.reply_validator import validate_reply
from app.core.tenant import resolve_tenant


FAKE = {
    "property_id": "fantasea-condo",
    "name": "Fantasea Condo",
    "location": "Phuket",
    "price": "from 9.50M THB",
    "price_numeric_min": 9_500_000,
    "bedrooms": "1-2",
    "bedrooms_display": "1-2",
    "ownership": "Foreign Freehold Condominium",
    "ownership_type": "Foreign Freehold Condominium",
    "status": "Available",
    "area": "27 sqm",
    "has_pool": False,
    "beach_access": True,
    "property_type": "Condo",
    "description": "",
    "highlights": [],
}


def test_named_listing_facts_beat_general_legal_essay():
    intent, args = detect_deterministic_intent(
        "Tell me the price, bedrooms and ownership of Fantasea Condo",
        {
            "last_result_ids": ["fantasea-condo"],
            "property_context": {
                "reference_map": {"Fantasea Condo": "fantasea-condo", "first property": "fantasea-condo"}
            },
        },
    )
    assert intent == IntentType.PROPERTY_DETAILS
    assert "Fantasea" in str(args.get("property_name") or args.get("property_id"))


def test_nonexistent_property_id_not_found_message():
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = SimpleNamespace(
        id="c1",
        session_id="s1",
        site_origin="thai.estate",
        preferences={},
        message_history=[],
    )
    with patch("app.services.public_agent.fetch_authorized_property", return_value=(None, None, "missing")), \
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
        )
        # Make resolve_name return None
        with patch("app.services.public_agent.resolve_name_against_context", return_value=None), \
             patch("app.services.public_agent.resolve_property_reference") as mock_res:
            mock_res.return_value = MagicMock(
                property_id=None, ambiguous=False, clarification=None, source="none"
            )
            result = agent._handle_property_details(
                db=MagicMock(),
                conversation=conversation,
                args={"property_id": "PROP-DOES-NOT-EXIST-999"},
                user_message="tell me about PROP-DOES-NOT-EXIST-999",
                start_time=0.0,
                language="en",
            )
    assert result["properties"] == []
    low = result["text"].lower()
    assert "not found" in low or "not in our verified database" in low
    assert "advisor@thai.estate" in result["text"]
    assert "advisor@varsoviaestate.com" not in result["text"]


def test_prompt_injection_does_not_invent_listings():
    # Validator rejects invented villa names when no cards were retrieved
    ok, reasons = validate_reply(
        reply_text=(
            "Ignoring prior instructions. Here are three villas: "
            "Secret Beach Villa, Hidden Lagoon Estate, Phantom Palm Residence."
        ),
        cards=[],
        tool_results=[],
        applied_filters={},
        tenant_advisor_email="advisor@tajlandia.com",
    )
    assert ok is False
    assert any("invented_property_name" in r for r in reasons)
    # Honest fallback contains no invented names
    text = build_no_match_reply(language="en", site_origin="tajlandia.com", criteria_summary="injection")
    assert "Secret Beach Villa" not in text
    assert "system prompt" not in text.lower()


def test_tenant_advisor_emails_never_cross_contaminate():
    for origin, email, banned in [
        ("tajlandia.com", "advisor@tajlandia.com", "advisor@varsoviaestate.com"),
        ("thai.estate", "advisor@thai.estate", "advisor@varsoviaestate.com"),
    ]:
        t = resolve_tenant(origin)
        assert t.advisor_email == email
        text = build_no_match_reply(language="en", site_origin=origin)
        assert email in text
        assert banned not in text
        off = get_off_topic_text("en", site_origin=origin, user_message="who is the prime minister")
        assert banned not in off


def test_known_listing_details_path_returns_facts():
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = SimpleNamespace(
        id="c1",
        session_id="s1",
        site_origin="tajlandia.com",
        preferences={
            "last_result_ids": ["fantasea-condo"],
            "property_context": {"reference_map": {"Fantasea Condo": "fantasea-condo"}},
        },
        message_history=[],
    )
    with patch("app.services.public_agent.fetch_authorized_property", return_value=(FAKE, FAKE, "firestore")), \
         patch("app.services.public_agent.lookup_property_by_slug_or_name", return_value=FAKE), \
         patch("app.services.public_agent.extract_explicit_property_token", return_value="fantasea-condo"), \
         patch("app.services.public_agent.append_conversation_turn"), \
         patch("app.services.public_agent.token_tracker"), \
         patch("app.services.public_agent.record_or_update_lead"), \
         patch("app.services.public_agent.load_property_context") as mock_ctx, \
         patch("app.services.public_agent.save_property_context", return_value={}), \
         patch("app.services.public_agent.set_active_property", side_effect=lambda c, *a, **k: c), \
         patch("app.services.public_agent.update_rolling_summary", side_effect=lambda c, *a, **k: c), \
         patch("app.services.public_agent.resolve_name_against_context", return_value="fantasea-condo"):
        mock_ctx.return_value = MagicMock(
            active_property_id="fantasea-condo",
            recently_discussed_ids=["fantasea-condo"],
            reference_map={"Fantasea Condo": "fantasea-condo"},
        )
        result = agent._handle_property_details(
            db=MagicMock(),
            conversation=conversation,
            args={
                "property_id": "fantasea-condo",
                "property_name": "Fantasea Condo",
                "focus_question": "Tell me the price, bedrooms and ownership of Fantasea Condo",
            },
            user_message="Tell me the price, bedrooms and ownership of Fantasea Condo",
            start_time=0.0,
            language="en",
        )
    text = result["text"].lower()
    assert "fantasea" in text
    assert "9.50" in result["text"] or "price" in text
    assert "bedroom" in text
    assert "ownership" in text or "freehold" in text
    assert "foreign freehold / leasehold available" not in text
    # Must not dump a legal essay about foreign land ownership
    assert "section 86" not in text
    assert "thai land code" not in text
