import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.db.session import get_admin_db, get_user_db, admin_engine, user_engine
from app.services.firestore_service import (
    get_admin_firestore_client,
    get_user_firestore_client,
    get_firestore_client,
    check_firestore_health
)


def test_dual_database_engine_resolution():
    """Verify admin and user database engines resolve independently."""
    assert admin_engine is not None
    assert user_engine is not None
    assert settings.get_admin_db_url() is not None
    assert settings.get_user_db_url() is not None


def test_custom_env_dual_database_urls():
    """Verify custom environment variables override the fallback URL."""
    with patch.object(settings, "ADMIN_DATABASE_URL", "postgresql://admin_usr:pass1@localhost:5432/admin_db"):
        with patch.object(settings, "USER_DATABASE_URL", "postgresql://user_usr:pass2@localhost:5432/user_db"):
            assert "admin_db" in settings.get_admin_db_url()
            assert "user_db" in settings.get_user_db_url()


def test_custom_env_dual_firestore_settings():
    """Verify admin vs user firestore configurations resolve independently."""
    with patch.object(settings, "ADMIN_FIRESTORE_PROJECT_ID", "tajlandia-admin-project"):
        with patch.object(settings, "USER_FIRESTORE_PROJECT_ID", "tajlandia-user-project"):
            assert settings.get_admin_firestore_project_id() == "tajlandia-admin-project"
            assert settings.get_user_firestore_project_id() == "tajlandia-user-project"

    with patch.object(settings, "ADMIN_FIRESTORE_DATABASE", "admin_database"):
        with patch.object(settings, "USER_FIRESTORE_DATABASE", "user_database"):
            assert settings.get_admin_firestore_database() == "admin_database"
            assert settings.get_user_firestore_database() == "user_database"

    with patch.object(settings, "ADMIN_FIREBASE_SERVICE_ACCOUNT_KEY", '{"project_id": "admin-sa"}'):
        with patch.object(settings, "USER_FIREBASE_SERVICE_ACCOUNT_KEY", '{"project_id": "user-sa"}'):
            assert settings.get_admin_firebase_service_account_key() == '{"project_id": "admin-sa"}'
            assert settings.get_user_firebase_service_account_key() == '{"project_id": "user-sa"}'


def test_health_check_reports_both_databases_and_firestore():
    """Verify /health endpoint returns health status for both admin and user databases."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "admin_database" in data
    assert "user_database" in data
    assert data["admin_database"]["connected"] is True
    assert data["user_database"]["connected"] is True
    assert "firestore" in data
