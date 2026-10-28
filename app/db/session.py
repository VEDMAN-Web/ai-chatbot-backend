import os
import logging
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from app.core.config import settings

logger = logging.getLogger(__name__)

Base = declarative_base()


def _normalize_db_url(url: str) -> str:
    if isinstance(url, str):
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
            return url.replace("postgresql://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql+psycopg://"):
            return url.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)
    return url


def _create_db_engine(raw_url: str, label: str):
    url = _normalize_db_url(raw_url)
    try:
        eng = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20
        )
        logger.info(f"Initialized {label} database engine.")
        return eng
    except Exception as e:
        logger.warning(f"Could not connect to {label} PostgreSQL ({e}). Using local SQLite cache.")
        sqlite_url = f"sqlite:///./data/local_{label}_cache.db"
        os.makedirs("data", exist_ok=True)
        return create_engine(sqlite_url, connect_args={"check_same_thread": False})


# 1. Admin Database (Staff, RBAC, Sessions, Audits, Admin Properties CRUD)
admin_engine = _create_db_engine(settings.get_admin_db_url(), "admin")
AdminSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=admin_engine)

# 2. User Database (Public Customer Chatbot, Leads, Inquiries, Property Catalog Search)
user_engine = _create_db_engine(settings.get_user_db_url(), "user")
UserSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=user_engine)

# Backward-compatibility aliases
engine = admin_engine
SessionLocal = AdminSessionLocal


def get_admin_db() -> Generator[Session, None, None]:
    """Provides a database session connected to the dedicated Admin Database (ADMIN_DATABASE_URL)."""
    global AdminSessionLocal, admin_engine
    try:
        db = AdminSessionLocal()
        db.connection()
    except Exception:
        sqlite_url = "sqlite:///./data/local_admin_cache.db"
        os.makedirs("data", exist_ok=True)
        admin_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=admin_engine)
        AdminSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=admin_engine)
        db = AdminSessionLocal()

    try:
        yield db
    finally:
        db.close()


def get_user_db() -> Generator[Session, None, None]:
    """Provides a database session connected to the dedicated User / Public Database (USER_DATABASE_URL)."""
    global UserSessionLocal, user_engine
    try:
        db = UserSessionLocal()
        db.connection()
    except Exception:
        sqlite_url = "sqlite:///./data/local_user_cache.db"
        os.makedirs("data", exist_ok=True)
        user_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=user_engine)
        UserSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=user_engine)
        db = UserSessionLocal()

    try:
        yield db
    finally:
        db.close()


def get_db(target: str = "admin") -> Generator[Session, None, None]:
    """
    Universal database dependency.
    Defaults to get_admin_db() for staff / admin operations, or routes to get_user_db() when requested.
    """
    if target == "user":
        yield from get_user_db()
    else:
        yield from get_admin_db()
