"""Google OAuth callback: escaped output, no upstream error text, tokens only stored on a successful exchange."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1 import auth as auth_module
from tests import fs_users
from app.repositories import users_repo

client = TestClient(app)
PREFIX = "goauth_test_"


@pytest.fixture
def staff_user():
    email = f"{PREFIX}{uuid.uuid4().hex[:6]}@tajlandia.com"
    fs_users.seed_user("admin", email, preferences={})
    return {"email": email, "id": str(users_repo.get_by_email(None, email).id)}


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload or {}, text

    def json(self):
        return self._payload


def _fake_google(monkeypatch, token_resp, userinfo_resp=None):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None, **kw):
            return token_resp

        def get(self, url, headers=None, **kw):
            return userinfo_resp or _Resp(200, {"email": "someone@example.com"})

    monkeypatch.setattr(auth_module.httpx, "Client", _Client)


def test_error_parameter_is_escaped():
    resp = client.get("/api/v1/auth/google/callback", params={"error": "<script>alert(1)</script>"})
    assert resp.status_code == 400
    assert "<script>" not in resp.text and "&lt;script&gt;" in resp.text


def test_missing_code_or_bad_state_is_rejected():
    assert client.get("/api/v1/auth/google/callback").status_code == 400
    assert client.get("/api/v1/auth/google/callback", params={"code": "c", "state": "forged"}).status_code == 400


def test_google_error_body_is_not_returned(staff_user, monkeypatch):
    state = auth_module._generate_state_token(staff_user["id"], staff_user["email"])
    _fake_google(monkeypatch, _Resp(400, text='{"error":"invalid_grant","client_secret":"LEAKY-UPSTREAM-TEXT"}'))
    resp = client.get("/api/v1/auth/google/callback", params={"code": "c", "state": state})
    assert resp.status_code == 500
    assert "LEAKY-UPSTREAM-TEXT" not in resp.text and "invalid_grant" not in resp.text
    assert "google_workspace" not in fs_users.get_preferences(staff_user["email"])


def test_success_page_escapes_the_google_email_and_stores_the_connection(staff_user, monkeypatch):
    state = auth_module._generate_state_token(staff_user["id"], staff_user["email"])
    _fake_google(
        monkeypatch,
        _Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "a b"}),
        _Resp(200, {"email": "<img src=x onerror=alert(1)>@example.com"}),
    )
    resp = client.get("/api/v1/auth/google/callback", params={"code": "c", "state": state})
    assert resp.status_code == 200
    assert "<img src=x" not in resp.text and "&lt;img" in resp.text
    gw = fs_users.get_preferences(staff_user["email"]).get("google_workspace")
    assert gw and gw["connected"] is True
