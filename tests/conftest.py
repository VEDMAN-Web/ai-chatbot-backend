"""
Pytest bootstrap. Firestore is the only datastore.

Live Google Firestore is blocked by default so the suite cannot hang on network RPCs.
Every test gets an in-memory FakeFirestore (tests.fake_firestore) patched in as the Firestore client.
Set ALLOW_LIVE_FIRESTORE=1 with credentials to run against a real project.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("ENV", "test")


def _live_firestore_allowed() -> bool:
    return os.environ.get("ALLOW_LIVE_FIRESTORE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def firestore_credentials_present() -> bool:
    path = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    return bool(path and os.path.isfile(path))


@pytest.fixture(autouse=True)
def _default_fake_firestore(monkeypatch):
    """Patch every Firestore client getter to one shared in-memory fake (unless live Firestore is allowed)."""
    if _live_firestore_allowed():
        yield None
        return
    from tests.fake_firestore import patch_firestore

    fake = patch_firestore(monkeypatch)
    monkeypatch.setattr("app.core.config.settings.ENABLE_AUDIT_LOGGING", True, raising=False)
    try:
        from app.services.firestore_service import clear_property_catalog_cache

        clear_property_catalog_cache()
    except Exception:
        pass
    try:
        from app.services.admin_perf import tool_cache_invalidate_all

        tool_cache_invalidate_all()
    except Exception:
        pass
    try:
        # The name index is process-wide with a 10-minute cache: start every test from an empty one.
        from app.services.property_name_index import get_property_name_index

        get_property_name_index().clear()
    except Exception:
        pass
    yield fake


@pytest.fixture
def db():
    """Placeholder for the removed SQL session (truthy, no-op commit/rollback)."""
    from app.core.deps import FirestoreSession

    return FirestoreSession()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_firestore: requires live Firestore credentials (skipped otherwise)",
    )


def pytest_collection_modifyitems(config, items):
    if _live_firestore_allowed() and firestore_credentials_present():
        return
    skip = pytest.mark.skip(
        reason=(
            "Live Firestore credentials not present "
            "(set ALLOW_LIVE_FIRESTORE=1 and GOOGLE_APPLICATION_CREDENTIALS)"
        )
    )
    live_files = (
        "test_firestore_auth.py",
        "test_firestore_integration.py",
    )
    for item in items:
        if "live_firestore" in item.keywords:
            item.add_marker(skip)
            continue
        path = str(getattr(item, "fspath", "") or "")
        if any(name in path for name in live_files):
            item.add_marker(skip)
