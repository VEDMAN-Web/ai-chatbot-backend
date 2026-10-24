"""Show-more must stay on topic, and listing text must not repeat itself."""
from unittest.mock import MagicMock

import pytest

import app.services.public_agent as public_agent_mod
from app.services.conversation_controls import format_budget_phrase
from app.services.i18n_service import bedroom_fit_label, build_rich_property_recommendation_text
from app.services.intent_router import IntentType, detect_deterministic_intent, message_requests_show_more
from app.services.public_agent import (
    PublicAgentService,
    _dedupe_reply_blocks,
    extract_user_preferences,
)


SHOW_MORE_PHRASES = [
    "show me more",
    "show me more properties",
    "no show me more",
    "no, show me more",
    "okay show me more",
    "please show more",
    "can you show me more",
    "more please",
    "show me some more",
    "give me more options",
    "show me the next ones",
    "next ones",
    "more results",
    "see more",
    "aur dikhao",
    "biju batavo",
    "vadhu batavo",
    "więcej",
    "ดูเพิ่มเติม",
]


@pytest.mark.parametrize("phrase", SHOW_MORE_PHRASES)
def test_show_more_phrases_are_deterministic(phrase):
    assert message_requests_show_more(phrase) is True
    intent, _args = detect_deterministic_intent(phrase, stored_state={"last_result_ids": ["a"]})
    assert intent == IntentType.SHOW_MORE


def test_short_followup_after_listings_is_show_more_and_politics_is_not():
    state = {"last_result_ids": ["villa-1"], "seen_property_ids": ["villa-1"]}
    intent, _args = detect_deterministic_intent("more again", stored_state=state)
    assert intent == IntentType.SHOW_MORE
    politics, _args = detect_deterministic_intent("who is the prime minister", stored_state=state)
    assert politics != IntentType.SHOW_MORE
    assert message_requests_show_more("who is the prime minister", listings_were_shown=True) is False


def test_budget_phrase_and_focus_line_use_one_wording():
    assert format_budget_phrase(50_000_000, exclusive=True) == "under 50M THB (about $1.43M)"
    assert format_budget_phrase(50_000_000, exclusive=False).startswith("up to 50M THB")
    prefs = extract_user_preferences(
        "i want under 50million and wants 3 bedroom in Phuket",
        stored_preferences={},
    )
    assert prefs["budget_display"] == "under 50M THB (about $1.43M)"
    assert "Up to 50,000,000" not in prefs["budget_display"]
    assert prefs["focus_summary"].startswith("Searching:")
    assert "Phuket" in prefs["focus_summary"]
    assert "under 50M THB" in prefs["focus_summary"]
    assert "budget" in prefs["answered_slots"]
    assert "bedrooms" in prefs["answered_slots"]
    assert "location" in prefs["answered_slots"]


def test_bedroom_label_and_narrowing_block_once():
    assert bedroom_fit_label("1-3", 3) == "1-3 beds (3-bed units available)"
    assert bedroom_fit_label("3 Beds", 3) == "3 Beds"
    assert bedroom_fit_label("Beds: on request", 3) == "Beds: on request"
    text = build_rich_property_recommendation_text(
        cards=[{"name": "Palm Villa", "location": "Phuket", "price": "12M THB", "bedrooms": "1-3"}],
        language="en",
        is_sparse=True,
        location=None,
        budget=None,
        bedrooms=3,
        site_origin="tajlandia.com",
    )
    assert "\n\nTo narrow this down to the best fit, may I ask:\n" in text
    assert text.count("To narrow this down to the best fit, may I ask:") == 1
    assert "1-3 beds (3-bed units available)" in text
    repeated = text + "\n\n" + "To narrow this down to the best fit, may I ask:\n• How many bedrooms?"
    cleaned = _dedupe_reply_blocks(repeated)
    assert cleaned.count("To narrow this down to the best fit, may I ask:") == 1


def test_quick_replies_are_deduped():
    agent = PublicAgentService.__new__(PublicAgentService)
    out = agent._post_process_response({
        "text": "Hello",
        "language": "en",
        "quick_replies": [
            "Explore luxury properties in Thailand",
            "explore luxury properties in thailand",
            "How does buying property in Thailand work?",
        ],
        "properties": [],
    })
    assert out["quick_replies"] == [
        "Explore luxury properties in Thailand",
        "How does buying property in Thailand work?",
    ]


