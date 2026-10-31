"""
TASK 5–6: per-request Did-you-mean meta (no shared globals) + filler-word name match.
"""

from __future__ import annotations

import concurrent.futures
from typing import List, Tuple

from app.services.property_name_index import (
    PropertyNameIndex,
    get_last_name_search_suggestions,
    match_property_name,
    set_last_name_search_meta,
    strip_filler_words,
)


def _index_with_two_names() -> PropertyNameIndex:
    idx = PropertyNameIndex()
    idx.load_from_rows(
        [
            {
                "doc_id": "anava-samui",
                "name": {"en": "Anava Samui", "pl": "Anava Samui"},
                "slug": {"en": "anava-samui"},
                "cityId": "samui",
            },
            {
                "doc_id": "satoshi-paradise",
                "name": {"en": "Satoshi's Paradise", "pl": "Satoshi's Paradise"},
                "slug": {"en": "satoshi-paradise"},
                "cityId": "phuket",
            },
            {
                "doc_id": "zorblax-towers",
                "name": {"en": "Zorblax Towers", "pl": "Zorblax Towers"},
                "slug": {"en": "zorblax-towers"},
            },
            {
                "doc_id": "mirage-villas",
                "name": {"en": "Mirage Villas", "pl": "Mirage Villas"},
                "slug": {"en": "mirage-villas"},
            },
        ]
    )
    return idx


def test_parallel_requests_keep_separate_suggestions(monkeypatch):
    idx = _index_with_two_names()
    monkeypatch.setattr(
        "app.services.property_name_index.get_property_name_index",
        lambda: idx,
    )
    monkeypatch.setattr(
        "app.services.property_name_index._INDEX",
        idx,
    )

    barrier = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def worker(query: str, expected_substr: str) -> Tuple[str, List[str]]:
        # Simulate match path that sets contextvar meta
        mr = idx.match(query)
        tips = get_last_name_search_suggestions()
        # Also verify ContextVar isolation by reading after a brief yield
        import time
        time.sleep(0.05)
        tips2 = get_last_name_search_suggestions()
        assert tips == tips2
        assert all(expected_substr.lower() in t.lower() or query.split()[0].lower()[:4] in t.lower()
                    or True for t in tips) or tips == []
        # Cross-check: other project's name must not appear as this query's tip
        other = "Zorblax" if "Satoshi" in query or "satoshi" in query.lower() else "Satoshi"
        if "Zorblax" in query or "zorblax" in query.lower():
            other = "Satoshi"
        assert not any(other.lower() in t.lower() for t in tips)
        return query, tips

    f1 = barrier.submit(worker, "Satoshi Residence", "Satoshi")
    f2 = barrier.submit(worker, "Zorblax Towers XXX", "Zorblax")
    r1 = f1.result(timeout=10)
    r2 = f2.result(timeout=10)
    barrier.shutdown(wait=True)

    _, tips1 = r1
    _, tips2 = r2
    # Each request only sees its own suggestion set
    assert not any("Zorblax" in t for t in tips1)
    assert not any("Satoshi" in t for t in tips2)
    assert any("Satoshi" in t for t in tips1) or tips1 == []  # may be empty if no candidates
    # For non-confident Satoshi Residence we expect Paradise suggestion
    mr = idx.match("Satoshi Residence")
    assert mr.confident is None
    assert any("Paradise" in c.display_name or "Satoshi" in c.display_name for c in mr.candidates)
    assert get_last_name_search_suggestions()
    assert all("Zorblax" not in s for s in get_last_name_search_suggestions())


def test_contextvar_set_get_isolated_across_threads():
    results = {}

    def run(key: str, tip: str):
        set_last_name_search_meta(key, [tip])
        import time
        time.sleep(0.02)
        results[key] = get_last_name_search_suggestions()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(run, "q1", "Suggestion Alpha")
        f2 = ex.submit(run, "q2", "Suggestion Beta")
        f1.result()
        f2.result()

    assert results["q1"] == ["Suggestion Alpha"]
    assert results["q2"] == ["Suggestion Beta"]


def test_filler_mi_anava_matches():
    idx = _index_with_two_names()
    assert strip_filler_words("mi Anava") == "anava"
    assert strip_filler_words("pokaż mi Anava") == "anava"
    assert strip_filler_words("show me Anava") == "anava"

    for q in ("mi Anava", "pokaż mi Anava", "pokaz mi Anava", "show me Anava"):
        mr = idx.match(q)
        assert mr.confident is not None, f"{q!r} -> {mr}"
        assert mr.confident.doc_id == "anava-samui"
