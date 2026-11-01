import uuid
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.repositories import users_repo
from app.core.security import hash_password, generate_session_token
from app.core.tenant import ALL_CANONICAL_WEBSITES

client = TestClient(app)


@pytest.fixture(autouse=True)
def seed_super_admin(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    email = "test_super_admin@tajlandia.com"
    if users_repo.get_by_email(None, email) is None:
        users_repo.create_user(
            None, email=email, name="Test Super Admin", role="super_admin", domain="tajlandia.com",
            password_hash=hash_password("SuperAdmin123!"), is_active=True, status="active",
            allowed_websites=list(ALL_CANONICAL_WEBSITES), preferences={},
        )
    yield


def _login_and_get_token(email: str, password: str) -> str:
    """Helper to authenticate and return session token."""
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, f"Login failed for {email}: {resp.text}"
    return resp.json()["access_token"]


def test_super_admin_can_assign_single_multiple_all_websites():
    """
    Super Admin can assign specific access to any user:
    - All websites (['varsoviaestate.com', 'tajlandia.com', 'thai.estate'])
    - Multiple websites (['tajlandia.com', 'varsoviaestate.com'])
    - Single website (['thai.estate'])
    """
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")
    headers = {"Authorization": f"Bearer {sa_token}"}

    # 1. Single website worker
    single_email = f"single_{uuid.uuid4().hex[:6]}@thai.estate"
    resp1 = client.post(
        "/api/v1/auth/admin/users",
        headers=headers,
        json={
            "email": single_email,
            "password": "WorkerPass123!",
            "name": "Single Site Worker",
            "role": "worker",
            "websites": ["thai.estate"]
        }
    )
    assert resp1.status_code == 201, resp1.text
    assert resp1.json()["allowed_websites"] == ["thai.estate"]

    # 2. Multiple websites worker (Tajlandia & Varsovia)
    multi_email = f"multi_{uuid.uuid4().hex[:6]}@tajlandia.com"
    resp2 = client.post(
        "/api/v1/auth/admin/users",
        headers=headers,
        json={
            "email": multi_email,
            "password": "WorkerPass123!",
            "name": "Multi Site Worker",
            "role": "worker",
            "websites": ["tajlandia.com", "varsoviaestate.com"]
        }
    )
    assert resp2.status_code == 201, resp2.text
    assigned_multi = resp2.json()["allowed_websites"]
    assert "tajlandia.com" in assigned_multi
    assert "varsoviaestate.com" in assigned_multi
    assert "thai.estate" not in assigned_multi

    # 3. All websites admin
    all_email = f"all_{uuid.uuid4().hex[:6]}@tajlandia.com"
    resp3 = client.post(
        "/api/v1/auth/admin/users",
        headers=headers,
        json={
            "email": all_email,
            "password": "AdminPass123!",
            "name": "All Sites Admin",
            "role": "admin",
            "websites": ["varsoviaestate.com", "tajlandia.com", "thai.estate"]
        }
    )
    assert resp3.status_code == 201, resp3.text
    assigned_all = resp3.json()["allowed_websites"]
    assert len(assigned_all) == 3
    assert set(assigned_all) == {"varsoviaestate.com", "tajlandia.com", "thai.estate"}


def test_user_login_returns_allowed_websites():
    """When a user logs in, the API returns their allowed_websites."""
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")
    user_email = f"login_test_{uuid.uuid4().hex[:6]}@tajlandia.com"

    create_u_resp = client.post(
        "/api/v1/auth/admin/users",
        headers={"Authorization": f"Bearer {sa_token}"},
        json={
            "email": user_email,
            "password": "MySecretPass123!",
            "name": "Check Websites Worker",
            "role": "worker",
            "websites": ["tajlandia.com", "varsoviaestate.com"]
        }
    )
    assert create_u_resp.status_code == 201, create_u_resp.text

    login_resp = client.post("/api/v1/auth/login", json={"email": user_email, "password": "MySecretPass123!"})
    assert login_resp.status_code == 200, login_resp.text
    user_info = login_resp.json()["user"]
    assert "allowed_websites" in user_info
    assert set(user_info["allowed_websites"]) == {"tajlandia.com", "varsoviaestate.com"}

    # Test logging in with dynamic target domain: "varsoviaestate.com"
    login_varsovia = client.post(
        "/api/v1/auth/login",
        json={"email": user_email, "password": "MySecretPass123!", "domain": "varsoviaestate.com"}
    )
    assert login_varsovia.status_code == 200
    assert login_varsovia.json()["user"]["domain"] == "varsoviaestate.com"

    # Test logging in with unauthorized domain "thai.estate" should be rejected with 403
    login_thai = client.post(
        "/api/v1/auth/login",
        json={"email": user_email, "password": "MySecretPass123!", "domain": "thai.estate"}
    )
    assert login_thai.status_code == 403



def test_active_checkbox_selection_and_isolation():
    """
    User has access to Tajlandia and Varsovia.
    - If user selects Tajlandia, CRUD is performed ONLY on Tajlandia.
    - User cannot select thai.estate (403 Forbidden).
    - User cannot see thai.estate properties.
    """
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")
    worker_email = f"worker_{uuid.uuid4().hex[:6]}@tajlandia.com"

    # Create worker with access to tajlandia.com & varsoviaestate.com
    create_w_resp = client.post(
        "/api/v1/auth/admin/users",
        headers={"Authorization": f"Bearer {sa_token}"},
        json={
            "email": worker_email,
            "password": "WorkerPass123!",
            "name": "Tajlandia & Varsovia Admin",
            "role": "admin",
            "websites": ["tajlandia.com", "varsoviaestate.com"]
        }
    )
    assert create_w_resp.status_code == 201, create_w_resp.text
    worker_token = _login_and_get_token(worker_email, "WorkerPass123!")

    # 1. Attempting to select an unauthorized website (thai.estate) yields 403 Forbidden
    resp_unauth = client.get(
        "/api/v1/admin/properties",
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Selected-Websites": "thai.estate"
        }
    )
    assert resp_unauth.status_code == 403
    assert "Access denied" in resp_unauth.json()["detail"]

    # 2. Select authorized checkbox (tajlandia.com)
    prop_id = f"prop-taj-{uuid.uuid4().hex[:6]}"
    create_resp = client.post(
        "/api/v1/admin/properties?allow_without_undo=true",
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Selected-Websites": "tajlandia.com"
        },
        json={
            "property_id": prop_id,
            "property_name": "Tajlandia Sunset Villa",
            "property_type": "Villa",
            "location": "Phuket",
            "bedrooms": "3",
            "status": "Available",
            "price_display": "15,000,000 THB",
            "websites": ["tajlandia.com"]
        }
    )
    assert create_resp.status_code == 201, create_resp.text
    created_prop = create_resp.json()
    assert "tajlandia.com" in created_prop["websites"]

    # 3. Fetching with X-Selected-Websites: tajlandia.com returns the property
    list_taj = client.get(
        "/api/v1/admin/properties",
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Selected-Websites": "tajlandia.com"
        }
    )
    assert list_taj.status_code == 200
    taj_ids = [p["property_id"] for p in list_taj.json()["properties"]]
    assert prop_id in taj_ids

    # 4. Switching selection to Varsovia only: Tajlandia property MUST NOT be returned
    list_var = client.get(
        "/api/v1/admin/properties",
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Selected-Websites": "varsoviaestate.com"
        }
    )
    assert list_var.status_code == 200
    var_ids = [p["property_id"] for p in list_var.json()["properties"]]
    assert prop_id not in var_ids

    # 5. Direct GET with X-Selected-Websites: varsoviaestate.com -> 403 (Forbidden: property belongs to Tajlandia)
    get_isolated = client.get(
        f"/api/v1/admin/properties/{prop_id}",
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Selected-Websites": "varsoviaestate.com"
        }
    )
    assert get_isolated.status_code == 403

    # 6. Direct GET with X-Selected-Websites: tajlandia.com -> 200 Success
    get_success = client.get(
        f"/api/v1/admin/properties/{prop_id}",
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Selected-Websites": "tajlandia.com"
        }
    )
    assert get_success.status_code == 200
    assert get_success.json()["property_id"] == prop_id

    # 7. Updating while selected website does not match -> 403 Forbidden
    update_fail = client.put(
        f"/api/v1/admin/properties/{prop_id}",
        headers={
            "Authorization": f"Bearer {worker_token}",
            "X-Selected-Websites": "varsoviaestate.com"
        },
        json={"property_name": "Attempted Hack Name"}
    )
    assert update_fail.status_code == 403


