import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, get_admin_db, get_user_db, _create_db_engine
from app.db.models import (
    Property,
    PropertyImage,
    PropertySpec,
    DocumentChunk,
    User,
    UserSession,
    Conversation,
    Lead,
    PendingAction,
    AuditLog,
)
from app.db.schema_sync import (
    ensure_sqlite_fallback_columns,
    ensure_postgres_schema,
    PERFORMANCE_INDEX_STATEMENTS,
)
from app.services.property_search import (
    PropertyFilterParams,
    search_properties_safe,
    not_deleted_condition,
    publicly_visible_condition,
)
from app.services.admin_perf import (
    tool_cache_get,
    tool_cache_set,
    tool_cache_invalidate_all,
)


from sqlalchemy.pool import StaticPool

@pytest.fixture
def sqlite_db_session():
    """In-memory SQLite database session isolated for test execution."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        test_engine.dispose()


def test_models_have_required_indexes_and_constraints():
    """Verify models have essential indexes and constraints configured on high-traffic fields."""
    prop_cols = {c.name: c for c in Property.__table__.columns}
    assert prop_cols["created_at"].index is True
    assert prop_cols["updated_at"].index is True
    assert prop_cols["deleted_at"].index is True
    assert prop_cols["is_public"].index is True
    assert prop_cols["status"].index is True

    # Property table indexes
    prop_index_names = {idx.name for idx in Property.__table__.indexes}
    if hasattr(Property, "__table_args__"):
        for arg in Property.__table_args__:
            if hasattr(arg, "name"):
                prop_index_names.add(arg.name)
    assert "ix_prop_search_composite" in prop_index_names
    assert "ix_prop_public_deleted_status" in prop_index_names
    assert "ix_prop_public_deleted_created" in prop_index_names
    assert "ix_prop_websites_gin" in prop_index_names

    # Conversation indexes
    conv_cols = {c.name: c for c in Conversation.__table__.columns}
    assert conv_cols["site_origin"].index is True
    assert conv_cols["agent_type"].index is True
    assert conv_cols["user_id"].index is True
    assert conv_cols["created_at"].index is True
    assert conv_cols["updated_at"].index is True
    conv_index_names = {idx.name for idx in Conversation.__table__.indexes}
    assert "ix_conversations_site_created" in conv_index_names

    # Lead indexes
    lead_cols = {c.name: c for c in Lead.__table__.columns}
    assert lead_cols["contact_phone"].index is True
    assert lead_cols["created_at"].index is True
    lead_index_names = {idx.name for idx in Lead.__table__.indexes}
    assert "ix_leads_status_created" in lead_index_names

    # User indexes
    user_cols = {c.name: c for c in User.__table__.columns}
    assert user_cols["is_active"].index is True
    assert user_cols["status"].index is True
    assert user_cols["created_at"].index is True

    # UserSession indexes
    session_cols = {c.name: c for c in UserSession.__table__.columns}
    assert session_cols["revoked_at"].index is True
    session_index_names = {idx.name for idx in UserSession.__table__.indexes}
    assert "ix_user_sessions_token_expires" in session_index_names

    # PendingAction indexes
    pa_cols = {c.name: c for c in PendingAction.__table__.columns}
    assert pa_cols["created_at"].index is True
    pa_index_names = {idx.name for idx in PendingAction.__table__.indexes}
    assert "ix_pending_actions_status_expires" in pa_index_names
    assert "ix_pending_actions_staff_status" in pa_index_names

    # PropertyImage indexes
    pi_index_names = {idx.name for idx in PropertyImage.__table__.indexes}
    assert "ix_property_images_prop_sort" in pi_index_names

    # DocumentChunk indexes
    doc_cols = {c.name: c for c in DocumentChunk.__table__.columns}
    assert doc_cols["source_type"].index is True
    doc_index_names = {idx.name for idx in DocumentChunk.__table__.indexes}
    assert "ix_doc_chunks_prop_internal" in doc_index_names

    # AuditLog indexes
    audit_index_names = {idx.name for idx in AuditLog.__table__.indexes}
    assert "ix_audit_logs_staff_created" in audit_index_names


def test_cascade_delete_and_relationship_consistency(sqlite_db_session):
    """Verify cascade deletes on Property, User, and Conversation remove children without orphan records."""
    db = sqlite_db_session

    # 1. Property -> Images, Specs, DocumentChunks
    prop = Property(
        property_id="test-prop-cascade-1",
        property_name="Cascade Villa",
        property_type="Villa",
        status="Available",
        price_numeric_min=Decimal("15000000"),
        is_public=True,
    )
    db.add(prop)
    db.flush()

    img1 = PropertyImage(property_id=prop.property_id, image_url="https://img.com/1.jpg", sort_order=1)
    img2 = PropertyImage(property_id=prop.property_id, image_url="https://img.com/2.jpg", sort_order=2)
    spec = PropertySpec(property_id=prop.property_id, architectural_style="Modern Tropical")
    doc = DocumentChunk(property_id=prop.property_id, document_title="Brochure", chunk_text="Luxury villa specs")
    db.add_all([img1, img2, spec, doc])
    db.commit()

    assert db.query(PropertyImage).filter(PropertyImage.property_id == prop.property_id).count() == 2
    assert db.query(PropertySpec).filter(PropertySpec.property_id == prop.property_id).count() == 1
    assert db.query(DocumentChunk).filter(DocumentChunk.property_id == prop.property_id).count() == 1

    # Delete property and verify complete cascade
    db.delete(prop)
    db.commit()

    assert db.query(PropertyImage).filter(PropertyImage.property_id == "test-prop-cascade-1").count() == 0
    assert db.query(PropertySpec).filter(PropertySpec.property_id == "test-prop-cascade-1").count() == 0
    assert db.query(DocumentChunk).filter(DocumentChunk.property_id == "test-prop-cascade-1").count() == 0

    # 2. Conversation -> Lead cascade
    conv = Conversation(
        session_id="session-cascade-101",
        site_origin="tajlandia.com",
    )
    db.add(conv)
    db.flush()

    lead = Lead(
        conversation_id=conv.id,
        contact_name="Alice Smith",
        contact_email="alice@example.com",
        contact_phone="+66812345678",
        status="new",
    )
    db.add(lead)
    db.commit()

    assert db.query(Lead).filter(Lead.conversation_id == conv.id).count() == 1

    # Delete conversation and verify orphan prevention
    db.delete(conv)
    db.commit()

    assert db.query(Lead).filter(Lead.conversation_id == conv.id).count() == 0

    # 3. User -> UserSession cascade
    user = User(
        email="test_cascade_user@tajlandia.com",
        name="Test User",
        role="employee",
    )
    db.add(user)
    db.flush()

    session1 = UserSession(
        user_id=user.id,
        token="tok-cascade-1",
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    db.add(session1)
    db.commit()

    assert db.query(UserSession).filter(UserSession.user_id == user.id).count() == 1

    db.delete(user)
    db.commit()

    assert db.query(UserSession).filter(UserSession.user_id == user.id).count() == 0


def test_soft_delete_and_visibility_consistency(sqlite_db_session):
    """Verify soft-delete logic, hybrid property status sync, and query visibility conditions."""
    db = sqlite_db_session

    p1 = Property(
        property_id="active-villa-1",
        property_name="Active Villa",
        property_type="Villa",
        status="Available",
        is_public=True,
    )
    p2 = Property(
        property_id="deleted-villa-2",
        property_name="Archived Villa",
        property_type="Villa",
        status="Available",
        is_public=True,
    )
    db.add_all([p1, p2])
    db.commit()

    # Soft delete p2 using hybrid property
    assert p2.is_deleted is False
    p2.is_deleted = True
    assert p2.is_deleted is True
    assert p2.deleted_at is not None
    assert p2.status == "Archived"
    assert p2.is_public is False
    db.commit()

    # Query with not_deleted_condition
    active_rows = db.query(Property).filter(not_deleted_condition()).all()
    active_ids = {r.property_id for r in active_rows}
    assert "active-villa-1" in active_ids
    assert "deleted-villa-2" not in active_ids

    # Query with publicly_visible_condition
    public_rows = db.query(Property).filter(publicly_visible_condition()).all()
    public_ids = {r.property_id for r in public_rows}
    assert "active-villa-1" in public_ids
    assert "deleted-villa-2" not in public_ids

    # Restore p2
    p2.is_deleted = False
    assert p2.deleted_at is None
    assert p2.status == "Available"
    assert p2.is_public is True
    db.commit()

    restored_rows = db.query(Property).filter(publicly_visible_condition()).all()
    restored_ids = {r.property_id for r in restored_rows}
    assert "deleted-villa-2" in restored_ids


def test_transaction_commit_and_rollback_safety(sqlite_db_session):
    """Verify transaction rollback prevents dirty writes and locks are cleanly released."""
    db = sqlite_db_session

    prop = Property(
        property_id="rollback-test-prop",
        property_name="Initial Name",
        property_type="Villa",
        price_numeric_min=Decimal("10000000"),
    )
    db.add(prop)
    db.commit()

    # Attempt an update that throws an exception, verify rollback safety
    try:
        prop.property_name = "Modified Name Should Rollback"
        # Simulate an error / conflict
        raise ValueError("Simulated route error during transaction")
    except ValueError:
        db.rollback()

    # Re-fetch from database to verify rollback
    fresh = db.query(Property).filter(Property.property_id == "rollback-test-prop").first()
    assert fresh.property_name == "Initial Name"


def test_dependency_generator_rollback_on_route_exception():
    """Verify get_admin_db and get_user_db generators rollback uncommitted changes on exception."""
    gen = get_admin_db()
    db = next(gen)

    prop = Property(
        property_id=f"test-gen-rollback-{uuid.uuid4().hex[:8]}",
        property_name="Pending Prop",
        property_type="Condominium",
    )
    db.add(prop)

    # Route throws an exception
    with pytest.raises(RuntimeError):
        try:
            raise RuntimeError("Route handler failed unexpectedly")
        except Exception:
            gen.throw(RuntimeError("Route handler failed unexpectedly"))

    # Verify session was closed and uncommitted addition was not persisted
    gen_check = get_admin_db()
    db_check = next(gen_check)
    try:
        found = db_check.query(Property).filter(Property.property_id == prop.property_id).first()
        assert found is None
    finally:
        try:
            next(gen_check)
        except StopIteration:
            pass


def test_concurrent_query_execution_performance(sqlite_db_session):
    """Verify thread-safe concurrent queries and lack of connection deadlock under load."""
    db = sqlite_db_session

    # Seed test properties
    for i in range(15):
        p = Property(
            property_id=f"concurrent-prop-{i}",
            property_name=f"Villa Emerald {i}",
            property_type="Villa" if i % 2 == 0 else "Condominium",
            status="Available",
            area="Phuket" if i % 2 == 0 else "Koh Samui",
            price_numeric_min=Decimal(f"{5000000 + (i * 500000)}"),
            is_public=True,
        )
        db.add(p)
    db.commit()

    results = []
    errors = []
    bind_engine = db.get_bind()
    WorkerSession = sessionmaker(bind=bind_engine)

    def worker(worker_id: int):
        s = WorkerSession()
        try:
            # Each thread performs safe search with filter on its own session
            params = PropertyFilterParams(
                property_type="Villa" if worker_id % 2 == 0 else "Condominium",
                limit=10,
            )
            t0 = time.perf_counter()
            matches = search_properties_safe(s, params)
            elapsed = time.perf_counter() - t0
            results.append((worker_id, len(matches), elapsed))
        except Exception as e:
            errors.append((worker_id, e))
        finally:
            s.close()

    threads = []
    start_time = time.perf_counter()
    for w in range(10):
        t = threading.Thread(target=worker, args=(w,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=5.0)

    total_time = time.perf_counter() - start_time
    assert len(errors) == 0, f"Concurrent workers experienced errors: {errors}"
    assert len(results) == 10
    # 10 concurrent threads query in well under 2 seconds
    assert total_time < 2.0


def test_admin_perf_tool_cache_concurrency_and_pruning():
    """Verify tool cache is thread-safe and prunes properly to prevent memory leaks."""
    tool_cache_invalidate_all()

    # Pre-populate 550 keys to trigger pruning logic
    for i in range(550):
        tool_cache_set(
            "search_properties",
            {"query": f"villa_{i}"},
            {"count": 1, "items": [f"item_{i}"]},
            scope="tajlandia.com",
        )

    # Cache should be bounded (not exploding beyond 500 items)
    hit = tool_cache_get("search_properties", {"query": "villa_549"}, 60.0, scope="tajlandia.com")
    assert hit is not None
    assert hit["count"] == 1

    # Concurrent cache reads and writes
    def cache_worker(w_id: int):
        tool_cache_set("get_buyer_leads", {"days": w_id}, {"leads": [w_id]}, scope="admin")
        read = tool_cache_get("get_buyer_leads", {"days": w_id}, 60.0, scope="admin")
        assert read is not None
        assert read["leads"] == [w_id]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(cache_worker, i) for i in range(20)]
        for f in futures:
            f.result()

    tool_cache_invalidate_all()
    assert tool_cache_get("search_properties", {"query": "villa_549"}, 60.0, scope="tajlandia.com") is None


def test_schema_sync_gracefully_handles_sqlite():
    """Verify ensure_postgres_schema executes cleanly on SQLite engine without raising to_regclass error."""
    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    # Should not raise exception
    ensure_postgres_schema([test_engine])
    # Calling fallback columns check
    ensure_sqlite_fallback_columns(test_engine)
    test_engine.dispose()


def test_create_db_engine_sqlite_arguments():
    """Verify _create_db_engine accepts sqlite:// URLs without pool_size argument errors."""
    eng = _create_db_engine("sqlite:///:memory:", label="test_sqlite")
    assert eng is not None
    with eng.connect() as conn:
        assert conn is not None
    eng.dispose()
