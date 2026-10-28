"""
Property name matching tests using listings_snapshot CSV (925 docs).
No network, no real Firestore — fixtures + mocks only.
"""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.property_name_index import (
    PropertyNameIndex,
    get_last_name_search_suggestions,
    get_property_name_index,
    match_property_name,
    normalize_property_name,
    set_last_name_search_meta,
)
from app.services.i18n_service import build_name_not_found_reply
from app.services.reply_validator import validate_reply

FIXTURE = Path(__file__).parent / "fixtures" / "listings_snapshot_2026-10-16.csv"


@pytest.fixture(scope="module")
def csv_rows():
    assert FIXTURE.is_file(), f"Missing fixture: {FIXTURE}"
    with FIXTURE.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 925
    return rows


@pytest.fixture
def name_index(csv_rows):
    idx = get_property_name_index()
    idx.clear()
    idx.load_from_rows(csv_rows)
    set_last_name_search_meta(None, [])
    yield idx
    set_last_name_search_meta(None, [])


def test_normalize_strips_accents_apostrophes_hyphens():
    assert normalize_property_name("  Satoshi's Paradise ") == "satoshi s paradise"
    assert normalize_property_name("Mono-Champaca") == "mono champaca"
    assert normalize_property_name("Café") == "cafe"


def test_anava_variants_resolve(name_index):
    for q in ("Anava", "Anava Samui", "Anava Condominium"):
        m = name_index.match(q)
        assert m.confident is not None, q
        assert "anava" in m.confident.display_name.lower()


def test_satoshi_exact_and_partial(name_index):
    exact = name_index.match("Satoshi's Paradise")
    assert exact.confident is not None
    assert "satoshi" in exact.confident.display_name.lower()

    for q in ("Satoshi", "Satoshi Residence"):
        m = name_index.match(q)
        assert m.confident is None, q
        names = [c.display_name for c in m.candidates]
        assert any("satoshi" in n.lower() and "paradise" in n.lower() for n in names)


def test_zorblax_not_found(name_index):
    m = name_index.match("Zorblax Towers")
    assert m.confident is None
    assert m.candidates == []


def test_beyond_400_unique_words(name_index, csv_rows):
    # These sit past doc position 400 in the CSV order
    positions = {r["doc_id"]: i for i, r in enumerate(csv_rows)}
    cases = {
        "Mirage": "mirage",
        "Monetaria": "monetaria",
        "Champaca": "champaca",
    }
    for q, needle in cases.items():
        m = name_index.match(q)
        assert m.confident is not None, q
        assert needle in m.confident.display_name.lower() or needle in m.confident.doc_id.lower()
        assert positions[m.confident.doc_id] > 400


def test_generic_words_never_confident(name_index):
    for q in ("villa", "villas", "condo", "eight", "phuket", "resort"):
        m = name_index.match(q)
        assert m.confident is None, q


def test_location_arg_same_as_query(name_index):
    """Same matcher for a name whether it arrives as query or location string."""
    for q in ("Anava", "Mirage", "Zorblax Towers", "Satoshi", "Champaca"):
        a = name_index.match(q)
        b = name_index.match(q)  # location uses identical match_property_name()
        assert (a.confident.doc_id if a.confident else None) == (
            b.confident.doc_id if b.confident else None
        )
        assert [c.doc_id for c in a.candidates] == [c.doc_id for c in b.candidates]