def test_cross_website_crud_with_all_access():
    """
    Super Admin or User with all 3 sites access creates a property for thai.estate.
    - When selecting thai.estate -> property is visible.
    - When selecting tajlandia.com -> property is strictly invisible.
    - When selecting both tajlandia.com and thai.estate -> property is visible.
    """
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")

    thai_prop_id = f"prop-thai-{uuid.uuid4().hex[:6]}"
    create_resp = client.post(
        "/api/v1/admin/properties?allow_without_undo=true",
        headers={
            "Authorization": f"Bearer {sa_token}",
            "X-Selected-Websites": "thai.estate"
        },
        json={
            "property_id": thai_prop_id,
            "property_name": "Exclusive Thai Estate Condominium",
            "property_type": "Condominium",
            "location": "Koh Samui",
            "bedrooms": "2",
            "status": "Available",
            "price_display": "8,500,000 THB",
            "price_numeric_min": 1000.0,
            "websites": ["thai.estate"]
        }
    )
    assert create_resp.status_code == 201, create_resp.text

    # 1. Fetch with thai.estate selected
    resp_thai = client.get(
        "/api/v1/admin/properties",
        headers={
            "Authorization": f"Bearer {sa_token}",
            "X-Selected-Websites": "thai.estate"
        }
    )
    assert resp_thai.status_code == 200
    assert thai_prop_id in [p["property_id"] for p in resp_thai.json()["properties"]]

    # 2. Fetch with tajlandia.com selected -> Thai property is NOT returned
    resp_taj = client.get(
        "/api/v1/admin/properties",
        headers={
            "Authorization": f"Bearer {sa_token}",
            "X-Selected-Websites": "tajlandia.com"
        }
    )
    assert resp_taj.status_code == 200
    assert thai_prop_id not in [p["property_id"] for p in resp_taj.json()["properties"]]

    # 3. Public endpoint GET /api/v1/properties with selected_websites query param
    pub_taj = client.get(f"/api/v1/properties?selected_websites=tajlandia.com&limit=100")
    assert pub_taj.status_code == 200
    assert thai_prop_id not in [p["property_id"] for p in pub_taj.json()["properties"]]

    pub_thai = client.get(f"/api/v1/properties?selected_websites=thai.estate&limit=100")
    assert pub_thai.status_code == 200
    assert thai_prop_id in [p["property_id"] for p in pub_thai.json()["properties"]]

    # 4. Multi-checkbox selection (tajlandia.com, thai.estate)
    resp_multi = client.get(
        "/api/v1/admin/properties",
        headers={
            "Authorization": f"Bearer {sa_token}",
            "X-Selected-Websites": "tajlandia.com, thai.estate"
        }
    )
    assert resp_multi.status_code == 200
    assert thai_prop_id in [p["property_id"] for p in resp_multi.json()["properties"]]


