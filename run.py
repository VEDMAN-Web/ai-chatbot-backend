import os
import sys
import time
import subprocess
import uvicorn

# Ensure repository root is on sys.path
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

def _env_flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def sql_startup_enabled():
    """SQL_STARTUP=false skips Postgres schema sync, alembic and catalog seeding (Firestore-only deploys)."""
    return _env_flag("SQL_STARTUP", True)


def alembic_on_start_enabled():
    """RUN_ALEMBIC_ON_START=false keeps schema sync but never runs alembic upgrade/stamp."""
    return _env_flag("RUN_ALEMBIC_ON_START", True)


def init_database():
    """Initializes tables and extensions with retries for cloud databases."""
    import sqlalchemy as sa
    from app.db.session import admin_engine, user_engine, Base
    import app.db.models  # Ensure all SQLAlchemy models are registered

    print("--> 1. Connecting to PostgreSQL and creating schema...")
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            for eng in {admin_engine, user_engine}:
                with eng.connect() as conn:
                    try:
                        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector;"))
                        conn.commit()
                        print(f"    [OK] pgvector extension checked/enabled on {eng.url.database or 'database'}.")
                    except Exception as ve:
                        print(f"    [NOTICE] pgvector extension: {ve}")

            from app.db.schema_sync import ensure_postgres_schema
            ensure_postgres_schema((admin_engine, user_engine))
            print("    [OK] All database tables successfully created for admin and user databases.")

            # 3. Upgrade alembic migrations to head (skippable)
            if not alembic_on_start_enabled():
                print("    [OK] RUN_ALEMBIC_ON_START=false: alembic upgrade skipped.")
                return True
            try:
                from alembic.config import Config
                from alembic import command
                alembic_cfg = Config(os.path.join(BASE_DIR, "alembic.ini"))
                command.upgrade(alembic_cfg, "head")
                print("    [OK] Alembic migration upgraded to head.")
            except Exception as ae:
                msg = str(ae)
                print(f"    [NOTICE] Alembic upgrade: {ae}")
                # Railway DB may reference a deleted revision id — re-stamp to current head
                if "Can't locate revision" in msg or "Can't locate revision identified by" in msg:
                    try:
                        from alembic.config import Config
                        from alembic import command
                        alembic_cfg = Config(os.path.join(BASE_DIR, "alembic.ini"))
                        command.stamp(alembic_cfg, "head")
                        print("    [OK] Alembic version stamped to head after missing-revision recovery.")
                    except Exception as stamp_e:
                        print(f"    [NOTICE] Alembic stamp recovery: {stamp_e}")

            return True
        except Exception as e:
            print(f"    [ATTEMPT {attempt}/{max_retries}] Database not ready yet: {e}")
            if attempt < max_retries:
                time.sleep(3)
    return False

def seed_data():
    """Seeds property catalog from excel/json."""
    print("--> 2. Seeding property catalog...")
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = BASE_DIR
        res = subprocess.run([sys.executable, os.path.join(BASE_DIR, "scripts", "ingest_properties.py")], env=env, check=False)
        if res.returncode == 0:
            print("    [OK] Properties successfully ingested.")
        else:
            print(f"    [NOTICE] Ingestion finished with code {res.returncode}")
    except Exception as e:
        print(f"    [WARNING] Ingestion error: {e}")

def main():
    if not sql_startup_enabled():
        print("--> [OK] SQL_STARTUP=false: skipping PostgreSQL schema sync, alembic and seeding (Firestore-only).")
        db_ok = False
    else:
        db_ok = init_database()
    if sql_startup_enabled() and db_ok:
        seed_data()
    elif sql_startup_enabled():
        print("--> [NOTICE] Skipping local PostgreSQL seeding (PostgreSQL not reachable or using remote Firestore).")

    # Railway injects PORT and routes the public URL to that port.
    raw_port = os.environ.get("PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError:
        port = 8000

    print(f"--> 3. Starting Uvicorn server on host 0.0.0.0 port {port}...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )


if __name__ == "__main__":
    main()
