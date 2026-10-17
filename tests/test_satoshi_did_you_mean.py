"""
TASK 1: Satoshi Residence → Did you mean Satoshi's Paradise; yes resolves.
Reproduces lost candidates when set_last_name_search_meta gets suggestions-only.
"""

from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.i18n_service import build_name_not_found_reply
from app.services.name_confirmation import (
    PREF_CANDIDATES,
    store_pending_name_candidates,
    try_resolve_pending_name,
)
from app.services.property_name_index import (
    PropertyNameIndex,
    get_last_name_search_candidates,
    get_last_name_search_suggestions,
    match_property_name,
    set_last_name_search_meta,
)


def _build_index() -> PropertyNameIndex:
    idx = PropertyNameIndex()
    idx.load_from_rows(
        [
            {
                "doc_id": "satoshis-paradise",
                "name": {"en": "Satoshi's Paradise", "pl": "Satoshi's Paradise"},
                "slug": {"en": "satoshis-paradise"},
                "cityId": "phuket",
            },
            {
                "doc_id": "anava-samui",
                "name": {"en": "Anava Samui", "pl": "Anava Samui"},
                "slug": {"en": "anava-samui"},
                "cityId": "samui",
            },
            {
                "doc_id": "mirage-villas",
                "name": {"en": "Mirage Villas"},
                "slug": {"en": "mirage-villas"},
            },
        ]
    )
    return idx


@pytest.fixture
def name_index(monkeypatch):
    idx = _build_index()
    monkeypatch.setattr(
        "app.services.property_name_index.get_property_name_index",
        lambda: idx,
    )
    monkeypatch.setattr("app.services.property_name_index._INDEX", idx)
    return idx


def test_suggestions_only_call_must_not_drop_candidates(name_index):
    """Reproduce staging bug: set_last_name_search_meta(query, [names]) wiped candidates."""
    mr = name_index.match("Satoshi Residence")
    assert mr.confident is None
    assert mr.candidates
    # Old firestore call style — suggestions positional, no candidates=
    set_last_name_search_meta(
        "Satoshi Residence",
        [c.display_name for c in mr.candidates[:3]],
    )
    tips = get_last_name_search_suggestions()
    cands = get_last_name_search_candidates()
    assert any("Paradise" in t for t in tips)
    # THIS is the bug: candidates were empty so pending/"yes" could not work
    assert cands, "candidates must include property_id + name for confirmation"
    assert any(c.get("property_id") == "satoshis-paradise" for c in cands)


def test_firestore_search_stores_candidates_for_satoshi(monkeypatch, name_index):
    from app.services import firestore_service as fs

    monkeypatch.setattr(
        "app.services.property_name_index.ensure_name_index_from_firestore",
        lambda **k: None,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.match_property_name",
        name_index.match,
    )
    monkeypatch.setattr(fs, "get_property_by_id_from_firestore", lambda *a, **k: None)

    out = fs.search_properties_firestore(query="Satoshi Residence", limit=4)
    assert out == []
    tips = get_last_name_search_suggestions()
    cands = get_last_name_search_candidates()
    assert any("Paradise" in t for t in tips)
    assert cands and any(c["property_id"] == "satoshis-paradise" for c in cands)


def test_execute_tool_stores_pending_and_message(monkeypatch, name_index):
    from app.services.public_agent import PublicAgentService

    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    conversation = MagicMock()
    conversation.preferences = {"language": "en"}
    conversation.session_id = "s1"
    conversation.id = "c1"
    conversation.site_origin = "tajlandia.com"
    conversation.message_history = []

    def fake_unified(db, params, limit=None, exclude_property_ids=None):
        # Named query miss path: empty cards; meta must already be set by search layer
        from app.services.firestore_service import search_properties_firestore
        return search_properties_firestore(query=params.query, limit=limit or 4)

    monkeypatch.setattr(
        "app.services.property_name_index.ensure_name_index_from_firestore",
        lambda **k: None,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.match_property_name",
        name_index.match,
    )
    monkeypatch.setattr(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("app.services.public_agent.search_properties_unified", fake_unified)
    monkeypatch.setattr(
        "app.services.public_agent.record_or_update_lead",
        lambda **k: None,
    )

    summary, cards, _ = agent.execute_tool(
        db=MagicMock(),
        conversation=conversation,
        tool_name="search_properties",
        args={"query": "Satoshi Residence"},
    )
    assert cards == []
    assert summary.get("name_suggestions")
    assert any("Paradise" in s for s in summary["name_suggestions"])
    assert summary.get("name_candidates")
    assert any(c["property_id"] == "satoshis-paradise" for c in summary["name_candidates"])
    assert "Paradise" in (summary.get("message") or "")
    prefs = conversation.preferences or {}
    pending = prefs.get(PREF_CANDIDATES) or []
    assert pending and pending[0]["property_id"] == "satoshis-paradise"


def test_yes_resolves_to_paradise_after_pending():
    prefs = store_pending_name_candidates(
        {},
        query="Satoshi Residence",
        candidates=[{"property_id": "satoshis-paradise", "name": "Satoshi's Paradise"}],
    )
    outcome = try_resolve_pending_name("yes", prefs, language="en")
    assert outcome.kind == "resolved"
    assert outcome.property_id == "satoshis-paradise"


def test_zorblax_no_suggestions(name_index):
    mr = name_index.match("Zorblax Towers")
    assert mr.confident is None
    set_last_name_search_meta("Zorblax Towers", candidates=mr.candidates or [])
    assert get_last_name_search_suggestions() == [] or not any(
        "Paradise" in t for t in get_last_name_search_suggestions()
    )


def test_anava_variants_still_confident(name_index):
    for q in ("Anava", "mi Anava", "pokaż mi Anava"):
        mr = name_index.match(q)
        assert mr.confident is not None, q
        assert mr.confident.doc_id == "anava-samui"


def test_cross_thread_tool_result_carries_suggestions(name_index):
    """If search runs in another thread, tool_summary must still carry tips+cands."""

    def search_in_thread(query: str):
        mr = name_index.match(query)
        # Simulate fixed firestore meta write inside the worker thread
        set_last_name_search_meta(
            query,
            suggestions=[c.display_name for c in (mr.candidates or [])[:3]],
            candidates=(mr.candidates or [])[:3],
        )
        return {
            "name_suggestions": get_last_name_search_suggestions(),
            "name_candidates": get_last_name_search_candidates(),
            "message": build_name_not_found_reply(
                language="en",
                site_origin="tajlandia.com",
                asked_name=query,
                suggestions=get_last_name_search_suggestions(),
            ),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        tool_summary = ex.submit(search_in_thread, "Satoshi Residence").result(timeout=10)

    # Main thread ContextVar may be empty — reply must use tool_summary
    assert any("Paradise" in s for s in tool_summary["name_suggestions"])
    assert tool_summary["name_candidates"]
    assert "Paradise" in tool_summary["message"]
    # Builder on main thread uses tool_summary, not ContextVar
    final = tool_summary["message"]
    assert "Satoshi" in final and "Paradise" in final
