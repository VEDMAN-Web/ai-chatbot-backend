"""
Soft-delete lifecycle: a deleted (archived) listing stays in the database but is never served again.

Covers, with one shared in-memory Firestore (admin and user clients point at the same project in
production) and in-memory SQLite:
  * Scenario A - before deletion: admin + super admin can read it; the public chatbot tools return it.
  * Scenario B - after deletion: rows remain (flagged, with who/when), normal APIs hide it, chatbot
    retrieval (Firestore by id/name, SQL fallback, name index "did you mean", catalog cache) never
    returns it.
  * Scenario C - the same questions asked by super admin, admin and worker staff tools.
  * Batch delete + undo keeps the Postgres row (undo must restore, never hard-delete).
No live Postgres, Firestore or LLM is touched.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin import router as admin_router
from app.core.auth import AuthenticatedStaff, get_current_user_from_db
from app.core.config import settings
pytest.importorskip("app.db.models", reason="SQL-backed scenarios; Postgres was removed (port to FakeFirestore later)")
from app.db.models import Conversation, Property  # noqa: E402
from app.db.session import get_db  # noqa: E402
from test_admin_rest_crud_golden import FakeFirestore, _sqlite_session  # tests/ is on sys.path under pytest

ALL_SITES = ["tajlandia.com", "varsoviaestate.com", "thai.estate"]
NAME = "Zephyr Lagoon Residence"


def _staff(role: str, sites=None, email=None) -> AuthenticatedStaff:
    sites = list(sites or ALL_SITES)
    return AuthenticatedStaff(
        user_id=f"{role}-qa-001",
        email=email or f"{role}@tajlandia.com",
        role=role,
        allowed_websites=sites,
        selected_websites=sites,
    )


@pytest.fixture
def world(monkeypatch):
    from app.services import firestore_service, property_name_index

    session = _sqlite_session()
    fs = FakeFirestore()  # one project for admin + user, as configured in production
    monkeypatch.setattr("app.services.firestore_service.get_admin_firestore_client", lambda: fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda: fs)
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ADMIN_ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "USER_ENABLE_FIRESTORE_SYNC", True)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr(settings, "ENABLE_LEAD_SAVING", False)
    firestore_service.clear_property_catalog_cache()
    monkeypatch.setattr(property_name_index, "_INDEX", property_name_index.PropertyNameIndex())

    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")

    def _db():
        yield session

    holder = {"staff": _staff("super_admin")}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_from_db] = lambda: holder["staff"]
    client = TestClient(app)
    yield {"client": client, "db": session, "fs": fs, "holder": holder}
    app.dependency_overrides.clear()
    firestore_service.clear_property_catalog_cache()
    session.close()


def _as(world, role, sites=None):
    world["holder"]["staff"] = _staff(role, sites)


def _create(world, pid="zephyr-lagoon", name=NAME, sites=None):
    body = {
        "property_id": pid,
        "property_name": name,
        "property_type": "Villa",
        "province": "Phuket",
        "district": "Rawai",
        "price_numeric_min": 7400000,
        "status": "Available",
        "bedrooms": 3,
        "description": "Three bedroom pool villa near Rawai beach.",
    }
    if sites:
        body["websites"] = sites
    res = world["client"].post("/api/v1/admin/properties", json=body)
    assert res.status_code == 201, res.text
    return pid


def _conversation():
    return Conversation(session_id="qa-soft-delete", site_origin="tajlandia.com", preferences={})


def _chat_lookup(world, **args):
    """What the public chatbot's get_property_by_id tool hands to the LLM."""
    from app.services.public_agent import public_agent

    result, cards, _ = public_agent.execute_tool(world["db"], _conversation(), "get_property_by_id", dict(args))
    return result, cards


def _chat_named_search(world, query):
    from app.services.public_agent import public_agent

    result, cards, _ = public_agent.execute_tool(world["db"], _conversation(), "search_properties", {"query": query})
    return result, cards


