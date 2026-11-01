"""Bug 7: admin list pages must not stream whole collections per request.

Seeds ~1000 users / log rows / conversation headers into the in-memory FakeFirestore and checks, through the fake's
read accounting, that the list endpoints read a bounded number of documents while returning the same pages and totals
as a full scan would.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.auth import AuthenticatedStaff
from app.core.config import settings
from app.core.tenant import ALL_CANONICAL_WEBSITES
from app.repositories import users_repo
from app.services import admin_logs_service as logs_svc
from app.services import chat_log_query as query
from tests import fs_users

client = TestClient(app)
PASSWORD = "PagePass123!"
N = 1000


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    logs_svc.reset_stats_cache()
    yield
    logs_svc.reset_stats_cache()


def _reads(fake, collection):
    return fake.stats["reads_by_collection"].get(collection, 0)


# ---- users directory ----------------------------------------------------------------------------

def _seed_users(fake, n=N):
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for i in range(n):
        email = f"bulk{i:04d}@tajlandia.com"
        uid = f"00000000-0000-4000-8000-{i:012d}"
        fake.collection("admin_authentication").document(email).set({
            "userId": uid, "email": email, "name": f"Bulk {i}", "role": "worker", "domain": "tajlandia.com",
            "allowed_websites": ["tajlandia.com"], "isActive": True, "status": "active",
            "createdAt": (base + timedelta(minutes=i)).isoformat(),
        })
        if i % 3 == 0:  # one active session
            fake.collection("admin_sessions").document(f"s-act-{i}").set(
                {"userId": uid, "tokenHash": f"s-act-{i}", "expiresAt": future, "revokedAt": None, "createdAt": past})
        if i % 5 == 0:  # an expired and a revoked session that must not count
            fake.collection("admin_sessions").document(f"s-exp-{i}").set(
                {"userId": uid, "tokenHash": f"s-exp-{i}", "expiresAt": past, "revokedAt": None, "createdAt": past})
            fake.collection("admin_sessions").document(f"s-rev-{i}").set(
                {"userId": uid, "tokenHash": f"s-rev-{i}", "expiresAt": future, "revokedAt": past, "createdAt": past})


@pytest.fixture
def seeded_users(_default_fake_firestore):
    fake = _default_fake_firestore
    _seed_users(fake)
    boss = fs_users.make_user(client, "super_admin", prefix="pg_", password=PASSWORD)
    return fake, boss


def test_authenticated_request_does_not_scan_all_users(seeded_users):
    fake, boss = seeded_users
    fake.reset_stats()
    resp = client.get("/api/v1/auth/me", headers=boss["headers"])
    assert resp.status_code == 200, resp.text
    # Resolving the session owner must be an indexed lookup, not a scan of ~1000 user documents.
    assert _reads(fake, "admin_authentication") <= 2, fake.stats


def test_get_user_by_id_is_an_indexed_lookup(seeded_users):
    fake, _ = seeded_users
    fake.reset_stats()
    rec = users_repo.get_by_id(None, "00000000-0000-4000-8000-000000000500")
    assert rec is not None and rec.email == "bulk0500@tajlandia.com"
    assert _reads(fake, "admin_authentication") <= 2, fake.stats


def test_get_user_by_id_still_finds_console_documents_without_user_id(seeded_users):
    fake, _ = seeded_users
    fake.collection("admin_authentication").document("legacy@tajlandia.com").set(
        {"email": "legacy@tajlandia.com", "role": "worker", "isActive": True})
    rec = users_repo.get_by_id(None, users_repo.stable_user_id("legacy@tajlandia.com"))
    assert rec is not None and rec.email == "legacy@tajlandia.com"


def test_users_list_counts_sessions_without_one_query_per_user(seeded_users):
    fake, boss = seeded_users
    fake.reset_stats()
    resp = client.get("/api/v1/auth/admin/users", headers=boss["headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) >= N
    # one query for the sessions, not ~1000
    assert fake.stats["streams"] <= 6, fake.stats
    assert _reads(fake, "admin_sessions") <= 400, fake.stats   # only unexpired sessions are read
    by_email = {it["email"]: it for it in items}
    assert by_email["bulk0000@tajlandia.com"]["active_sessions_count"] == 1   # active + expired + revoked
    assert by_email["bulk0001@tajlandia.com"]["active_sessions_count"] == 0
    assert by_email["bulk0003@tajlandia.com"]["active_sessions_count"] == 1
    assert by_email["bulk0005@tajlandia.com"]["active_sessions_count"] == 0   # expired + revoked only
    assert int(resp.headers["X-Total-Count"]) == len(items)


def test_users_list_optional_pagination_returns_correct_pages_and_total(seeded_users):
    fake, boss = seeded_users
    full = client.get("/api/v1/auth/admin/users", headers=boss["headers"]).json()
    page = client.get("/api/v1/auth/admin/users?limit=50&offset=100", headers=boss["headers"])
    assert page.status_code == 200, page.text
    assert [u["id"] for u in page.json()] == [u["id"] for u in full[100:150]]
    assert int(page.headers["X-Total-Count"]) == len(full)
    last = client.get(f"/api/v1/auth/admin/users?limit=50&offset={len(full) - 10}", headers=boss["headers"])
    assert [u["id"] for u in last.json()] == [u["id"] for u in full[-10:]]


def test_users_list_reflects_writes_immediately(seeded_users):
    fake, boss = seeded_users
    client.get("/api/v1/auth/admin/users", headers=boss["headers"])
    created = client.post("/api/v1/auth/admin/users", headers=boss["headers"], json={
        "email": "fresh_page_user@tajlandia.com", "password": "Fresh12345!", "role": "worker"})
    assert created.status_code == 201, created.text
    emails = {u["email"] for u in client.get("/api/v1/auth/admin/users", headers=boss["headers"]).json()}
    assert "fresh_page_user@tajlandia.com" in emails


