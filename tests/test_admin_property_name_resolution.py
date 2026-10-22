"""
TASK 5 — Admin property name resolution before propose.
Exact, typo/fuzzy, ambiguous, not found, filler words. Never invent ids.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.auth import AuthenticatedStaff
from app.services.admin_property_resolver import (
    extract_property_name_phrase,
    resolve_admin_property_name,
    try_resolve_admin_pending_choice,
    store_admin_pending_resolution,
)
from app.services.property_name_index import (
    NameIndexEntry,
    NameMatchResult,
    PropertyNameIndex,
)
from app.services.admin_agent import _run_deterministic_admin


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
    )


def _index_with_pirak() -> PropertyNameIndex:
    idx = PropertyNameIndex()
    idx.load_from_rows(
        [
            {
                "doc_id": "pirak-villa-phuket",
                "name": {"en": "Pirak Villa"},
                "slug": {"en": "pirak-villa"},
                "cityId": "phuket",
            },
            {
                "doc_id": "pirak-bay-residences",
                "name": {"en": "Pirak Bay Residences"},
                "slug": {"en": "pirak-bay"},
                "cityId": "phuket",
            },
            {
                "doc_id": "anava-samui",
                "name": {"en": "Anava Samui"},
                "slug": {"en": "anava-samui"},
            },
            {
                "doc_id": "satoshi-paradise",
                "name": {"en": "Satoshi's Paradise"},
                "slug": {"en": "satoshi-paradise"},
            },
        ]
    )
    return idx


@pytest.fixture
def name_index(monkeypatch):
    idx = _index_with_pirak()
    monkeypatch.setattr(
        "app.services.property_name_index.get_property_name_index",
        lambda: idx,
    )
    monkeypatch.setattr("app.services.property_name_index._INDEX", idx)
    monkeypatch.setattr(
        "app.services.admin_property_resolver.ensure_name_index_from_firestore",
        lambda **k: None,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.ensure_name_index_from_firestore",
        lambda **k: None,
    )
    return idx


def test_extract_phrase_from_change_price_message():
    phrase = extract_property_name_phrase("change price of pirak villa to 5 million")
    assert phrase is not None
    assert "pirak" in phrase.lower()
    assert "5" not in phrase
    assert "million" not in phrase.lower()


def test_exact_match_resolves(name_index):
    res = resolve_admin_property_name("Pirak Villa", already_extracted=True)
    assert res.kind == "resolved"
    assert res.property_id == "pirak-villa-phuket"
    assert "Pirak Villa" in (res.official_name or "")


def test_typo_fuzzy_match_resolves(name_index):
    # slight typo should still hit Pirak Villa at high fuzzy threshold
    res = resolve_admin_property_name("Pirak Vlla", already_extracted=True)
    assert res.kind == "resolved"
    assert res.property_id == "pirak-villa-phuket"


def test_ambiguous_asks_which_and_proposes_nothing(name_index):
    # "Pirak" hits both Pirak Villa and Pirak Bay Residences
    res = resolve_admin_property_name("Pirak", already_extracted=True)
    assert res.kind in ("ambiguous", "resolved")
    # Brand-only often demoted to ambiguous with candidates
    if res.kind == "ambiguous":
        assert len(res.candidates) >= 2
        assert res.message and "Which one" in res.message
        assert all(c.get("property_id") for c in res.candidates)
    else:
        # If index is confident on one, still a real id — never invented
        assert res.property_id in ("pirak-villa-phuket", "pirak-bay-residences")


def test_not_found_suggests_closest(name_index):
    res = resolve_admin_property_name("Zorblax Towers XXX", already_extracted=True)
    assert res.kind == "not_found"
    assert res.property_id is None
    assert "couldn't find" in (res.message or "").lower() or "could not find" in (res.message or "").lower()
    # may include closest tips
    assert isinstance(res.suggestions, list)


def test_filler_words_never_property_names(name_index):
    for word in ("you", "do", "which", "private", "ok", "show", "more", "the", "this"):
        res = resolve_admin_property_name(word, already_extracted=True)
        assert res.kind in ("filler", "not_found", "empty"), f"{word} -> {res.kind}"
        assert res.property_id is None


def test_full_message_resolves_pirak_villa(name_index):
    res = resolve_admin_property_name("change price of pirak villa to 5 million")
    assert res.kind == "resolved"
    assert res.property_id == "pirak-villa-phuket"
    assert res.official_name


def test_pending_ordinal_choice():
    prefs = store_admin_pending_resolution(
        {},
        query="pirak",
        candidates=[
            {"property_id": "pirak-villa-phuket", "name": "Pirak Villa"},
            {"property_id": "pirak-bay-residences", "name": "Pirak Bay Residences"},
        ],
        write_intent={"fields": {"price_display": "5 million THB"}, "mode": "update"},
    )
    pick = try_resolve_admin_pending_choice("1", prefs)
    assert pick.kind == "resolved"
    assert pick.property_id == "pirak-villa-phuket"


def test_deterministic_update_echoes_official_name(name_index, staff, monkeypatch):
    conv = SimpleNamespace(
        id=uuid4(),
        session_id="s1",
        preferences={},
        message_history=[],
        site_origin="admin",
    )
    proposed = []

    def fake_exec(db, tool, args, staff_user, conv_id):
        proposed.append(args)
        return (
            {
                "status": "PROPOSED",
                "pending_action_id": "pa1",
                "diff_table": "| Field | Current | New |\n| price | 10M | 5M |",
                "message": "staged",
            },
            True,
            "pa1",
        )

    monkeypatch.setattr("app.services.admin_agent._execute_admin_tool", fake_exec)
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.is_off_topic_query", lambda m: False)

    out = _run_deterministic_admin(
        db=MagicMock(),
        conv=conv,
        message="change price of pirak villa to 5 million",
        staff_user=staff,
        language="en",
    )
    assert proposed, "must propose after resolve"
    assert proposed[0]["property_id"] == "pirak-villa-phuket"
    assert "PROP-NEW" not in proposed[0]["property_id"]
    text = out.get("text") or ""
    assert "Pirak Villa" in text
    assert "pirak-villa-phuket" in text
    assert out.get("pending_action_id") == "pa1"


def test_deterministic_ambiguous_proposes_nothing(name_index, staff, monkeypatch):
    conv = SimpleNamespace(
        id=uuid4(),
        session_id="s1",
        preferences={},
        message_history=[],
        site_origin="admin",
    )
    # Force ambiguous outcome
    monkeypatch.setattr(
        "app.services.admin_property_resolver.resolve_admin_property_name",
        lambda *a, **k: SimpleNamespace(
            kind="ambiguous",
            query="pirak",
            property_id=None,
            official_name=None,
            candidates=[
                {"property_id": "pirak-villa-phuket", "name": "Pirak Villa"},
                {"property_id": "pirak-bay-residences", "name": "Pirak Bay Residences"},
            ],
            suggestions=["Pirak Villa", "Pirak Bay Residences"],
            message="Which one?\n1) **Pirak Villa**\n2) **Pirak Bay Residences**",
            reason="ambiguous",
        ),
    )
    called = []
    monkeypatch.setattr(
        "app.services.admin_agent._execute_admin_tool",
        lambda *a, **k: called.append(1) or ({}, False, None),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.is_off_topic_query", lambda m: False)

    out = _run_deterministic_admin(
        db=MagicMock(),
        conv=conv,
        message="change price of pirak to 5 million",
        staff_user=staff,
        language="en",
    )
    assert not called
    assert out.get("pending_action_id") is None
    assert "Which one" in (out.get("text") or "")
    assert conv.preferences.get("admin_pending_name_candidates")


def test_deterministic_not_found_no_invented_id(name_index, staff, monkeypatch):
    conv = SimpleNamespace(
        id=uuid4(),
        session_id="s1",
        preferences={},
        message_history=[],
        site_origin="admin",
    )
    called = []
    monkeypatch.setattr(
        "app.services.admin_agent._execute_admin_tool",
        lambda *a, **k: called.append(1) or ({}, False, None),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.is_off_topic_query", lambda m: False)

    out = _run_deterministic_admin(
        db=MagicMock(),
        conv=conv,
        message="change price of Zorblax Fantasy Towers to 5 million",
        staff_user=staff,
        language="en",
    )
    assert not called
    assert out.get("pending_action_id") is None
    text = (out.get("text") or "").lower()
    assert "couldn't find" in text or "could not find" in text
    assert "prop-new" not in text
