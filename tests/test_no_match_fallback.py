"""Step 1: zero-result searches must not invent cards via unfiltered fallback."""
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from app.services.i18n_service import build_no_match_reply


FAKE_LISTINGS = [
    {
        "property_id": "villa-a",
        "name": "Villa A",
        "location": "Phuket",
        "price": "from 20M THB",
        "price_numeric_min": 20_000_000,
        "property_type": "Villa",
        "bedrooms": 3,
    },
    {
        "property_id": "condo-b",
        "name": "Condo B",
        "location": "Bangkok",
        "price": "from 8M THB",
        "price_numeric_min": 8_000_000,
        "property_type": "Condo",
        "bedrooms": 2,
    },
]


def test_build_no_match_reply_en_no_alternatives():
    text = build_no_match_reply(
        language="en",
        site_origin="tajlandia.com",
        criteria_summary="villas under 5M THB in Layan",
    )
    assert "No verified listing matches" in text
    assert "villas under 5M THB in Layan" in text
    assert "advisor@tajlandia.com" in text
    assert "advisor@varsoviaestate.com" not in text
    assert "\u2014" not in text  # no em-dash


def test_build_no_match_reply_with_one_relaxed_filter():
    text = build_no_match_reply(
        language="en",
        site_origin="thai.estate",
        criteria_summary="Layan, Villa, budget <= 15000000",
        relaxed_filter="location -> Phuket",
        alternatives=FAKE_LISTINGS[:1],
    )
    assert "relaxing exactly one filter" in text.lower() or "location -> Phuket" in text
    assert "Villa A" in text
    assert "advisor@thai.estate" in text
    assert "Condo B" not in text


def test_show_more_zero_results_does_not_call_empty_filter_search():
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = SimpleNamespace(
        id="c1",
        session_id="s1",
        site_origin="tajlandia.com",
        preferences={"location": "Layan", "property_type": "Villa", "budget_max": 5_000_000},
        message_history=[],
    )
    db = MagicMock()
    calls = []

    def fake_search(db_arg, params, limit=4, exclude_property_ids=None):
        calls.append(params)
        return []

    with patch("app.services.public_agent.search_properties_unified", side_effect=fake_search), \
         patch("app.services.public_agent.get_session_seen_property_ids", return_value=[]), \
         patch("app.services.public_agent.append_conversation_turn"), \
         patch("app.services.public_agent.token_tracker"):
        result = agent._handle_show_more(
            db=db,
            conversation=conversation,
            args={"offset": 4, "limit": 4},
            user_message="show more",
            start_time=0.0,
            language="en",
        )

    assert result["properties"] == []
    assert "No verified listing matches" in result["text"]
    assert "advisor@tajlandia.com" in result["text"]
    assert "advisor@varsoviaestate.com" not in result["text"]
    # Every search must have kept at least one real filter (never fully empty)
    for p in calls:
        has_filter = any(
            getattr(p, attr, None) is not None
            for attr in ("location", "property_type", "bedrooms", "max_price", "has_pool", "min_price")
        )
        assert has_filter, f"empty-filter search was called: {p}"
