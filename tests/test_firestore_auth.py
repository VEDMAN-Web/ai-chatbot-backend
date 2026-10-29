"""Firestore-backed admin authentication (admin_authentication collection) against the in-memory fake."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.firestore_auth_service import (
    add_or_update_admin_in_firestore,
    get_admin_from_firestore,
    verify_firestore_admin_credentials,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def seeded_admin():
    add_or_update_admin_in_firestore(
        email="admin@tajlandia.com", password="Admin@123456", role="super_admin", name="Admin"
    )
    yield


def test_firestore_admin_lookup_and_verification():
    admin_doc = get_admin_from_firestore("admin@tajlandia.com")
    assert admin_doc is not None
    assert admin_doc.get("email") == "admin@tajlandia.com"
    assert admin_doc.get("role") in ["super_admin", "admin"]

    verified = verify_firestore_admin_credentials("admin@tajlandia.com", "Admin@123456")
    assert verified is not None
    assert verified["email"] == "admin@tajlandia.com"
    assert verified["role"] == "super_admin"
    assert verify_firestore_admin_credentials("admin@tajlandia.com", "WrongPassword999") is None


def test_login_endpoint_authenticates_with_firestore_admin():
    resp = client.post("/api/v1/auth/login", json={"email": "admin@tajlandia.com", "password": "Admin@123456"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["token_type"] == "bearer" and data["access_token"]
    assert data["user"]["email"] == "admin@tajlandia.com"
    assert data["user"]["role"] in ["super_admin", "admin"]


def test_new_admin_added_to_firestore_can_immediately_login():
    add_or_update_admin_in_firestore(
        email="new_manager@tajlandia.com", password="Manager@2026Secure", role="super_admin", name="New Manager"
    )
    resp = client.post(
        "/api/v1/auth/login", json={"email": "new_manager@tajlandia.com", "password": "Manager@2026Secure"}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    chat = client.post(
        "/api/v1/admin/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"session_id": "test-new-admin-session", "message": "Hello, verify my admin access"},
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["type"] == "text"
