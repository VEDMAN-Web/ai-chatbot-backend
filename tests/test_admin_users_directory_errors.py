"""
GET /api/v1/auth/admin/users returned 500 (reported by Rutul Patel, fixed on the Firestore code).

  * the `status` query parameter shadowed fastapi.status, so the wrong-domain 403 crashed into a 500;
  * list/details blocked super admins whose account domain differs from the deployment domain.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.v1.auth as auth_api
from app.core.auth import AuthenticatedStaff, require_admin_or_super_admin
from app.main import app

URL = "/api/v1/auth/admin/users"


def _staff(role: str, domain_key: str) -> AuthenticatedStaff:
    return AuthenticatedStaff(
        user_id=f"{role}@example.com", email=f"{role}@example.com", role=role,
        domain_key=domain_key, allowed_websites=["tajlandia.com"], selected_websites=[],
    )


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(auth_api, "get_current_env_domain_key", lambda: "tajlandia")
    yield TestClient(app)
    app.dependency_overrides.pop(require_admin_or_super_admin, None)


def _login_as(role: str, domain_key: str) -> None:
    app.dependency_overrides[require_admin_or_super_admin] = lambda: _staff(role, domain_key)


def test_admin_of_other_domain_gets_403_not_500(client):
    _login_as("admin", "varsovia")
    resp = client.get(URL)
    assert resp.status_code == 403
    assert "different domain" in resp.json()["detail"]


def test_status_filter_still_works_and_super_admin_is_not_blocked(client):
    _login_as("super_admin", "varsovia")
    resp = client.get(URL, params={"status": "active"})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_malformed_user_id_is_404_not_500(client):
    _login_as("super_admin", "varsovia")
    resp = client.get(f"{URL}/not-a-real-id")
    assert resp.status_code == 404
