"""
Pytest bootstrap — set test DATABASE_URL before app settings are imported.
Must not contain production credentials.

Live Google Firestore is blocked by default (clients return None) so the suite
cannot hang on network RPCs. Tests that need Firestore must patch clients
themselves (FakeFirestore) or set ALLOW_LIVE_FIRESTORE=1 with credentials.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test_user:test_pass@127.0.0.1:5432/tajlandia_test",
)
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
def _block_live_google_firestore(monkeypatch):
    """Return None from Firestore getters unless ALLOW_LIVE_FIRESTORE is set."""
    if _live_firestore_allowed():
        yield
        return

    def _none(*a, **k):
        return None

    monkeypatch.setattr(
        "app.services.firestore_service.get_firestore_client",
        _none,
    )
    monkeypatch.setattr(
        "app.services.firestore_service.get_admin_firestore_client",
        _none,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.firestore_service.get_user_firestore_client",
        _none,
        raising=False,
    )
    yield


@pytest.fixture(autouse=True)
def _default_stores_postgres_when_no_fs(monkeypatch):
    """
    Product default is firestore-only, but with live FS blocked most unit tests
    have no client. Use postgres store modes unless a test overrides them or
    provides FakeFirestore via set_store_modes / patch_firestore.
    """
    if _live_firestore_allowed():
        yield
        return
    # Soft default for isolated unit tests (explicit firestore tests override)
    monkeypatch.setattr("app.core.config.settings.STORE_PENDING_ACTIONS", "postgres")
    monkeypatch.setattr("app.core.config.settings.STORE_AUDIT", "postgres")
    monkeypatch.setattr("app.core.config.settings.STORE_LEADS_READ", "postgres")
    monkeypatch.setattr("app.core.config.settings.STORE_USERS", "postgres")
    yield


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
