import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings

logger = logging.getLogger(__name__)

db_url = settings.DATABASE_URL
if isinstance(db_url, str):
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    elif db_url.startswith("postgresql+psycopg://"):
        db_url = db_url.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)

try:
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
except Exception as e:
    logger.warning(f"Could not connect to PostgreSQL ({e}). Using local SQLite database.")
    sqlite_url = "sqlite:///./data/local_cache.db"
    os.makedirs("data", exist_ok=True)
    engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    global SessionLocal
    try:
        db = SessionLocal()
        # Verify connection is live
        db.connection()
    except Exception:
        # If PostgreSQL fails on request (connection refused), cleanly fall back to local sqlite
        sqlite_url = "sqlite:///./data/local_cache.db"
        os.makedirs("data", exist_ok=True)
        local_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=local_engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=local_engine)
        db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