def test_card_label_helper_is_used_once_in_the_frontend():
    script = open("app/static/js/app.js", encoding="utf-8").read()
    assert "function bedLabel(" in script
    assert "${p.bedrooms} Beds" not in script
    assert "Matching properties" in script
    assert "AI Focus:" not in script


def _agent(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = MagicMock()
    agent.api_key = "sk-test"
    monkeypatch.setattr(public_agent_mod, "append_conversation_turn", lambda **_k: None)
    monkeypatch.setattr(public_agent_mod.token_tracker, "log_turn", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: ["Explore luxury properties in Thailand"])
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)
    monkeypatch.setattr(agent, "_maybe_resolve_pending_name_candidates", lambda *a, **k: None)
    return agent


def _conv():
    conv = MagicMock()
    conv.preferences = {}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "tajlandia.com"
    conv.agent_type = "public"
    return conv


def test_replay_show_me_more_stays_on_topic(monkeypatch):
    agent = _agent(monkeypatch)
    catalogue = [
        {"property_id": "p1", "name": "One", "price": "10M THB", "price_thb_min": 10_000_000, "location": "Phuket", "bedrooms": "3", "property_type": "Villa"},
        {"property_id": "p2", "name": "Two", "price": "20M THB", "price_thb_min": 20_000_000, "location": "Phuket", "bedrooms": "3", "property_type": "Villa"},
        {"property_id": "p3", "name": "Three", "price": "30M THB", "price_thb_min": 30_000_000, "location": "Phuket", "bedrooms": "1-3", "property_type": "Villa"},
        {"property_id": "s1", "name": "Samui House", "price": "15M THB", "price_thb_min": 15_000_000, "location": "Koh Samui", "bedrooms": "3", "property_type": "Villa"},
    ]

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        loc = (getattr(params, "location", None) or "").lower()
        excluded = {str(i).lower() for i in (exclude_property_ids or [])}
        rows = []
        for row in catalogue:
            if row["property_id"].lower() in excluded:
                continue
            if loc and loc not in row["location"].lower():
                continue
            max_price = getattr(params, "max_price", None)
            if max_price is not None and row["price_thb_min"] > float(max_price):
                continue
            rows.append(dict(row))
        rows.sort(key=lambda r: r["price_thb_min"])
        return rows[: limit or 4]

    monkeypatch.setattr(public_agent_mod, "search_properties_unified", fake_search)
    conv = _conv()
    off_topic_marker = "off-topic questions"

    hello = agent.run_conversation(db=MagicMock(), conversation=conv, user_message="hello", requested_language="en")
    assert hello.get("text")
    assert len({q.lower() for q in hello.get("quick_replies") or []}) == len(hello.get("quick_replies") or [])

    agent.run_conversation(
        db=MagicMock(), conversation=conv,
        user_message="Explore luxury properties in Thailand", requested_language="en",
    )
    agent.run_conversation(
        db=MagicMock(), conversation=conv,
        user_message="i want under 50million any under 50million and wants 3 bedroom",
        requested_language="en",
    )
    phuket = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="i want in Phuket", requested_language="en",
    )
    assert off_topic_marker not in (phuket.get("text") or "")
    conv.message_history = [
        {"role": "user", "content": "i want in Phuket"},
        {"role": "assistant", "content": phuket.get("text") or "Phuket villa 20M THB 3 bedrooms"},
    ]

    more_1 = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="no show me more", requested_language="en",
    )
    more_2 = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="show me more properties", requested_language="en",
    )
    for turn in (more_1, more_2):
        assert off_topic_marker not in (turn.get("text") or "")
        has_listings = bool(turn.get("properties"))
        honest_end = "That is all I have" in (turn.get("text") or "")
        assert has_listings or honest_end

    samui = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="okay any in Koh Samui ?", requested_language="en",
    )
    assert off_topic_marker not in (samui.get("text") or "")
    assert any("samui" in str(p.get("location", "")).lower() for p in (samui.get("properties") or [])) or "Samui" in (samui.get("text") or "")

    conv.message_history.append({"role": "assistant", "content": "Phuket villa 20M THB"})
    politics = agent.run_conversation(
        db=MagicMock(), conversation=conv, user_message="who is the prime minister", requested_language="en",
    )
    assert off_topic_marker in (politics.get("text") or "")
    assert politics.get("properties") == []
