"""
Pytest bootstrap for the user chatbot service.

Staff users and sessions default to Firestore in production (STORE_USERS=firestore). Most unit tests have
no Firestore client, so they run on the SQL store unless a test sets the mode itself (the F3 tests do).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_users_store_postgres(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.STORE_USERS", "postgres")
    yield