def _leaks(name, payload) -> bool:
    return name.lower() in repr(payload).lower()


# ----------------------------------------------------------------------------- Scenario A + B + C


def test_deleted_listing_is_kept_flagged_and_never_served(world):
    from app.services.firestore_service import (
        fetch_properties_from_firestore,
        get_property_by_id_from_firestore,
    )
    from app.services.property_search import get_property_by_id, get_property_by_name

    pid = _create(world)

    # --- Scenario A: visible before deletion
    for role in ("super_admin", "admin"):
        _as(world, role)
        got = world["client"].get(f"/api/v1/admin/properties/{pid}")
        assert got.status_code == 200, (role, got.text)
        assert got.json()["property_name"] == NAME
    result, cards = _chat_lookup(world, property_id=pid)
    # the public card presenter shortens display names ("... Residence" -> "..."), by design
    assert result["found"] is True and result["property_id"] == pid and "Zephyr Lagoon" in result["name"]
    assert "7.40" in str(result["price"]) or "7,400,000" in str(result["price"]) or "7.4" in str(result["price"])
    result, cards = _chat_lookup(world, property_name=NAME)
    assert result["found"] is True and cards
    # warm the catalog cache so the deletion has to invalidate it
    assert any(p["property_id"] == pid for p in fetch_properties_from_firestore(limit=5000, target="user"))

    # --- delete as an admin
    _as(world, "admin")
    deleted = world["client"].delete(f"/api/v1/admin/properties/{pid}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "ARCHIVED"

    # rows remain, flagged, with who and when
    row = world["db"].query(Property).filter(Property.property_id == pid).first()
    assert row is not None
    assert row.is_public is False and row.status == "Archived"
    assert row.deleted_at is not None
    assert row.deleted_by == "admin@tajlandia.com"
    doc = world["fs"].get("projects", pid)
    assert doc is not None and doc["isActive"] is False and doc["_deletedBy"] == "admin@tajlandia.com"

    # --- Scenario B: normal APIs hide it
    for role in ("super_admin", "admin", "worker"):
        _as(world, role)
        assert world["client"].get(f"/api/v1/admin/properties/{pid}").status_code == 404, role
        listing = world["client"].get("/api/v1/admin/properties").json()
        assert all(p["property_id"] != pid for p in listing["properties"]), role

    # chatbot retrieval never returns it - by id, by name, by named search, via SQL fallback, via catalog
    for args in ({"property_id": pid}, {"property_name": NAME}, {"property_id": NAME.lower()}):
        result, cards = _chat_lookup(world, **args)
        assert result.get("found") is False, (args, result)
        assert not cards
        assert not _leaks("7400000", result) and not _leaks("Rawai", result)
    result, cards = _chat_named_search(world, NAME)
    assert not cards and all(p.get("property_id") != pid for p in result.get("properties", []))
    assert get_property_by_id_from_firestore(pid) is None
    assert get_property_by_id_from_firestore(NAME) is None
    assert get_property_by_id_from_firestore(pid, target="admin") is None
    assert get_property_by_id(world["db"], pid) is None
    assert get_property_by_name(world["db"], NAME) is None
    catalog = fetch_properties_from_firestore(limit=5000, target="user")
    assert all(p["property_id"] != pid for p in catalog)

    # "did you mean" suggestions must not reveal the archived name either
    result, _ = _chat_lookup(world, property_name="Zephyr Lagoon Residenc")
    assert not _leaks(NAME, result.get("name_suggestions") or [])


def test_admin_can_opt_in_to_view_archived_record_but_worker_cannot(world):
    pid = _create(world)
    world["client"].delete(f"/api/v1/admin/properties/{pid}")
    _as(world, "admin")
    got = world["client"].get(f"/api/v1/admin/properties/{pid}", params={"include_archived": "true"})
    assert got.status_code == 200, got.text
    assert got.json()["is_public"] is False
    _as(world, "worker")
    denied = world["client"].get(f"/api/v1/admin/properties/{pid}", params={"include_archived": "true"})
    assert denied.status_code == 403


def test_deleting_an_already_archived_listing_is_a_conflict(world):
    pid = _create(world)
    assert world["client"].delete(f"/api/v1/admin/properties/{pid}").status_code == 200
    again = world["client"].delete(f"/api/v1/admin/properties/{pid}")
    assert again.status_code == 409, again.text


def test_sql_only_archived_row_is_not_served_by_chatbot_fallback(world):
    from app.services.property_search import get_property_by_id, get_property_by_name

    db = world["db"]
    db.add(Property(property_id="sql-only-archived", property_name="Quartz Cove Villa", is_public=False,
                    status="Archived", province="Phuket", websites=["tajlandia.com"]))
    db.add(Property(property_id="sql-only-draft", property_name="Opal Cove Villa", is_public=False,
                    status="Available", province="Phuket", websites=["tajlandia.com"]))
    db.add(Property(property_id="sql-only-live", property_name="Amber Cove Villa", is_public=True,
                    status="Available", province="Phuket", websites=["tajlandia.com"]))
    db.commit()
    assert get_property_by_id(db, "sql-only-archived") is None
    assert get_property_by_name(db, "Quartz Cove") is None
    assert get_property_by_id(db, "sql-only-draft") is None  # unpublished drafts are not public either
    assert get_property_by_id(db, "sql-only-live").property_id == "sql-only-live"
    assert get_property_by_name(db, "Amber Cove").property_id == "sql-only-live"


def test_sql_fallback_defers_to_firestore_when_firestore_says_inactive(world):
    """Batch deletes used to deactivate Firestore only; the public SQL fallback must not resurrect them."""
    from app.services.property_search import get_property_by_id

    db = world["db"]
    db.add(Property(property_id="fs-inactive", property_name="Coral Ridge", is_public=True,
                    status="Available", province="Phuket", websites=["tajlandia.com"]))
    db.commit()
    world["fs"].set("projects", "fs-inactive", {"name": "Coral Ridge", "isActive": False,
                                                "_deletedAt": "2026-11-01T00:00:00+00:00"})
    assert get_property_by_id(db, "fs-inactive") is None


def test_staff_can_still_see_unpublished_drafts_but_public_cannot(world):
    from app.services.firestore_service import get_property_by_id_from_firestore

    world["fs"].set("projects", "draft-1", {"name": "Draft Harbour", "isActive": False})
    assert get_property_by_id_from_firestore("draft-1", target="admin") is not None
    assert get_property_by_id_from_firestore("draft-1", target="user") is None


def test_undo_restores_an_archived_listing_everywhere(world):
    from app.services.firestore_service import get_property_by_id_from_firestore

    pid = _create(world)
    op = world["client"].delete(f"/api/v1/admin/properties/{pid}").json()["operation_id"]
    undone = world["client"].post(f"/api/v1/admin/operations/{op}/undo")
    assert undone.status_code == 200, undone.text
    row = world["db"].query(Property).filter(Property.property_id == pid).first()
    world["db"].refresh(row)
    assert row.is_public is True and row.deleted_at is None and row.deleted_by is None
    assert get_property_by_id_from_firestore(pid) is not None
    assert world["client"].get(f"/api/v1/admin/properties/{pid}").status_code == 200


# ----------------------------------------------------------------------------- batch delete / undo


def test_batch_delete_archives_sql_and_undo_never_hard_deletes(world):
    from app.services.admin_batch_service import (
        execute_batch_property_operation,
        undo_batch_property_operation,
    )
    from app.services.property_search import get_property_by_id

    pid = _create(world, pid="batch-keep", name="Batch Keep Villa")
    staff = _staff("admin")
    out = execute_batch_property_operation(
        action_type="delete", records=[{"property_id": pid}], staff_user=staff,
        source_filename="qa.csv", db_session=world["db"], confirmed_by_staff=True,
    )
    row = world["db"].query(Property).filter(Property.property_id == pid).first()
    assert row is not None and row.is_public is False and row.status == "Archived"
    assert row.deleted_by == staff.email
    assert get_property_by_id(world["db"], pid) is None

    undo_batch_property_operation(out["operation_id"], staff, db_session=world["db"])
    world["db"].expire_all()
    row = world["db"].query(Property).filter(Property.property_id == pid).first()
    assert row is not None, "undo of a batch delete must not permanently delete the Postgres row"
    assert row.is_public is True and row.deleted_at is None


def test_batch_update_undo_keeps_postgres_row(world):
    from app.services.admin_batch_service import (
        execute_batch_property_operation,
        undo_batch_property_operation,
    )

    pid = _create(world, pid="batch-upd", name="Batch Update Villa")
    staff = _staff("admin")
    out = execute_batch_property_operation(
        action_type="update", records=[{"property_id": pid, "priceFrom": 8100000}], staff_user=staff,
        source_filename="qa.csv", db_session=world["db"], confirmed_by_staff=True,
    )
    undo_batch_property_operation(out["operation_id"], staff, db_session=world["db"])
    world["db"].expire_all()
    assert world["db"].query(Property).filter(Property.property_id == pid).first() is not None


def test_batch_delete_respects_tenant_scope_for_unassigned_listings(world):
    """A doc with no websites belongs to tajlandia.com only; a varsovia-only admin must not delete it."""
    from app.services.admin_batch_service import BatchOperationError, execute_batch_property_operation

    world["fs"].set("projects", "legacy-1", {"name": "Legacy Unassigned", "isActive": True})
    staff = _staff("admin", sites=["varsoviaestate.com"])
    with pytest.raises(BatchOperationError):
        execute_batch_property_operation(
            action_type="delete", records=[{"property_id": "legacy-1"}], staff_user=staff,
            source_filename="qa.csv", db_session=world["db"], confirmed_by_staff=True,
        )
    assert world["fs"].get("projects", "legacy-1")["isActive"] is True


def test_admin_list_paginates_and_filters_status(world):
    from app.services.firestore_service import clear_property_catalog_cache

    for i in range(5):
        _create(world, pid=f"page-{i}", name=f"Paging Villa {i}")
    clear_property_catalog_cache()
    c = world["client"]
    pages = [c.get("/api/v1/admin/properties", params={"limit": 2, "offset": off}).json()["properties"] for off in (0, 2, 4, 6)]
    ids = [p["property_id"] for page in pages for p in page]
    assert [len(p) for p in pages] == [2, 2, 1, 0]
    assert len(set(ids)) == 5
    sold = c.get("/api/v1/admin/properties", params={"status": "Sold Out"}).json()["properties"]
    assert sold == []


# ----------------------------------------------------------------------------- role restrictions


def test_worker_cannot_create_update_or_delete(world):
    pid = _create(world)
    _as(world, "worker")
    c = world["client"]
    assert c.post("/api/v1/admin/properties", json={"property_name": "X", "price_numeric_min": 1}).status_code == 403
    assert c.put(f"/api/v1/admin/properties/{pid}", json={"price_numeric_min": 1}).status_code == 403
    assert c.delete(f"/api/v1/admin/properties/{pid}").status_code == 403
    assert world["fs"].get("projects", pid)["isActive"] is True
    # read is allowed for staff
    assert c.get(f"/api/v1/admin/properties/{pid}").status_code == 200


def test_admin_cannot_delete_another_tenants_listing(world):
    pid = _create(world, sites=["varsoviaestate.com"])
    _as(world, "admin", sites=["thai.estate"])
    res = world["client"].delete(f"/api/v1/admin/properties/{pid}")
    assert res.status_code == 403
    assert world["fs"].get("projects", pid)["isActive"] is True
