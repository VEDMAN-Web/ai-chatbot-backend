import os
import sys
import time
import subprocess
import uvicorn

# Ensure repository root is on sys.path
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

def init_database():
    """Initializes tables and extensions with retries for cloud databases."""
    import sqlalchemy as sa
    from app.db.session import engine, Base
    import app.db.models  # Ensure all SQLAlchemy models are registered

    print("--> 1. Connecting to PostgreSQL and creating schema...")
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            with engine.connect() as conn:
                try:
                    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector;"))
                    conn.commit()
                    print("    [OK] pgvector extension checked/enabled.")
                except Exception as ve:
                    print(f"    [NOTICE] pgvector extension: {ve}")

            # 1. Safe DDL synchronization for existing tables
            with engine.begin() as conn:
                conn.execute(sa.text("""
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS google_email VARCHAR(255);
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS google_refresh_token TEXT;
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_websites JSONB DEFAULT '[]'::jsonb;
                    ALTER TABLE properties ADD COLUMN IF NOT EXISTS websites JSONB DEFAULT '[]'::jsonb;
                """))
                print("    [OK] Checked and synchronized users & properties table columns.")

            # 2. Ensure all tables exist
            Base.metadata.create_all(bind=engine)
            print("    [OK] All database tables successfully created.")

            # 3. Upgrade alembic migrations to head
            try:
                from alembic.config import Config
                from alembic import command
                alembic_cfg = Config(os.path.join(BASE_DIR, "alembic.ini"))
                command.upgrade(alembic_cfg, "head")
                print("    [OK] Alembic migration upgraded to head.")
            except Exception as ae:
                print(f"    [NOTICE] Alembic upgrade: {ae}")

            # 4. Seed/verify admin@tajlandia.com in PostgreSQL
            try:
                from app.db.session import SessionLocal
                from app.db.models import User
                from app.core.config import settings
                from app.core.security import hash_password
                with SessionLocal() as db:
                    admin = db.query(User).filter(User.email.ilike(settings.ADMIN_EMAIL)).first()
                    if not admin:
                        admin = User(
                            email=settings.ADMIN_EMAIL.lower(),
                            name="Admin Staff",
                            role="super_admin",
                            password_hash=hash_password(settings.ADMIN_PASSWORD),
                            is_active=True
                        )
                        db.add(admin)
                        db.commit()
                        print(f"    [OK] Seeded super admin account '{settings.ADMIN_EMAIL}' in database.")
                    else:
                        admin.password_hash = hash_password(settings.ADMIN_PASSWORD)
                        admin.is_active = True
                        admin.role = "super_admin"
                        db.commit()
                        print(f"    [OK] Synchronized super admin password in database.")
            except Exception as se:
                print(f"    [NOTICE] Admin seed: {se}")

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
    db_ok = init_database()
    if db_ok:
        seed_data()
    else:
        print("--> [NOTICE] Skipping local PostgreSQL seeding (PostgreSQL not reachable or using remote Firestore).")

    # Determine port from Railway environment
    raw_port = os.environ.get("PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError:
        port = 8000

    #print(f"--> 3. Starting Uvicorn server on host 0.0.0.0 port {port}...")
    #uvicorn.run("app.main:app", host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")

    # Start Uvicorn on port 8000
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
