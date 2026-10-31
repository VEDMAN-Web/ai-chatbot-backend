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
)


def ensure_postgres_schema(engines: Iterable) -> None:
    """Create every model table, then add columns that old databases may lack."""
    from app.db.session import Base
    import app.db.models  # noqa: F401  — registers tables on Base.metadata

    seen = []
    for eng in engines:
        if eng in seen:
            continue
        seen.append(eng)
        name = getattr(getattr(eng, "url", None), "database", None) or "database"
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
        print(f"    [OK] users and properties are ready on {name}.")
