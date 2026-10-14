import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db.session import SessionLocal
from app.services.firestore_auth_service import (
    get_admin_from_firestore,
    verify_firestore_admin_credentials,
    add_or_update_admin_in_firestore
)

client = TestClient(app)


def test_firestore_admin_lookup_and_verification():
    """Verifies that admin@tajlandia.com exists in Firestore and verifies with Admin@123456."""
    admin_doc = get_admin_from_firestore("admin@tajlandia.com")
    assert admin_doc is not None, "admin@tajlandia.com must exist in Firestore admin_authentication"
    assert admin_doc.get("email") == "admin@tajlandia.com"
    assert admin_doc.get("role") in ["super_admin", "admin"]

    # Valid password
    verified = verify_firestore_admin_credentials("admin@tajlandia.com", "Admin@123456")
    assert verified is not None
    assert verified["email"] == "admin@tajlandia.com"
    assert verified["role"] == "super_admin"

    # Invalid password
    bad = verify_firestore_admin_credentials("admin@tajlandia.com", "WrongPassword999")
    assert bad is None


def test_login_endpoint_authenticates_with_live_firestore_admin():
    """Verifies that POST /api/v1/auth/login succeeds using live Firestore credentials."""
    resp = client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@tajlandia.com",
            "password": "Admin@123456"
        }
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == "admin@tajlandia.com"
    assert data["user"]["role"] in ["super_admin", "admin"]


def test_new_admin_added_to_firestore_can_immediately_login():
    """
    Simulates adding a new admin to Firestore admin_authentication (as would happen via Firebase Console).
    Verifies that the new admin can immediately log in and access protected admin APIs.
    """
    test_email = "new_manager@tajlandia.com"
    test_pass = "Manager@2026Secure"

    try:
        # Add to live Firestore
        add_or_update_admin_in_firestore(
            email=test_email,
            password=test_pass,
            role="super_admin",
            name="New Manager"
        )

        # Immediately log in via API
        resp = client.post(
            "/api/v1/auth/login",
            json={
                "email": test_email,
                "password": test_pass
            }
        )
        assert resp.status_code == 200, f"New admin login failed: {resp.text}"
        token = resp.json()["access_token"]
        assert token is not None

        # Verify protected admin endpoint with the token
        chat_resp = client.post(
            "/api/v1/admin/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "session_id": "test-new-admin-session",
                "message": "Hello, verify my admin access"
            }
        )
        assert chat_resp.status_code == 200
        assert chat_resp.json()["type"] == "text"
    finally:
        # Guarantee test artifact is immediately removed from production Firestore
        from app.services.firestore_service import get_firestore_client
        db = get_firestore_client()
        if db:
            db.collection("admin_authentication").document(test_email).delete()