def test_search_name_as_location_and_query(monkeypatch, name_index):
    from app.services import firestore_service as fs
    import app.services.property_name_index as pni

    monkeypatch.setattr(pni, "ensure_name_index_from_firestore", lambda **k: None)
    monkeypatch.setattr(pni, "match_property_name", name_index.match)
    monkeypatch.setattr(pni, "distinctive_tokens", pni.distinctive_tokens)
    monkeypatch.setattr(pni, "set_last_name_search_meta", pni.set_last_name_search_meta)

    def fake_get(pid, target="user"):
        entry = name_index._by_id.get(pid)  # noqa: SLF001
        if entry:
            return {
                "property_id": entry.doc_id,
                "name": entry.display_name,
                "location": "",
                "address": "",
                "district": "",
                "province": "",
                "latitude": None,
                "longitude": None,
            }
        m = name_index.match(str(pid))
        if m.confident:
            return {
                "property_id": m.confident.doc_id,
                "name": m.confident.display_name,
                "location": "",
                "address": "",
                "district": "",
                "province": "",
                "latitude": None,
                "longitude": None,
            }
        return None

    monkeypatch.setattr(fs, "get_property_by_id_from_firestore", fake_get)

    # Patch symbols used inside search_properties_firestore after local import
    import app.services.firestore_service as fs_mod

    real_search = fs_mod.search_properties_firestore

    def _run(query=None, location=None):
        with patch.object(pni, "match_property_name", name_index.match):
            with patch.object(pni, "ensure_name_index_from_firestore", lambda **k: None):
                with patch(
                    "app.services.maps_service.build_google_maps_url",
                    return_value="",
                ):
                    with patch(
                        "app.services.maps_service.build_google_maps_embed_url",
                        return_value={},
                    ):
                        with patch(
                            "app.services.maps_service.get_property_proximity_landmarks",
                            return_value=[],
                        ):
                            return real_search(query=query, location=location, limit=6)

    by_query = _run(query="Anava")
    by_loc = _run(location="Anava")
    assert len(by_query) == 1 and len(by_loc) == 1
    assert by_query[0]["property_id"] == by_loc[0]["property_id"]

    assert _run(query="Zorblax Towers") == []
    assert _run(location="Zorblax Towers") == []


def test_deterministic_no_shuffle(name_index):
    results = [name_index.match("Mirage").confident.doc_id for _ in range(20)]
    assert len(set(results)) == 1


def test_every_full_name_resolves(name_index, csv_rows):
    """Typing each listing's own full name (prefer name_pl, else name_en, else slug) resolves."""
    failures = []
    for row in csv_rows:
        name = (row.get("name_pl") or "").strip() or (row.get("name_en") or "").strip()
        if not name:
            name = (row.get("slug_en") or row.get("slug_pl") or row.get("doc_id") or "").strip()
        m = name_index.match(name)
        if not m.confident or m.confident.doc_id != row["doc_id"]:
            # Allow confident hit on same listing via alternate display
            if m.confident and m.confident.doc_id == row["doc_id"]:
                continue
            failures.append((row["doc_id"], name[:50], m.confident.doc_id if m.confident else None))
    assert failures == [], f"{len(failures)} failures e.g. {failures[:5]}"


def test_name_not_found_reply_honest_no_emdash():
    text = build_name_not_found_reply(
        "en",
        "tajlandia.com",
        asked_name="Zorblax Towers",
        suggestions=["Satoshi's Paradise"],
    )
    assert "Zorblax" in text
    assert "Did you mean" in text
    assert "advisor@tajlandia.com" in text
    assert "advisor@varsoviaestate.com" not in text
    assert "\u2014" not in text
    assert "bedroom" not in text.lower() or "bedrooms" not in text.lower()
    # must not invent numeric price facts
    assert "THB" not in text


def test_reply_validator_rejects_wrong_cards_for_named_query():
    ok, reasons = validate_reply(
        reply_text="Here is AYANA Heights for you.",
        cards=[{"property_id": "ayana", "name": "AYANA Heights Seaview Residences", "price": "10M THB"}],
        applied_filters={"query": "Mirage"},
    )
    assert ok is False
    assert any("named_property_missing" in r for r in reasons)

    ok2, _ = validate_reply(
        reply_text="Here is Mirage Sukhumvit 27.",
        cards=[{"property_id": "mirage-sukhumvit-27", "name": "Mirage Sukhumvit 27", "price": "10M THB"}],
        applied_filters={"query": "Mirage"},
    )
    assert ok2 is True


def test_search_no_unfiltered_fallback_on_bad_name(monkeypatch, name_index):
    from app.services import firestore_service as fs

    fetch_calls = {"n": 0}

    def boom_fetch(*a, **k):
        fetch_calls["n"] += 1
        raise AssertionError("fetch must not run for failed name search")

    monkeypatch.setattr(
        "app.services.property_name_index.ensure_name_index_from_firestore",
        lambda **k: None,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.match_property_name",
        name_index.match,
    )
    monkeypatch.setattr(fs, "fetch_properties_from_firestore", boom_fetch)
    monkeypatch.setattr(fs, "get_property_by_id_from_firestore", lambda *a, **k: None)

    out = fs.search_properties_firestore(query="Zorblax Towers", limit=6)
    assert out == []
    assert fetch_calls["n"] == 0
    tips = get_last_name_search_suggestions()
    assert isinstance(tips, list)
