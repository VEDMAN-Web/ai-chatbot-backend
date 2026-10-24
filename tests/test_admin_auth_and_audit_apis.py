import uuid
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.services.firestore_auth_service import (
    add_or_update_admin_in_firestore,
    get_admin_from_firestore,
    generate_password_reset_token,
    verify_password_reset_token
)

client = TestClient(app)


def test_forgot_password_generates_token_and_rejects_invalid_email():
    """Known and unknown emails share one response. The token is only on the test hook."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from app.db.session import get_db
    from app.services.firestore_auth_service import get_test_reset_token

    unknown = f"nonexistent_{uuid.uuid4().hex[:8]}@tajlandia.com"
    known = "admin@tajlandia.com"
    db = MagicMock()

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    try:
        db.query.return_value.filter.return_value.first.return_value = None
        with patch("app.services.firestore_auth_service.get_admin_from_firestore", return_value=None):
            unknown_resp = client.post("/api/v1/auth/forgot-password", json={"email": unknown})

        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(email=known)
        with patch("app.services.firestore_auth_service.get_admin_from_firestore", return_value={"email": known}):
            known_resp = client.post("/api/v1/auth/forgot-password", json={"email": known})
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert unknown_resp.status_code == known_resp.status_code == 200
    assert unknown_resp.json() == known_resp.json()
    assert "reset_token" not in known_resp.json()
    token = get_test_reset_token(known)
    assert token
    assert verify_password_reset_token(token, expected_email=known) == known


def test_reset_password_flow_and_tamper_rejection():
    """
    Tests the reset-password endpoint using a dedicated test admin account,
    verifying tamper rejection and successful password reset.
    """
    test_email = f"test_reset_{uuid.uuid4().hex[:6]}@tajlandia.com"
    init_pass = "InitialPass123!"
    new_pass = "BrandNewPass456!"

    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from app.db.session import get_db
    from app.services.firestore_auth_service import get_test_reset_token

    user = SimpleNamespace(id=uuid.uuid4(), email=test_email, password_hash="old")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    db.query.return_value.filter.return_value.update.return_value = 1

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    try:
        with patch("app.services.firestore_auth_service.get_admin_from_firestore", return_value={"email": test_email}), patch(
            "app.services.firestore_auth_service.update_admin_password_in_firestore", return_value=True
        ):
            forgot_resp = client.post("/api/v1/auth/forgot-password", json={"email": test_email})
            assert forgot_resp.status_code == 200
            assert "reset_token" not in forgot_resp.json()
            reset_token = get_test_reset_token(test_email)
            assert reset_token

            bad_resp = client.post(
                "/api/v1/auth/reset-password",
                json={
                    "email": test_email,
                    "reset_token": "tampered_fake_token_abc123",
                    "new_password": new_pass,
                },
            )
            assert bad_resp.status_code == 400

            short_resp = client.post(
                "/api/v1/auth/reset-password",
                json={
                    "email": test_email,
                    "reset_token": reset_token,
                    "new_password": "123",
                },
            )
            assert short_resp.status_code in [400, 422]

            ok_resp = client.post(
                "/api/v1/auth/reset-password",
                json={
                    "email": test_email,
                    "reset_token": reset_token,
                    "new_password": new_pass,
                },
            )
            assert ok_resp.status_code == 200
            assert ok_resp.json()["success"] is True
            assert user.password_hash != "old"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_change_password_endpoint():
    """Tests the authenticated change-password endpoint."""
    test_email = f"test_change_{uuid.uuid4().hex[:6]}@tajlandia.com"
    current_pass = "CurrentPass123!"
    updated_pass = "UpdatedPass789!"

    # Create test admin
    add_or_update_admin_in_firestore(
        email=test_email,
        password=current_pass,
        role="employee",
        name="Change Test User"
    )

    # Log in to get token
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": test_email, "password": current_pass}
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Incorrect old password -> 400
    wrong_old = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"old_password": "WrongPassword999!", "new_password": updated_pass}
    )
    assert wrong_old.status_code == 400

    # 2. Correct old password -> 200
    change_ok = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"old_password": current_pass, "new_password": updated_pass}
    )
    assert change_ok.status_code == 200
    assert change_ok.json()["success"] is True

    # 3. Log in with new password
    relogin = client.post(
        "/api/v1/auth/login",
        json={"email": test_email, "password": updated_pass}
    )
    assert relogin.status_code == 200


def test_admin_register_endpoint():
    """Verifies that an authorized admin can register new staff members and sync to Firestore."""
    # Log in as super_admin
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@tajlandia.com", "password": settings.ADMIN_PASSWORD}
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    new_email = f"registered_agent_{uuid.uuid4().hex[:6]}@tajlandia.com"
    new_pass = "AgentSecret@2026"

    # Register via Bearer token
    reg_resp = client.post(
        "/api/v1/auth/register",
        headers=headers,
        json={
            "email": new_email,
            "password": new_pass,
            "name": "Registered Agent",
            "role": "employee"
        }
    )
    assert reg_resp.status_code == 201
    reg_data = reg_resp.json()
    assert reg_data["email"] == new_email
    assert reg_data["role"] == "employee"

    # Verify Firestore document exists
    fs_doc = get_admin_from_firestore(new_email)
    assert fs_doc is not None
    assert fs_doc.get("email") == new_email

    # Verify immediate login
    agent_login = client.post(
        "/api/v1/auth/login",
        json={"email": new_email, "password": new_pass}
    )
    assert agent_login.status_code == 200


def test_admin_activity_logs_api():
    """Verifies the GET /api/v1/admin/activity-logs endpoint."""
    # Log in as admin
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@tajlandia.com", "password": settings.ADMIN_PASSWORD}
    )
    assert login_resp.status_code == 200
    headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

    # Fetch activity logs
    logs_resp = client.get("/api/v1/admin/activity-logs?limit=10", headers=headers)
    assert logs_resp.status_code == 200
    data = logs_resp.json()
    assert "total" in data
    assert "logs" in data
    assert isinstance(data["logs"], list)

    # Filter by action or admin
    filtered_resp = client.get(
        "/api/v1/admin/activity-logs?action=auth&limit=5",
        headers=headers
    )
    assert filtered_resp.status_code == 200
    fdata = filtered_resp.json()
    assert "logs" in fdata


def test_data_change_history_api():
    """Verifies the GET /api/v1/admin/data-history endpoint."""
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@tajlandia.com", "password": settings.ADMIN_PASSWORD}
    )
    assert login_resp.status_code == 200
    headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

    # Fetch data change history
    history_resp = client.get("/api/v1/admin/data-history?limit=10", headers=headers)
    assert history_resp.status_code == 200
    hdata = history_resp.json()
    assert "total" in hdata
    assert "history" in hdata
    assert isinstance(hdata["history"], list)

    # Filter by target_type
    prop_history = client.get(
        "/api/v1/admin/data-history?target_type=property&limit=5",
        headers=headers
    )
    assert prop_history.status_code == 200
    assert "history" in prop_history.json()
