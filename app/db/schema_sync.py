"""Create application tables before adding columns to older databases."""

from __future__ import annotations

from typing import Iterable

import sqlalchemy as sa

USER_COLUMN_STATEMENTS = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_email VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_refresh_token TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_websites JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'active'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS domain VARCHAR(100) DEFAULT 'tajlandia.com'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences JSONB DEFAULT '{}'::jsonb",
)
PROPERTY_COLUMN_STATEMENTS = (
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS websites JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS project_name_pl VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS project_name_en VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS slug_pl VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS slug_en VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS subtitle_pl VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS subtitle_en VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS city VARCHAR(100)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS expected_roi VARCHAR(50)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS rental_yield VARCHAR(50)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS completion_status VARCHAR(100)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS completion_date VARCHAR(100)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS available_units INTEGER",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS short_description_pl TEXT",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS description_pl TEXT",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS short_description_en TEXT",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS description_en TEXT",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS video_url VARCHAR(1024)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS search_location VARCHAR(500)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS address_en VARCHAR(500)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS address_pl VARCHAR(500)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS neighborhood_lifestyle TEXT",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS layouts JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS seo_title_pl VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS seo_title_en VARCHAR(255)",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS seo_description_pl TEXT",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS seo_description_en TEXT",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS is_featured BOOLEAN DEFAULT FALSE",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS is_top_list BOOLEAN DEFAULT FALSE",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255)",
    "CREATE INDEX IF NOT EXISTS ix_properties_deleted_at ON properties (deleted_at)",
)

PERFORMANCE_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_properties_created_at ON properties (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_properties_updated_at ON properties (updated_at)",
    "CREATE INDEX IF NOT EXISTS ix_properties_public_deleted_status ON properties (is_public, deleted_at, status)",
    "CREATE INDEX IF NOT EXISTS ix_properties_public_deleted_created ON properties (is_public, deleted_at, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_property_images_prop_sort ON property_images (property_id, sort_order)",
    "CREATE INDEX IF NOT EXISTS ix_document_chunks_source_type ON document_chunks (source_type)",
    "CREATE INDEX IF NOT EXISTS ix_document_chunks_prop_internal ON document_chunks (property_id, is_internal_only)",
    "CREATE INDEX IF NOT EXISTS ix_users_is_active ON users (is_active)",
    "CREATE INDEX IF NOT EXISTS ix_users_status ON users (status)",
    "CREATE INDEX IF NOT EXISTS ix_users_created_at ON users (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_user_sessions_revoked_at ON user_sessions (revoked_at)",
    "CREATE INDEX IF NOT EXISTS ix_user_sessions_token_expires ON user_sessions (token, expires_at, revoked_at)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_site_origin ON conversations (site_origin)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_agent_type ON conversations (agent_type)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_user_id ON conversations (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_created_at ON conversations (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_site_created ON conversations (site_origin, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_leads_contact_phone ON leads (contact_phone)",
    "CREATE INDEX IF NOT EXISTS ix_leads_created_at ON leads (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_leads_status_created ON leads (status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_pending_actions_created_at ON pending_actions (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_pending_actions_status_expires ON pending_actions (status, expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_pending_actions_staff_status ON pending_actions (staff_user_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_audit_logs_staff_created ON audit_logs (staff_user_id, created_at)",
)


SQLITE_PROPERTY_COLUMNS = (
    ("deleted_at", "DATETIME"),
    ("deleted_by", "VARCHAR(255)"),
)


def ensure_sqlite_fallback_columns(engine) -> None:
    """The local SQLite fallback cache only gets create_all, which never adds columns to an existing table."""
    try:
        with engine.begin() as conn:
            cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(properties)"))}
            if not cols:
                return
            for name, ddl in SQLITE_PROPERTY_COLUMNS:
                if name not in cols:
                    conn.execute(sa.text(f"ALTER TABLE properties ADD COLUMN {name} {ddl}"))
    except Exception as exc:  # never block the fallback session
        print(f"SQLite fallback column check skipped: {exc}")


def ensure_postgres_schema(engines: Iterable) -> None:
    """Create every model table, then add columns and performance indexes that old databases may lack."""
    from app.db.session import Base
    import app.db.models  # noqa: F401  — registers tables on Base.metadata

    seen = []
    for eng in engines:
        if eng in seen:
            continue
        seen.append(eng)
        name = getattr(getattr(eng, "url", None), "database", None) or "database"

        # SQLite fallback engine support
        if getattr(eng, "dialect", None) and eng.dialect.name != "postgresql":
            Base.metadata.create_all(bind=eng)
            ensure_sqlite_fallback_columns(eng)
            print(f"    [OK] SQLite fallback tables and columns ready on {name}.")
            continue

        print(f"    [OK] Creating {len(Base.metadata.tables)} tables on {name}.")
        Base.metadata.create_all(bind=eng)
        with eng.begin() as conn:
            users = conn.execute(sa.text("SELECT to_regclass('public.users')")).scalar()
            properties = conn.execute(sa.text("SELECT to_regclass('public.properties')")).scalar()
            if not users:
                present = [row[0] for row in conn.execute(sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                ))]
                raise RuntimeError(
                    "users was not created. public tables: " + (", ".join(present) or "(none)")
                )
            for statement in USER_COLUMN_STATEMENTS:
                conn.execute(sa.text(statement))
            if properties:
                for statement in PROPERTY_COLUMN_STATEMENTS:
                    conn.execute(sa.text(statement))
            for statement in PERFORMANCE_INDEX_STATEMENTS:
                try:
                    conn.execute(sa.text(statement))
                except Exception as idx_err:
                    print(f"    [WARN] Index statement skipped ({statement}): {idx_err}")
        print(f"    [OK] users, properties, and performance indexes are ready on {name}.")
