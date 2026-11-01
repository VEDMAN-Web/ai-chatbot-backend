"""
Regression tests for soft delete enforcement, RAG integrity, cache invalidation,
and cross-role isolation (Super Admin, Admin, User/Public).

Verifies:
1. Normal delete operations never permanently hard-delete records.
2. Soft deletion flags (is_deleted, deleted_at, status=Archived, is_public=False) are set.
3. Database record remains preserved with is_deleted=True.
4. Complete exclusion across all retrieval paths:
   - Search queries (search_properties_safe, get_property_by_id, get_property_by_name)
   - PropertyNameIndex matching and auto-complete
   - Knowledge / RAG retrieval (get_property_knowledge)
   - In-memory cache (PropertySearchCache)
   - Admin Chat CRUD read commands
5. Verified across Super Admin, Admin, and User/Public contexts.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin import router as admin_router
from app.core.auth import AuthenticatedStaff, get_current_user_from_db
from app.core.config import settings
from app.db.models import Conversation, Property, PropertySpec, PropertyImage
from app.db.session import get_db
from test_admin_rest_crud_golden import FakeFirestore, _sqlite_session


ALL_SITES = ["tajlandia.com", "varsoviaestate.com", "thai.estate"]
NAME = "Sapphire Haven Residence"
PID = "sapphire-haven"


def _make_staff(role: str, sites=None, email=None) -> AuthenticatedStaff:
    sites = list(sites or ALL_SITES)
    return AuthenticatedStaff(
        user_id=f"{role}-qa-test",
        email=email or f"{role}@tajlandia.com",
        role=role,
        allowed_websites=sites,
        selected_websites=sites,
    )


@pytest.fixture
def test_env(monkeypatch):
    from app.services import firestore_service, property_name_index, cache_service

    session = _sqlite_session()
    bind = session.get_bind()
    for tbl in (PropertySpec.__table__, PropertyImage.__table__):
        tbl.create(bind=bind, checkfirst=True)
    fs = FakeFirestore()
    monkeypatch.setattr("app.services.firestore_service.get_admin_firestore_client", lambda: fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda: fs)
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "USER_ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr(settings, "ENABLE_LEAD_SAVING", False)

    firestore_service.clear_property_catalog_cache()
    cache_service.search_cache.invalidate()
    monkeypatch.setattr(property_name_index, "_INDEX", property_name_index.PropertyNameIndex())

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")

    def _db():
        yield session

    holder = {"staff": _make_staff("super_admin")}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_from_db] = lambda: holder["staff"]
    client = TestClient(app)

    yield {"client": client, "db": session, "fs": fs, "holder": holder}

    app.dependency_overrides.clear()
    firestore_service.clear_property_catalog_cache()
    cache_service.search_cache.invalidate()
    session.close()


def _set_role(env, role: str, sites=None):
    env["holder"]["staff"] = _make_staff(role, sites)


def _create_property(env, pid=PID, name=NAME, sites=None):
    body = {
        "property_id": pid,
        "property_name": name,
        "property_type": "Villa",
        "province": "Phuket",
        "district": "Bang Tao",
        "price_numeric_min": 18500000,
        "status": "Available",
        "bedrooms": 4,
        "description": "Luxurious 4 bedroom pool villa in Bang Tao.",
        "has_pool": True,
    }
    if sites:
        body["websites"] = sites
    res = env["client"].post("/api/v1/admin/properties", json=body)
    assert res.status_code == 201, res.text
    return pid


def test_soft_delete_full_workflow_across_roles(test_env):
    """
    Mandatory workflow verification:
      1. Create a test record.
      2. Retrieve it through the chatbot/search.
      3. Soft-delete it.
      4. Verify the database record still exists (is_deleted=True, deleted_at set).
      5. Verify the chatbot/search cannot retrieve it.
      6. Repeat using Super Admin, Admin, and User accounts.
      7. Verify caches cannot expose it.
    """
    from app.services.property_search import get_property_by_id, get_property_by_name, search_properties_safe, PropertyFilterParams
    from app.services.public_agent import public_agent
    from app.services.cache_service import search_cache
    from app.services.property_name_index import match_property_name

    db = test_env["db"]
    client = test_env["client"]

    # 1. Create test record
    pid = _create_property(test_env, pid=PID, name=NAME)

    # Populate cache
    cache_key = search_cache.make_cache_key({"location": "Phuket"})
    search_cache.set(cache_key, [{"property_id": pid, "name": NAME, "price": "18.5M THB"}])
    assert search_cache.get(cache_key) is not None

    # 2. Retrieve through chatbot and search prior to deletion
    # Via SQL
    active_row = get_property_by_id(db, pid)
    assert active_row is not None
    assert active_row.is_deleted is False

    # Via public chatbot agent
    conv = Conversation(session_id="rag-qa-soft-delete", site_origin="tajlandia.com", preferences={})
    res_chat, cards, _ = public_agent.execute_tool(db, conv, "get_property_by_id", {"property_id": pid})
    assert res_chat.get("found") is True
    assert res_chat.get("property_id") == pid

    # Via admin endpoints as Super Admin and Admin
    for role in ("super_admin", "admin"):
        _set_role(test_env, role)
        got = client.get(f"/api/v1/admin/properties/{pid}")
        assert got.status_code == 200
        assert got.json()["property_name"] == NAME

    # 3. Soft-delete it (as Admin)
    _set_role(test_env, "admin")
    del_res = client.delete(f"/api/v1/admin/properties/{pid}")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "ARCHIVED"

    # 4. Verify DB record STILL EXISTS, never permanently hard-deleted
    row_in_db = db.query(Property).filter(Property.property_id == pid).first()
    assert row_in_db is not None, "Record must NOT be permanently hard-deleted"
    assert row_in_db.is_deleted is True, "is_deleted flag must be True"
    assert row_in_db.deleted_at is not None, "deleted_at must be populated"
    assert row_in_db.status == "Archived"
    assert row_in_db.is_public is False

    # 5. Verify cache invalidation triggered
    assert search_cache.get(cache_key) is None, "Cache must be invalidated on soft delete"

    # 6. Verify exclusion across Super Admin, Admin, and User contexts
    # Context 6A: User / Public
    assert get_property_by_id(db, pid, staff=False) is None
    assert get_property_by_name(db, NAME, staff=False) is None
    public_results = search_properties_safe(db, PropertyFilterParams(location="Phuket"))
    assert all(p.property_id != pid for p in public_results)

    # Public chatbot cannot retrieve or find it
    res_chat_after, cards_after, _ = public_agent.execute_tool(db, conv, "get_property_by_id", {"property_id": pid})
    assert res_chat_after.get("found") is False
    assert not cards_after

    res_search_after, cards_search_after, _ = public_agent.execute_tool(db, conv, "search_properties", {"query": NAME})
    assert all(p.get("property_id") != pid for p in res_search_after.get("properties", []))
    assert not cards_search_after

    # Context 6B: Admin (normal endpoints return 404, list hides it)
    _set_role(test_env, "admin")
    assert client.get(f"/api/v1/admin/properties/{pid}").status_code == 404
    admin_list = client.get("/api/v1/admin/properties").json()
    assert all(p["property_id"] != pid for p in admin_list.get("properties", []))
    assert get_property_by_id(db, pid, staff=True) is None

    # Context 6C: Super Admin (normal endpoint returns 404)
    _set_role(test_env, "super_admin")
    assert client.get(f"/api/v1/admin/properties/{pid}").status_code == 404
    super_admin_list = client.get("/api/v1/admin/properties").json()
    assert all(p["property_id"] != pid for p in super_admin_list.get("properties", []))

    # Opt-in archived query for audit purposes works only when explicitly requested
    opt_in = client.get(f"/api/v1/admin/properties/{pid}", params={"include_archived": "true"})
    assert opt_in.status_code == 200
    assert opt_in.json()["is_public"] is False


def test_admin_knowledge_rag_excludes_soft_deleted(test_env):
    """Verify get_property_knowledge strictly excludes soft-deleted listings."""
    from app.services.admin_knowledge_service import get_property_knowledge

    db = test_env["db"]
    pid = _create_property(test_env, pid="knowledge-prop", name="Knowledge Haven Villa")

    # Attach specification
    db.add(PropertySpec(
        property_id=pid,
        roof_material="Clay Tile",
        flooring="Italian Marble",
        walls="Reinforced Concrete",
        internal_notes="VIP confidential investor pricing."
    ))
    db.commit()

    # Pre-deletion: knowledge is retrievable
    know_before = get_property_knowledge(db, pid, include_internal_notes=True)
    assert know_before is not None
    assert know_before["property_id"] == pid
    assert know_before["specifications"]["roof_material"] == "Clay Tile"
    assert know_before["internal_notes"] == "VIP confidential investor pricing."

    # Soft-delete the property
    _set_role(test_env, "admin")
    res_del = test_env["client"].delete(f"/api/v1/admin/properties/{pid}")
    assert res_del.status_code == 200

    # Post-deletion: get_property_knowledge returns None by default
    know_after = get_property_knowledge(db, pid, include_internal_notes=True)
    assert know_after is None, "RAG / Knowledge service must NEVER return soft-deleted records"

    # Explicit audit access with include_deleted=True
    know_audit = get_property_knowledge(db, pid, include_internal_notes=True, include_deleted=True)
    assert know_audit is not None
    assert know_audit["property_id"] == pid


def test_property_name_index_excludes_soft_deleted():
    """Verify PropertyNameIndex.load_from_rows filters soft-deleted rows and remove_entry works."""
    from app.services.property_name_index import PropertyNameIndex

    idx = PropertyNameIndex()
    rows = [
        {
            "doc_id": "active-1",
            "name": {"en": "Emerald Bay Villas"},
            "cityId": "phuket",
            "status": "Available",
            "is_public": True,
        },
        {
            "doc_id": "deleted-1",
            "name": {"en": "Ruby Coast Residence"},
            "cityId": "phuket",
            "status": "Archived",
            "deleted_at": "2026-11-01T00:00:00",
            "is_deleted": True,
        },
        {
            "doc_id": "deleted-2",
            "name": {"en": "Diamond Ridge Condos"},
            "cityId": "samui",
            "_deletedAt": "2026-11-01T00:00:00",
            "status": "Deleted",
        },
    ]

    idx.load_from_rows(rows)

    # Active listing is indexed
    match_active = idx.match("Emerald Bay Villas")
    assert match_active.confident is not None
    assert match_active.confident.doc_id == "active-1"

    # Soft-deleted listings are completely excluded
    match_del1 = idx.match("Ruby Coast Residence")
    assert match_del1.confident is None
    assert all(c.doc_id != "deleted-1" for c in match_del1.candidates)

    match_del2 = idx.match("Diamond Ridge Condos")
    assert match_del2.confident is None
    assert all(c.doc_id != "deleted-2" for c in match_del2.candidates)

    # Mention search also rejects deleted listings
    mention = idx.find_mention("What is the price of Ruby Coast Residence in Phuket?")
    assert mention.confident is None
    assert all(c.doc_id != "deleted-1" for c in mention.candidates)

    # Test remove_entry immediately removes an active listing
    idx.remove_entry("active-1")
    assert idx.get_entry("active-1") is None
    assert idx.match("Emerald Bay Villas").confident is None


def test_cache_service_sanitizes_and_evicts_deleted_records():
    """Verify PropertySearchCache evicts properties and never leaks soft-deleted items."""
    from app.services.cache_service import PropertySearchCache

    cache = PropertySearchCache(default_ttl_seconds=600)
    key1 = cache.make_cache_key({"location": "phuket"})
    key2 = cache.make_cache_key({"property_type": "villa"})

    # Setting records with a soft-deleted item filters it out automatically
    raw_data = [
        {"property_id": "live-1", "name": "Live Villa", "status": "Available"},
        {"property_id": "archived-1", "name": "Archived Villa", "status": "Archived", "is_deleted": True},
    ]
    cache.set(key1, raw_data)

    cached_results = cache.get(key1)
    assert cached_results is not None
    assert len(cached_results) == 1
    assert cached_results[0]["property_id"] == "live-1"

    # Test evict_property removes keys containing that property
    cache.set(key2, [{"property_id": "live-1", "name": "Live Villa"}])
    assert cache.get(key2) is not None
    evicted = cache.evict_property("live-1")
    assert evicted == 2
    assert cache.get(key1) is None
    assert cache.get(key2) is None


def test_admin_chat_crud_read_rejects_soft_deleted(test_env):
    """Verify resolve_listing_for_read rejects soft-deleted properties."""
    from app.services.admin_chat_crud import resolve_listing_for_read, propose_archive

    db = test_env["db"]
    pid = _create_property(test_env, pid="chat-crud-test", name="Breeze Point Villas")

    # Before deletion
    assert resolve_listing_for_read(db, "Show Breeze Point Villas") == pid

    # Delete property
    _set_role(test_env, "admin")
    test_env["client"].delete(f"/api/v1/admin/properties/{pid}")

    # After deletion: resolve_listing_for_read returns None
    assert resolve_listing_for_read(db, "Show Breeze Point Villas") is None

    # Staging an archive on an already-archived property returns already_archived status
    staff = test_env["holder"]["staff"]
    staged = propose_archive(db, staff, staff.email, "conv-1", pid, ALL_SITES)
    assert staged["ok"] is False
    assert staged["status"] == "already_archived"


def test_property_model_is_deleted_hybrid_property():
    """Verify Property.is_deleted hybrid property getters, setters, and SQL expressions."""
    from app.db.models import Property
    from test_admin_rest_crud_golden import _sqlite_session

    db = _sqlite_session()
    prop = Property(
        property_id="hybrid-prop",
        property_name="Hybrid Test Villa",
        status="Available",
        is_public=True
    )
    db.add(prop)
    db.commit()

    # Initial state
    assert prop.is_deleted is False

    # Set via is_deleted property
    prop.is_deleted = True
    db.commit()
    assert prop.is_deleted is True
    assert prop.status == "Archived"
    assert prop.is_public is False
    assert prop.deleted_at is not None

    # Query via SQL expression
    active_in_sql = db.query(Property).filter(Property.is_deleted == False).all()
    deleted_in_sql = db.query(Property).filter(Property.is_deleted == True).all()
    assert all(p.property_id != "hybrid-prop" for p in active_in_sql)
    assert any(p.property_id == "hybrid-prop" for p in deleted_in_sql)

    # Restore via is_deleted = False
    prop.is_deleted = False
    db.commit()
    assert prop.is_deleted is False
    assert prop.status == "Available"
    assert prop.is_public is True
    assert prop.deleted_at is None

    active_restored = db.query(Property).filter(Property.is_deleted == False).all()
    assert any(p.property_id == "hybrid-prop" for p in active_restored)

    db.close()
