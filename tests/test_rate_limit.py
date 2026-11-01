"""Sliding-window limits: allow up to the cap, block after, allow again once the window moves."""

import os

import pytest
from fastapi import HTTPException

from app.core.rate_limit import SlidingWindowLimiter, client_ip, enforce_rate_limit, reset_rate_limiter


SCOPES = (
    "login",
    "forgot_password",
    "reset_password",
    "public_chat",
    "admin_chat",
    "admin_write",
    "url_import",
    "superadmin_write",
)


class _Client:
    def __init__(self, host):
        self.host = host


class _Request:
    def __init__(self, host="10.0.0.8", headers=None):
        self.client = _Client(host)
        self.headers = headers or {}


@pytest.mark.parametrize("scope", SCOPES)
def test_each_scope_blocks_then_resets(scope):
    limiter = SlidingWindowLimiter()
    key = f"{scope}:id:user@example.com"
    assert limiter.check(key, 2, 10, now=100)[0] is True
    assert limiter.check(key, 2, 10, now=101)[0] is True
    allowed, retry_after = limiter.check(key, 2, 10, now=102)
    assert allowed is False
    assert retry_after >= 1
    assert limiter.check(key, 2, 10, now=112)[0] is True


def test_forwarded_for_is_ignored_unless_trusted(monkeypatch):
    monkeypatch.setattr("app.core.rate_limit.settings.TRUST_PROXY_HEADERS", False)
    request = _Request(host="10.0.0.8", headers={"x-forwarded-for": "203.0.113.9"})
    assert client_ip(request) == "10.0.0.8"
    monkeypatch.setattr("app.core.rate_limit.settings.TRUST_PROXY_HEADERS", True)
    assert client_ip(request) == "203.0.113.9"


def test_enforce_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setenv("ENFORCE_RATE_LIMITS", "1")
    reset_rate_limiter()
    request = _Request()
    for _ in range(2):
        enforce_rate_limit(request, scope="login-test", identities=["a@b.com"], limit=2, window_seconds=60)
    with pytest.raises(HTTPException) as exc:
        enforce_rate_limit(request, scope="login-test", identities=["a@b.com"], limit=2, window_seconds=60)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]
    assert "message" in exc.value.detail
    os.environ.pop("ENFORCE_RATE_LIMITS", None)
    reset_rate_limiter()
