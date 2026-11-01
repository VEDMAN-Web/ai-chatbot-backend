"""
TASK 3: Catalog / name-index single-flight, stale-while-revalidate, OpenAI timeout settings, startup pre-warm.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_catalog():
    from app.services.firestore_service import clear_property_catalog_cache, _CATALOG_REFRESHING, _CATALOG_INFLIGHT

    clear_property_catalog_cache()
    with __import__("app.services.firestore_service", fromlist=["_CATALOG_LOCK"])._CATALOG_LOCK:
        _CATALOG_REFRESHING.clear()
        _CATALOG_INFLIGHT.clear()
    yield
    clear_property_catalog_cache()
    with __import__("app.services.firestore_service", fromlist=["_CATALOG_LOCK"])._CATALOG_LOCK:
        _CATALOG_REFRESHING.clear()
        _CATALOG_INFLIGHT.clear()


def test_four_threads_cold_cache_streams_once(monkeypatch):
    """4 parallel cold requests → collection streamed only once; all get results."""
    import app.services.firestore_service as fs

    clear = fs.clear_property_catalog_cache
    clear()

    call_count = {"n": 0}
    barrier = threading.Barrier(4, timeout=10)
    load_started = threading.Event()

    def slow_uncached(**kwargs):
        call_count["n"] += 1
        load_started.set()
        time.sleep(0.25)
        return [
            {"property_id": f"p{i}", "name": f"Prop {i}", "city_id": "phuket"}
            for i in range(5)
        ]

    monkeypatch.setattr(fs, "_fetch_properties_from_firestore_uncached", slow_uncached)
    monkeypatch.setattr(fs, "_catalog_ttl_sec", lambda: 900.0)

    results: List[Any] = [None] * 4
    errors: List[BaseException] = []

    def worker(i: int):
        try:
            barrier.wait()
            results[i] = fs.fetch_properties_from_firestore(limit=5000, target="user")
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert call_count["n"] == 1, f"expected single-flight load, got {call_count['n']}"
    for r in results:
        assert r is not None and len(r) == 5


def test_expired_cache_returns_stale_immediately(monkeypatch):
    import app.services.firestore_service as fs

    fs.clear_property_catalog_cache()
    stale_data = [{"property_id": "stale1", "name": "Stale Villa"}]
    # Seed expired cache
    with fs._CATALOG_LOCK:
        fs._CATALOG_CACHE["user||True"] = (time.monotonic() - 10_000, list(stale_data))

    refresh_started = threading.Event()
    refresh_done = threading.Event()

    def slow_refresh(**kwargs):
        refresh_started.set()
        time.sleep(0.4)
        refresh_done.set()
        return [{"property_id": "fresh1", "name": "Fresh Villa"}]

    monkeypatch.setattr(fs, "_fetch_properties_from_firestore_uncached", slow_refresh)
    monkeypatch.setattr(fs, "_catalog_ttl_sec", lambda: 1.0)

    t0 = time.monotonic()
    out = fs.fetch_properties_from_firestore(limit=5000, target="user")
    elapsed = time.monotonic() - t0

    assert out[0]["property_id"] == "stale1"
    assert elapsed < 0.2, f"stale return should be immediate, took {elapsed:.3f}s"
    assert refresh_started.wait(timeout=2.0)
    assert refresh_done.wait(timeout=3.0)
    # After refresh, cache should be fresh
    out2 = fs.fetch_properties_from_firestore(limit=5000, target="user")
    assert out2[0]["property_id"] == "fresh1"


def test_failed_refresh_keeps_old_data(monkeypatch):
    import app.services.firestore_service as fs

    fs.clear_property_catalog_cache()
    good = [{"property_id": "keep1", "name": "Keep Me"}]
    with fs._CATALOG_LOCK:
        fs._CATALOG_CACHE["user||True"] = (time.monotonic() - 10_000, list(good))

    def boom(**kwargs):
        raise fs.FirestoreDatabaseError("simulated firestore down")

    monkeypatch.setattr(fs, "_fetch_properties_from_firestore_uncached", boom)
    monkeypatch.setattr(fs, "_catalog_ttl_sec", lambda: 1.0)

    out = fs.fetch_properties_from_firestore(limit=5000, target="user")
    assert out[0]["property_id"] == "keep1"
    # Wait briefly for background refresh to fail
    time.sleep(0.3)
    with fs._CATALOG_LOCK:
        hit = fs._CATALOG_CACHE.get("user||True")
    assert hit is not None
    assert hit[1][0]["property_id"] == "keep1"


def test_name_index_failed_refresh_keeps_entries():
    from app.services.property_name_index import PropertyNameIndex

    idx = PropertyNameIndex()
    idx.load_from_rows([{"doc_id": "abc", "name": "Anava", "cityId": "phuket"}])
    assert len(idx._entries) == 1

    def bad_loader():
        raise RuntimeError("firestore down")

    idx.configure_loader(bad_loader)
    # Force age past TTL
    idx._loaded_at = time.time() - 10_000
    idx.ensure_fresh(force=False)
    # SWR: background may still be running — wait a bit
    time.sleep(0.3)
    assert len(idx._entries) == 1
    assert idx._entries[0].doc_id == "abc"


@pytest.mark.skip(reason="rp-user-fix switched the public chatbot from OpenAI to Claude; this tests the old OpenAI client helper. Replace with a Claude-client equivalent.")
def test_openai_timeout_and_retries_applied_to_client(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPENAI_TIMEOUT", 25.0)
    monkeypatch.setattr("app.core.config.settings.OPENAI_MAX_RETRIES", 1)
    monkeypatch.setattr("app.core.config.settings.OPENAI_API_KEY", "sk-test-key")

    from app.services.public_agent import PublicAgentService, _openai_timeout_sec, _openai_max_retries

    assert _openai_timeout_sec() == 25.0
    assert _openai_max_retries() == 1

    with patch("app.services.public_agent.OpenAI") as MockOpenAI:
        MockOpenAI.return_value = MagicMock()
        svc = PublicAgentService()
        MockOpenAI.assert_called()
        kwargs = MockOpenAI.call_args.kwargs
        assert kwargs.get("timeout") == 25.0
        assert kwargs.get("max_retries") == 1
        assert svc.timeout == 25.0
        assert svc.max_retries == 1


@pytest.mark.skip(reason="rp-user-fix switched the public chatbot from OpenAI to Claude; this tests the old OpenAI client helper. Replace with a Claude-client equivalent.")
def test_safe_openai_create_sets_timeout(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.OPENAI_TIMEOUT", 25.0)
    from app.services.public_agent import _safe_openai_create

    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock()
    _safe_openai_create(client, model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs.get("timeout") == 25.0


def test_startup_prewarm_does_not_raise_when_firestore_unavailable(monkeypatch):
    from app.main import _prewarm_caches_background

    def boom(*a, **k):
        raise RuntimeError("no firestore")

    monkeypatch.setattr(
        "app.services.firestore_service.prewarm_property_catalog",
        boom,
        raising=False,
    )
    # Patch at import site used inside prewarm
    monkeypatch.setattr(
        "app.services.firestore_service.fetch_properties_from_firestore",
        boom,
    )

    def boom_index(*a, **k):
        raise RuntimeError("no index")

    monkeypatch.setattr(
        "app.services.property_name_index.prewarm_property_name_index",
        boom_index,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.property_name_index.ensure_name_index_from_firestore",
        boom_index,
    )

    # Must not raise
    _prewarm_caches_background()


def test_name_index_single_flight_cold(monkeypatch):
    from app.services.property_name_index import PropertyNameIndex

    idx = PropertyNameIndex()
    calls = {"n": 0}
    barrier = threading.Barrier(4, timeout=10)

    def slow_loader():
        calls["n"] += 1
        time.sleep(0.25)
        return [{"doc_id": "x1", "name": "Lotus", "cityId": "phuket"}]

    idx.configure_loader(slow_loader)
    errors = []

    def worker():
        try:
            barrier.wait()
            idx.ensure_fresh(force=True)
        except Exception as e:
            errors.append(e)

    # First wave: force cold on empty — only first force=True should lead; but each
    # ensure_fresh(force=True) on empty: first becomes leader, others wait.
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    assert calls["n"] == 1
    assert len(idx._entries) == 1
