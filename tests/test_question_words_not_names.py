"""
TASK 1 + 2: Question/generic words are never property names;
bedroom min/max/range filters ("4 or more", "4+", "between 3 and 5").
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.intent_router import (
    IntentType,
    detect_deterministic_intent,
    extract_search_filters_deterministic,
    is_valid_property_name_candidate,
)
from app.services.property_name_index import get_property_name_index, set_last_name_search_meta
from app.services.public_agent import PublicAgentService, extract_explicit_property_token
from app.services.firestore_service import listing_matches_bedroom_constraints

FIXTURE = Path(__file__).parent / "fixtures" / "listings_snapshot_2026-10-16.csv"


@pytest.fixture(scope="module")
def csv_rows():
    assert FIXTURE.is_file(), f"Missing fixture: {FIXTURE}"
    with FIXTURE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def name_index(csv_rows):
    idx = get_property_name_index()
    idx.clear()
    idx.load_from_rows(csv_rows)
    set_last_name_search_meta(None, [])
    yield idx
    set_last_name_search_meta(None, [])


# ---------------------------------------------------------------------------
# TASK 1 — question / generic words never property names
# ---------------------------------------------------------------------------

_SEARCHISH = [
    "Do you have anything with 4 or more bedrooms?",
    "do you have villas with 5 bedrooms",
    "Do you have a pool villa?",
    "Do you have 4+ bedroom villas under 30M",
    "Is there anything with 3 bedrooms?",
    "have you got anything with a sea view",
]


@pytest.mark.parametrize("msg", _SEARCHISH)
def test_do_you_have_filter_phrases_are_search_not_name(name_index, msg):
    intent, args = detect_deterministic_intent(msg, {})
    assert intent == IntentType.PROPERTY_SEARCH, f"{msg!r} -> {intent} {args}"
    assert intent != IntentType.PROPERTY_DETAILS
    name = str(args.get("property_name") or args.get("property_id") or "").lower()
    for banned in ("you", "there", "anything", "something", "got"):
        assert name != banned and name != f"there {banned}" and "there anything" not in name


@pytest.mark.parametrize(
    "word",
    ["you", "there", "anything", "something", "it", "this", "that", "any", "one"],
)
def test_stop_words_alone_are_never_valid_names(word):
    assert is_valid_property_name_candidate(word) is False


def test_does_it_have_gym_with_active_is_followup(name_index):
    state = {
        "last_result_ids": ["pirak-villa"],
        "property_context": {"active_property_id": "pirak-villa"},
        "selected_property_ids": ["pirak-villa"],
    }
    intent, args = detect_deterministic_intent("Does it have a gym?", state)
    assert intent == IntentType.PROPERTY_DETAILS
    assert str(args.get("property_id") or "").lower() in {"pirak-villa", "pirak villa"}
    assert str(args.get("property_name") or "").lower() not in {"it", "you", "there"}


def test_does_it_have_gym_without_active_not_name_not_found(name_index):
    intent, args = detect_deterministic_intent("Does it have a gym?", {})
    # Must not invent a property named "it"
    assert (args or {}).get("property_name", "").lower() not in {"it", "you"}
    if intent == IntentType.PROPERTY_DETAILS:
        assert args.get("from_property_context") or args.get("property_id") not in (None, "it")


def test_do_you_have_pirak_villa_is_details(name_index):
    intent, args = detect_deterministic_intent("Do you have Pirak Villa?", {})
    assert intent == IntentType.PROPERTY_DETAILS
    blob = " ".join(str(args.get(k) or "") for k in ("property_id", "property_name")).lower()
    assert "pirak" in blob


def test_do_you_have_above_element_is_details(name_index):
    intent, args = detect_deterministic_intent("do you have Above Element Villas?", {})
    assert intent == IntentType.PROPERTY_DETAILS
    blob = " ".join(str(args.get(k) or "") for k in ("property_id", "property_name")).lower()
    assert "above" in blob or "element" in blob


def test_do_you_have_zorblax_is_honest_not_found(name_index, monkeypatch):
    intent, args = detect_deterministic_intent("Do you have Zorblax Towers?", {})
    assert intent == IntentType.PROPERTY_DETAILS
    blob = " ".join(str(args.get(k) or "") for k in ("property_id", "property_name")).lower()
    assert "zorblax" in blob

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    conv = MagicMock()
    conv.preferences = {"language": "en"}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "tajlandia.com"
    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: None,
    )
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker", SimpleNamespace(log_turn=lambda *a, **k: None))
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)

    res = agent._handle_property_details(
        db=MagicMock(),
        conversation=conv,
        args=args,
        user_message="Do you have Zorblax Towers?",
        start_time=0.0,
        language="en",
    )
    low = res["text"].lower()
    assert "zorblax" in low or "not" in low or "couldn't find" in low or "could not" in low or "verified" in low
    assert '"you"' not in low and "'you'" not in low


@pytest.mark.parametrize(
    "msg",
    [
        "Do you have anything with 4 or more bedrooms?",
        "Is there anything with 3 bedrooms?",
        "have you got anything with a sea view",
    ],
)
def test_handle_details_never_name_not_found_for_stop_word_args(msg, monkeypatch):
    """Even if a stale path passes property_name=you, details must not name-not-found it."""
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    conv = MagicMock()
    conv.preferences = {"language": "en", "location": "Phuket"}
    conv.session_id = "s"
    conv.id = "c"
    conv.message_history = []
    conv.site_origin = "tajlandia.com"

    cards = [
        {
            "property_id": "v4",
            "name": "Four Bed Villa",
            "location": "Phuket",
            "price": "from 12M THB",
            "price_numeric_min": 12_000_000,
            "property_type": "Villa",
            "bedrooms": "4-5 Beds",
            "has_pool": True,
        }
    ]
    captured = {}

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        captured["params"] = params
        return list(cards)

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker", SimpleNamespace(log_turn=lambda *a, **k: None))
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)
    monkeypatch.setattr(
        "app.services.public_agent.lookup_property_by_slug_or_name",
        lambda tok: None,
    )

    # Simulate bad args that used to produce '"you" is not in our verified database'
    bad_args = {"property_name": "you", "property_id": "you", "focus_question": msg}
    # Route through chat intent first — must become search
    intent, good_args = detect_deterministic_intent(msg, {})
    assert intent == IntentType.PROPERTY_SEARCH

    res = agent._handle_property_search(
        db=MagicMock(),
        conversation=conv,
        user_message=msg,
        start_time=0.0,
        language="en",
    )
    low = res["text"].lower()
    assert '"you"' not in low and "'you'" not in low
    assert "there anything" not in low
    assert "not in our verified database" not in low or "four bed" in low or "villa" in low


def test_extract_explicit_token_rejects_stop_words():
    assert extract_explicit_property_token("Do you have anything?", {"property_name": "you"}) is None
    assert extract_explicit_property_token("Is there anything?", {"property_id": "there anything"}) is None
    tok = extract_explicit_property_token(
        "Do you have Pirak Villa?",
        {"property_name": "Pirak Villa", "explicit_property_token": "Pirak Villa"},
    )
    assert tok and "pirak" in tok.lower()


# ---------------------------------------------------------------------------
# TASK 2 — bedroom min / max / range
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg,expect",
    [
        ("4 or more bedrooms", {"min_bedrooms": 4}),
        ("4+ bedrooms", {"min_bedrooms": 4}),
        ("4 bedrooms or more", {"min_bedrooms": 4}),
        ("at least 4 beds", {"min_bedrooms": 4}),
        ("minimum 4 bedrooms", {"min_bedrooms": 4}),
        ("more than 3 bedrooms", {"min_bedrooms": 4}),
        ("over 3 bedrooms", {"min_bedrooms": 4}),
        ("3 or fewer", {"max_bedrooms": 3}),
        ("up to 3 bedrooms", {"max_bedrooms": 3}),
        ("at most 3 bedrooms", {"max_bedrooms": 3}),
        ("between 3 and 5 bedrooms", {"min_bedrooms": 3, "max_bedrooms": 5}),
        ("3-5 bedrooms", {"min_bedrooms": 3, "max_bedrooms": 5}),
        ("5-6 beds", {"min_bedrooms": 5, "max_bedrooms": 6}),
        ("co najmniej 4 sypialnie", {"min_bedrooms": 4}),
        ("4 lub więcej sypialni", {"min_bedrooms": 4}),
        ("od 3 do 5 sypialni", {"min_bedrooms": 3, "max_bedrooms": 5}),
        ("4 ห้องนอนขึ้นไป", {"min_bedrooms": 4}),
        ("อย่างน้อย 4 ห้องนอน", {"min_bedrooms": 4}),
        ("3 bedroom villa", {"bedrooms": 3}),
        ("Do you have anything with 4 or more bedrooms?", {"min_bedrooms": 4}),
        ("Do you have 4+ bedroom villas under 30M", {"min_bedrooms": 4, "max_price": 30_000_000.0}),
    ],
)
def test_bedroom_range_phrases_extract_filters(msg, expect):
    f = extract_search_filters_deterministic(msg)
    for k, v in expect.items():
        assert f.get(k) == v, f"{msg!r}: expected {k}={v}, got {f}"


@pytest.mark.parametrize(
    "beds,min_b,max_b,exact,ok",
    [
        ("4-5 Beds", 4, None, None, True),
        ("3-5 Beds", 4, None, None, True),  # largest >= 4
        ("2-3 Beds", 4, None, None, False),
        ("", 4, None, None, False),
        ("2-3 Beds", None, 3, None, True),
        ("4-5 Beds", None, 3, None, False),
        ("2-4 Beds", 3, 5, None, True),  # overlap
        ("1-2 Beds", 3, 5, None, False),
        ("3 Beds", None, None, 3, True),
        ("2-4 Beds", None, None, 3, True),
        ("5 Beds", None, None, 3, False),
    ],
)
def test_listing_matches_bedroom_constraints(beds, min_b, max_b, exact, ok):
    assert (
        listing_matches_bedroom_constraints(
            beds, bedrooms=exact, min_bedrooms=min_b, max_bedrooms=max_b
        )
        is ok
    )


def test_four_or_more_search_returns_only_matching_cards(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    conv = SimpleNamespace(
        preferences={"language": "en"},
        session_id="s",
        id="c",
        message_history=[],
        site_origin="tajlandia.com",
    )

    catalogue = [
        {"property_id": "lo", "name": "Low Beds", "location": "Phuket", "price": "8M",
         "price_numeric_min": 8_000_000, "property_type": "Villa", "bedrooms": "2-3 Beds"},
        {"property_id": "mid", "name": "Mid Beds", "location": "Phuket", "price": "12M",
         "price_numeric_min": 12_000_000, "property_type": "Villa", "bedrooms": "3-5 Beds"},
        {"property_id": "hi", "name": "High Beds", "location": "Phuket", "price": "18M",
         "price_numeric_min": 18_000_000, "property_type": "Villa", "bedrooms": "5 Beds"},
        {"property_id": "unk", "name": "Unknown Beds", "location": "Phuket", "price": "10M",
         "price_numeric_min": 10_000_000, "property_type": "Villa", "bedrooms": ""},
    ]
    captured = {}

    def fake_unified(db, params, limit=None, exclude_property_ids=None):
        captured["params"] = params
        excl = {str(x).lower() for x in (exclude_property_ids or [])}
        out = []
        for c in catalogue:
            if str(c["property_id"]).lower() in excl:
                continue
            if not listing_matches_bedroom_constraints(
                c.get("bedrooms"),
                bedrooms=getattr(params, "bedrooms", None),
                min_bedrooms=getattr(params, "min_bedrooms", None),
                max_bedrooms=getattr(params, "max_bedrooms", None),
            ):
                continue
            out.append(dict(c))
        return out[: (limit or 6)]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_unified)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker", SimpleNamespace(log_turn=lambda *a, **k: None))
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)

    msg = "Do you have anything with 4 or more bedrooms?"
    intent, args = detect_deterministic_intent(msg, {})
    assert intent == IntentType.PROPERTY_SEARCH
    assert args.get("min_bedrooms") == 4

    res = agent._handle_property_search(
        db=MagicMock(), conversation=conv, user_message=msg, start_time=0.0, language="en",
    )
    ids = [c["property_id"] for c in (res.get("properties") or [])]
    assert "lo" not in ids and "unk" not in ids
    assert set(ids) <= {"mid", "hi"}
    assert ids
    low = res["text"].lower()
    assert "4" in low or "bedroom" in low
    assert '"you"' not in low
    # Stored filters keep min_bedrooms
    assert getattr(captured["params"], "min_bedrooms", None) == 4
    stored = (conv.preferences or {}).get("active_filters") or {}
    assert stored.get("min_bedrooms") == 4 or conv.preferences.get("min_bedrooms") == 4


def test_impossible_bedroom_min_honest_no_match(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    conv = SimpleNamespace(
        preferences={"language": "en"},
        session_id="s",
        id="c",
        message_history=[],
        site_origin="tajlandia.com",
    )

    monkeypatch.setattr(
        "app.services.public_agent.search_properties_unified",
        lambda *a, **k: [],
    )
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker", SimpleNamespace(log_turn=lambda *a, **k: None))
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)

    msg = "20 or more bedrooms"
    intent, args = detect_deterministic_intent(msg, {})
    assert args.get("min_bedrooms") == 20
    res = agent._handle_property_search(
        db=MagicMock(), conversation=conv, user_message=msg, start_time=0.0, language="en",
    )
    assert not res.get("properties")
    low = res["text"].lower()
    assert "no" in low or "not" in low or "couldn't" in low or "could not" in low or "none" in low


def test_four_or_more_then_under_30m_keeps_both(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    state = {
        "active_filters": {"min_bedrooms": 4, "location": "Phuket"},
        "location": "Phuket",
        "min_bedrooms": 4,
        "last_result_ids": ["seen-a"],
        "seen_property_ids": ["seen-a"],
        "pagination_offset": 4,
        "limit": 4,
    }
    conv = SimpleNamespace(
        preferences=dict(state),
        session_id="s",
        id="c",
        message_history=[],
        site_origin="tajlandia.com",
    )

    captured = {}

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        captured["params"] = params
        captured["exclude"] = list(exclude_property_ids or [])
        return [
            {
                "property_id": "new-4",
                "name": "Kept Filters Villa",
                "location": "Phuket",
                "price": "25M",
                "price_numeric_min": 25_000_000,
                "property_type": "Villa",
                "bedrooms": "4 Beds",
            }
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker", SimpleNamespace(log_turn=lambda *a, **k: None))
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)

    intent, args = detect_deterministic_intent("show me other under 30M", state)
    assert intent == IntentType.SHOW_MORE
    overrides = args.get("filter_overrides") or {}
    assert float(overrides.get("max_price") or 0) == 30_000_000.0
    agent._handle_show_more(
        db=MagicMock(), conversation=conv, args=args, user_message="show me other under 30M",
        start_time=0.0, language="en",
    )
    params = captured.get("params")
    assert params is not None
    assert float(params.max_price or 0) == 30_000_000.0
    # min bedrooms preserved from stored filters (budget override alone)
    af = conv.preferences.get("active_filters") or {}
    assert af.get("min_bedrooms") == 4 or getattr(params, "min_bedrooms", None) == 4
    # Filter change starts a fresh page (exclude cleared); pure "show me other" excludes seen.


def test_show_other_excludes_seen_with_min_bedrooms(monkeypatch):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None
    state = {
        "active_filters": {"min_bedrooms": 4},
        "min_bedrooms": 4,
        "last_result_ids": ["seen-1"],
        "seen_property_ids": ["seen-1"],
        "pagination_offset": 0,
        "limit": 4,
    }
    conv = SimpleNamespace(
        preferences=dict(state),
        session_id="s",
        id="c",
        message_history=[],
        site_origin="tajlandia.com",
    )
    captured = {}

    def fake_search(db, params, limit=None, exclude_property_ids=None):
        captured["exclude"] = list(exclude_property_ids or [])
        return [
            {
                "property_id": "fresh-4",
                "name": "Fresh 4 Bed",
                "location": "Phuket",
                "price": "15M",
                "price_numeric_min": 15_000_000,
                "bedrooms": "4 Beds",
                "property_type": "Villa",
            }
        ]

    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_search)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **k: None)
    monkeypatch.setattr("app.services.public_agent.token_tracker", SimpleNamespace(log_turn=lambda *a, **k: None))
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", lambda **k: None)
    monkeypatch.setattr(agent, "_generate_quick_replies", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_validate_or_fallback", lambda text, cards, **kw: (text, cards or []))
    monkeypatch.setattr(agent, "_post_process_response", lambda resp, **kw: resp)

    intent, args = detect_deterministic_intent("show me other", state)
    assert intent == IntentType.SHOW_MORE
    agent._handle_show_more(
        db=MagicMock(), conversation=conv, args=args, user_message="show me other",
        start_time=0.0, language="en",
    )
    assert "seen-1" in captured.get("exclude", [])
