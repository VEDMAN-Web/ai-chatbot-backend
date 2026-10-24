"""Forgot-password must not leak the reset token, and tokens are single-use."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.services.firestore_auth_service import (
    generate_password_reset_token,
    get_test_reset_token,
    verify_password_reset_token,
)

client = TestClient(app)
GENERIC = "If an account exists for this email, a reset link has been sent."


def _db_override(user):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    return db


def test_unknown_and_known_email_responses_match():
    known = "known-reset@example.com"
    unknown = f"missing-{uuid.uuid4().hex[:8]}@example.com"
    _db_override(None)
    try:
        with patch("app.services.firestore_auth_service.get_admin_from_firestore", return_value=None):
            missing = client.post("/api/v1/auth/forgot-password", json={"email": unknown})
        _db_override(SimpleNamespace(email=known))
        with patch(
            "app.services.firestore_auth_service.get_admin_from_firestore",
            return_value={"email": known},
        ):
            present = client.post("/api/v1/auth/forgot-password", json={"email": known})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert missing.status_code == present.status_code == 200
    assert missing.json() == present.json() == {"success": True, "message": GENERIC}
    assert "reset_token" not in present.text
    assert get_test_reset_token(known)


def test_production_never_returns_reset_token(monkeypatch):
    email = "prod-reset@example.com"
    monkeypatch.setattr("app.core.config.settings.ENV", "production")
    monkeypatch.setattr("app.core.config.settings.EXPOSE_RESET_TOKEN_IN_RESPONSE", True)
    _db_override(SimpleNamespace(email=email))
    try:
        with patch(
            "app.services.firestore_auth_service.get_admin_from_firestore",
            return_value={"email": email},
        ):
            resp = client.post("/api/v1/auth/forgot-password", json={"email": email})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "message": GENERIC}
    assert "reset_token" not in resp.json()
    assert get_test_reset_token(email) is None


def test_used_token_fails():
    email = f"used-{uuid.uuid4().hex[:6]}@example.com"
    user = SimpleNamespace(id=uuid.uuid4(), email=email, password_hash="old")
    _db_override(user)
    try:
        with patch(
            "app.services.firestore_auth_service.get_admin_from_firestore",
            return_value={"email": email},
        ), patch(
            "app.services.firestore_auth_service.update_admin_password_in_firestore",
            return_value=True,
        ):
            issued = client.post("/api/v1/auth/forgot-password", json={"email": email})
            token = get_test_reset_token(email)
            assert issued.status_code == 200 and token
            first = client.post(
                "/api/v1/auth/reset-password",
                json={"email": email, "reset_token": token, "new_password": "BrandNewPass456!"},
            )
            second = client.post(
                "/api/v1/auth/reset-password",
                json={"email": email, "reset_token": token, "new_password": "AnotherPass789!"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["detail"] == "Invalid or expired password reset token."


def test_expired_token_fails():
    email = "expired-reset@example.com"
    token = generate_password_reset_token(email, expires_in_seconds=30)
    with patch("time.time", return_value=time.time() + 120):
        assert verify_password_reset_token(token, expected_email=email) is None
