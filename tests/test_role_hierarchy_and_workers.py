import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal
from app.db.models import User, Property
from app.core.security import hash_password

client = TestClient(app)

@pytest.fixture(autouse=True)
def mock_firestore_for_tests(monkeypatch):
    """Disable live Firestore network calls during fast local test execution."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "get_admin_enable_firestore_sync", lambda: False)
    monkeypatch.setattr("app.services.action_service._fs_sync_enabled", lambda: False)
    monkeypatch.setattr("app.services.firestore_auth_service.get_firestore_client", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.admin_batch_service.get_firestore_client", lambda *a, **kw: None)
    with SessionLocal() as db:
        p = db.query(Property).filter(Property.property_id == "test-promoted-admin-01").first()
        if p:
            db.delete(p)
            db.commit()
    yield
    with SessionLocal() as db:
        p = db.query(Property).filter(Property.property_id == "test-promoted-admin-01").first()
        if p:
            db.delete(p)
            db.commit()


def _seed_test_accounts():
    """Ensure all requested test users are present in local test DB."""
    accounts = [
        ("superadmin@tajlandia.com", "Super Admin", "super_admin", "superAdmin@1234", ["varsoviaestate.com", "tajlandia.com", "thai.estate"]),
        ("admin_rbsc@tajlandia.com", "RBSC Admin", "admin", "admin321", ["tajlandia.com"]),
        ("admin@tajlandia.com", "Dual Site Admin", "admin", "Admin@1234", ["tajlandia.com", "varsoviaestate.com"]),
        ("worker01@tajlandia.com", "Worker 01", "worker", "worker01@123", ["tajlandia.com"]),
        ("worker02@tajlandia.com", "Worker 02", "worker", "worker02@123", ["tajlandia.com"]),
        ("worker03@tajlandia.com", "Worker 03", "worker", "worker03@123", ["tajlandia.com"]),
        ("worker04@tajlandia.com", "Worker 04", "worker", "worker04@123", ["tajlandia.com"]),
        ("worker05@tajlandia.com", "Worker 05", "worker", "worker05@123", ["tajlandia.com"]),
    ]
    with SessionLocal() as db:
        for email, name, role, pw, websites in accounts:
            u = db.query(User).filter(User.email.ilike(email)).first()
            if not u:
                u = User(
                    email=email.lower(),
                    name=name,
                    role=role,
                    domain="tajlandia.com",
                    password_hash=hash_password(pw),
                    allowed_websites=list(websites),
                    preferences={"allowed_websites": list(websites)},
                    is_active=True
                )
                db.add(u)
            else:
                u.name = name
                u.role = role
                u.password_hash = hash_password(pw)
                u.allowed_websites = list(websites)
                prefs = dict(u.preferences or {})
                prefs["allowed_websites"] = list(websites)
                u.preferences = prefs
                u.is_active = True
            db.commit()


def test_all_requested_users_login():
    """Verify that all 8 requested accounts authenticate and receive correct roles and website access."""
    _seed_test_accounts()

    credentials = [
        ("superadmin@tajlandia.com", "superAdmin@1234", "super_admin", {"varsoviaestate.com", "tajlandia.com", "thai.estate"}),
        ("admin_rbsc@tajlandia.com", "admin321", "admin", {"tajlandia.com"}),
        ("admin@tajlandia.com", "Admin@1234", "admin", {"tajlandia.com", "varsoviaestate.com"}),
        ("worker01@tajlandia.com", "worker01@123", "worker", {"tajlandia.com"}),
        ("worker02@tajlandia.com", "worker02@123", "worker", {"tajlandia.com"}),
        ("worker03@tajlandia.com", "worker03@123", "worker", {"tajlandia.com"}),
        ("worker04@tajlandia.com", "worker04@123", "worker", {"tajlandia.com"}),
        ("worker05@tajlandia.com", "worker05@123", "worker", {"tajlandia.com"}),
    ]

    for email, password, expected_role, expected_sites in credentials:
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, f"Login failed for {email}: {resp.text}"
        data = resp.json()
        assert "access_token" in data
        user = data["user"]
        assert user["email"] == email.lower()
        assert user["role"] == expected_role, f"Expected role {expected_role} for {email}, got {user['role']}"
        assert set(user["allowed_websites"]) == expected_sites, f"Websites mismatch for {email}"


def test_worker_cannot_create_update_delete_properties():
    """Verify that a worker can view properties and chat, but is strictly blocked from creating, updating, or deleting."""
    _seed_test_accounts()

    # Login as worker01
    w_resp = client.post("/api/v1/auth/login", json={"email": "worker01@tajlandia.com", "password": "worker01@123"})
    assert w_resp.status_code == 200
    token = w_resp.json()["access_token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Selected-Websites": "tajlandia.com"
    }

    # 1. Worker CAN view properties
    list_resp = client.get("/api/v1/admin/properties", headers=headers)
    assert list_resp.status_code == 200

    # 2. Worker CANNOT create properties (403 Forbidden)
    create_resp = client.post("/api/v1/admin/properties", headers=headers, json={
        "property_id": "TEST-WORKER-BLOCKED",
        "property_name": "Worker Should Not Create",
        "property_type": "Villa",
        "price_display": "10,000,000 THB",
        "bedrooms": 3,
        "bathrooms": 2,
        "websites": ["tajlandia.com"]
    })
    assert create_resp.status_code == 403, f"Worker creation should be 403, got: {create_resp.status_code}"

    # 3. Worker CANNOT update properties (403 Forbidden)
    update_resp = client.put("/api/v1/admin/properties/PROP-PHK-001", headers=headers, json={
        "property_name": "Worker Attempted Update"
    })
    assert update_resp.status_code == 403, f"Worker update should be 403, got: {update_resp.status_code}"

    # 4. Worker CANNOT delete properties (403 Forbidden)
    del_resp = client.delete("/api/v1/admin/properties/PROP-PHK-001", headers=headers)
    assert del_resp.status_code == 403, f"Worker delete should be 403, got: {del_resp.status_code}"


def test_admin_can_promote_worker_to_admin_with_all_websites():
    """Verify that an Admin / Super Admin can promote a worker to admin and grant full domain access."""
    _seed_test_accounts()

    # 1. Login as Super Admin
    sa_resp = client.post("/api/v1/auth/login", json={"email": "superadmin@tajlandia.com", "password": "superAdmin@1234"})
    assert sa_resp.status_code == 200
    sa_token = sa_resp.json()["access_token"]
    sa_headers = {"Authorization": f"Bearer {sa_token}"}

    # 2. Get worker01 user ID
    with SessionLocal() as db:
        w1 = db.query(User).filter(User.email == "worker01@tajlandia.com").first()
        w1_id = str(w1.id)

    # 3. Promote worker01 to admin with all 3 websites
    promote_resp = client.patch(
        f"/api/v1/auth/admin/users/{w1_id}",
        headers=sa_headers,
        json={
            "role": "admin",
            "websites": ["varsoviaestate.com", "tajlandia.com", "thai.estate"]
        }
    )
    assert promote_resp.status_code == 200, promote_resp.text
    promoted_data = promote_resp.json()
    assert promoted_data["role"] == "admin"
    assert set(promoted_data["allowed_websites"]) == {"varsoviaestate.com", "tajlandia.com", "thai.estate"}

    # 4. Worker01 logs in now as Admin
    w1_resp = client.post("/api/v1/auth/login", json={"email": "worker01@tajlandia.com", "password": "worker01@123"})
    assert w1_resp.status_code == 200
    w1_data = w1_resp.json()
    assert w1_data["user"]["role"] == "admin"
    assert len(w1_data["user"]["allowed_websites"]) == 3
    new_token = w1_data["access_token"]

    # 5. Worker01 (now Admin) CAN now create properties!
    admin_headers = {
        "Authorization": f"Bearer {new_token}",
        "X-Selected-Websites": "thai.estate"
    }
    create_as_admin = client.post("/api/v1/admin/properties?allow_without_undo=true", headers=admin_headers, json={
        "property_id": "test-promoted-admin-01",
        "property_name": "Promoted Admin Luxury Villa",
        "property_type": "Villa",
        "location": "Bang Tao, Phuket",
        "status": "Ready to move in",
        "price_display": "30,000,000 THB",
        "bedrooms": 4,
        "bathrooms": 4,
        "websites": ["thai.estate"]
    })
    assert create_as_admin.status_code == 201, create_as_admin.text