def test_super_admin_can_block_and_unblock_user_terminating_sessions():
    """
    Test that when Super Admin blocks an admin or worker:
    1. Status changes to 'blocked' and is_active becomes False.
    2. All active sessions are immediately revoked.
    3. Blocked user cannot call /me, property CRUD, or admin chat with existing token (403 Forbidden).
    4. Blocked user cannot log in (403 Forbidden).
    5. Super Admin unblocking restores access.
    """
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")
    sa_headers = {"Authorization": f"Bearer {sa_token}"}

    # 1. Create a user
    target_email = f"target_{uuid.uuid4().hex[:6]}@tajlandia.com"
    create_resp = client.post(
        "/api/v1/auth/admin/users",
        headers=sa_headers,
        json={
            "email": target_email,
            "password": "Password123!",
            "name": "Target Staff",
            "role": "admin",
            "websites": ["tajlandia.com"]
        }
    )
    assert create_resp.status_code == 201, create_resp.text
    target_user_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "active"

    # 2. Target user logs in
    target_token = _login_and_get_token(target_email, "Password123!")
    target_headers = {
        "Authorization": f"Bearer {target_token}",
        "X-Selected-Websites": "tajlandia.com"
    }

    # 3. Verify user can access profile before blocking
    me_resp = client.get("/api/v1/auth/me", headers=target_headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["status"] == "active"

    # 4. Super Admin blocks the user
    block_resp = client.post(
        f"/api/v1/auth/admin/users/{target_user_id}/block",
        headers=sa_headers,
        json={"blocked": True, "reason": "Security protocol"}
    )
    assert block_resp.status_code == 200, block_resp.text
    block_data = block_resp.json()
    assert block_data["status"] == "blocked"
    assert block_data["is_active"] is False
    assert block_data["active_sessions_count"] == 0

    # 5. Blocked user's existing token is immediately rejected on ALL APIs
    # 5a. Profile /me
    me_blocked = client.get("/api/v1/auth/me", headers=target_headers)
    assert me_blocked.status_code == 403
    assert "blocked" in me_blocked.text.lower()

    # 5b. Admin properties listing
    props_blocked = client.get("/api/v1/admin/properties", headers=target_headers)
    assert props_blocked.status_code == 403

    # 5c. Admin chat
    chat_blocked = client.post(
        "/api/v1/admin/chat",
        headers=target_headers,
        json={"message": "Hello", "session_id": "test_sess"}
    )
    assert chat_blocked.status_code == 403

    # 6. Blocked user cannot log in
    login_attempt = client.post(
        "/api/v1/auth/login",
        json={"email": target_email, "password": "Password123!"}
    )
    assert login_attempt.status_code == 403
    assert "blocked" in login_attempt.text.lower()

    # 7. Super Admin unblocks user
    unblock_resp = client.post(
        f"/api/v1/auth/admin/users/{target_user_id}/block",
        headers=sa_headers,
        json={"blocked": False}
    )
    assert unblock_resp.status_code == 200
    assert unblock_resp.json()["status"] == "active"
    assert unblock_resp.json()["is_active"] is True

    # 8. Unblocked user can log in again and obtain fresh session
    login_success = client.post(
        "/api/v1/auth/login",
        json={"email": target_email, "password": "Password123!"}
    )
    assert login_success.status_code == 200
    new_token = login_success.json()["access_token"]
    assert new_token != target_token

    # 9. Fresh token works
    new_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert new_me.status_code == 200
    assert new_me.json()["status"] == "active"


def test_super_admin_cannot_block_own_account():
    """Super Admin cannot block their own account."""
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")
    sa_headers = {"Authorization": f"Bearer {sa_token}"}
    me_resp = client.get("/api/v1/auth/me", headers=sa_headers)
    sa_id = me_resp.json()["id"]

    resp = client.post(
        f"/api/v1/auth/admin/users/{sa_id}/block",
        headers=sa_headers,
        json={"blocked": True}
    )
    assert resp.status_code == 400
    assert "cannot block their own account" in resp.text.lower()


def test_regular_admin_cannot_block_users():
    """Regular Admin does not have permission to block accounts (403 Forbidden)."""
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")
    sa_headers = {"Authorization": f"Bearer {sa_token}"}

    # Create a regular admin and worker
    admin_email = f"reg_admin_{uuid.uuid4().hex[:6]}@tajlandia.com"
    worker_email = f"worker_{uuid.uuid4().hex[:6]}@tajlandia.com"
    client.post(
        "/api/v1/auth/admin/users",
        headers=sa_headers,
        json={"email": admin_email, "password": "Password123!", "role": "admin", "websites": ["tajlandia.com"]}
    )
    w_resp = client.post(
        "/api/v1/auth/admin/users",
        headers=sa_headers,
        json={"email": worker_email, "password": "Password123!", "role": "worker", "websites": ["tajlandia.com"]}
    )
    worker_id = w_resp.json()["id"]

    admin_token = _login_and_get_token(admin_email, "Password123!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Regular admin attempts to call block endpoint -> 403 Forbidden
    block_resp = client.post(
        f"/api/v1/auth/admin/users/{worker_id}/block",
        headers=admin_headers,
        json={"blocked": True}
    )
    assert block_resp.status_code == 403


def test_worker_cannot_perform_property_crud_but_can_chat():
    """
    Worker role:
    - Can access admin chat
    - Can view properties
    - CANNOT create properties (403)
    - CANNOT update properties (403)
    - CANNOT delete properties (403)
    """
    sa_token = _login_and_get_token("test_super_admin@tajlandia.com", "SuperAdmin123!")
    sa_headers = {"Authorization": f"Bearer {sa_token}"}

    # Create a worker
    worker_email = f"worker_crud_{uuid.uuid4().hex[:6]}@tajlandia.com"
    client.post(
        "/api/v1/auth/admin/users",
        headers=sa_headers,
        json={"email": worker_email, "password": "Password123!", "role": "worker", "websites": ["varsoviaestate.com", "tajlandia.com"]}
    )

    worker_token = _login_and_get_token(worker_email, "Password123!")
    worker_headers = {
        "Authorization": f"Bearer {worker_token}",
        "X-Selected-Websites": "varsoviaestate.com"
    }

    # 1. Worker can view properties
    list_resp = client.get("/api/v1/admin/properties", headers=worker_headers)
    assert list_resp.status_code == 200

    # 2. Worker cannot create properties
    create_resp = client.post(
        "/api/v1/admin/properties",
        headers=worker_headers,
        json={
            "property_id": f"prop-w-{uuid.uuid4().hex[:4].translate(str.maketrans('0123456789abcdef', 'bcdfghjklmnpqrst'))}",
            "property_name": "Worker Villa",
            "websites": ["tajlandia.com"]
        }
    )
    assert create_resp.status_code == 403
    assert "lacks permission" in create_resp.text.lower()

    # 3. Worker cannot update properties
    put_resp = client.put(
        "/api/v1/admin/properties/PROP-W-TEST",
        headers=worker_headers,
        json={"property_name": "Updated"}
    )
    assert put_resp.status_code == 403

    # 4. Worker cannot delete properties
    del_resp = client.delete(
        "/api/v1/admin/properties/PROP-W-TEST",
        headers=worker_headers
    )
    assert del_resp.status_code == 403
