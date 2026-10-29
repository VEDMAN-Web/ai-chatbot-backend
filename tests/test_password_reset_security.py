"""Forgot-password must not leak the reset token, and tokens are single-use."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.deps import get_db
from app.main import app
from app.services.firestore_auth_service import (
    generate_password_reset_token,
    get_test_reset_token,
    verify_password_reset_token,
)

client = TestClient(app)
GENERIC = "If an account exists for this email, a reset link has been sent."


def _seed_user(email):
    from app.core.security import hash_password
    from app.repositories import users_repo

    return users_repo.create_user(
        None, email=email, name="Reset User", role="employee", domain="tajlandia.com",
        allowed_websites=["tajlandia.com"], password_hash=hash_password("OldPass123!"),
        is_active=True, status="active", preferences={},
    )


def test_unknown_and_known_email_responses_match():
    known = "known-reset@example.com"
    unknown = f"missing-{uuid.uuid4().hex[:8]}@example.com"
    _seed_user(known)
    missing = client.post("/api/v1/auth/forgot-password", json={"email": unknown})
    present = client.post("/api/v1/auth/forgot-password", json={"email": known})

    assert missing.status_code == present.status_code == 200
    assert missing.json() == present.json() == {"success": True, "message": GENERIC}
    assert "reset_token" not in present.text
    assert get_test_reset_token(known)


def test_production_never_returns_reset_token(monkeypatch):
    email = "prod-reset@example.com"
    monkeypatch.setattr("app.core.config.settings.ENV", "production")
    monkeypatch.setattr("app.core.config.settings.EXPOSE_RESET_TOKEN_IN_RESPONSE", True)
    _seed_user(email)
    resp = client.post("/api/v1/auth/forgot-password", json={"email": email})

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "message": GENERIC}
    assert "reset_token" not in resp.json()
    assert get_test_reset_token(email) is None


def test_used_token_fails():
    email = f"used-{uuid.uuid4().hex[:6]}@example.com"
    _seed_user(email)
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

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["detail"] == "Invalid or expired password reset token."


def test_expired_token_fails():
    email = "expired-reset@example.com"
    token = generate_password_reset_token(email, expires_in_seconds=30)
    with patch("time.time", return_value=time.time() + 120):
        assert verify_password_reset_token(token, expected_email=email) is None
