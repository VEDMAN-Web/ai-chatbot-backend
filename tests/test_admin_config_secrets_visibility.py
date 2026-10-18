"""
TASK 3 — Secrets and flags visibility (config.py / admin config-status).
"""

from __future__ import annotations

import inspect
import logging
from unittest.mock import patch

import pytest

from app.core import config as config_mod
from app.core.config import (
    Settings,
    assert_database_configured_or_exit,
    get_admin_config_status,
    log_config_warnings,
)
from app.services.admin_agent import redact_admin_sensitive_output


def test_config_default_has_no_real_password():
    src = inspect.getsource(Settings)
    # Hardcoded production-style password must be gone
    assert "xyz123" not in src
    assert "xyz123%4024" not in src
    # Field default is empty (env/conftest supply test URLs)
    field_default = Settings.model_fields["DATABASE_URL"].default
    assert field_default == ""


def test_fail_fast_when_database_url_empty_outside_test(monkeypatch):
    monkeypatch.setattr(config_mod, "is_test_environment", lambda: False)
    with patch.object(config_mod.settings, "DATABASE_URL", ""):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            assert_database_configured_or_exit()


def test_no_fail_fast_in_test_env(monkeypatch):
    monkeypatch.setattr(config_mod, "is_test_environment", lambda: True)
    with patch.object(config_mod.settings, "DATABASE_URL", ""):
        assert_database_configured_or_exit()  # must not raise


def test_log_config_warnings_each_case(caplog, monkeypatch):
    monkeypatch.setattr(config_mod.settings, "ENABLE_AUDIT_LOGGING", False)
    monkeypatch.setattr(config_mod.settings, "ENABLE_LEAD_SAVING", False)
    monkeypatch.setattr(
        config_mod.settings,
        "GOOGLE_REDIRECT_URI",
        "https://web-production-935b27.up.railway.app/api/v1/auth/google/callback",
    )
    monkeypatch.setattr(config_mod.settings, "BASE_WEBSITE_URL", "https://varsoviaestate.com/en")
    monkeypatch.setattr(config_mod.settings, "ADMIN_FIRESTORE_PROJECT_ID", None)
    monkeypatch.setattr(config_mod.settings, "INTERNAL_API_SECRET", "tajlandia-internal-secret-token")
    monkeypatch.setattr(config_mod.settings, "SECRET_KEY", "")
    monkeypatch.setenv("K_SERVICE", "sparkbeta-api")

    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        log_config_warnings()

    messages = " | ".join(r.message for r in caplog.records)
    assert "Admin audit logging is OFF" in messages
    assert "Lead saving is OFF" in messages
    assert "railway.app" in messages.lower()
    assert "ADMIN_FIRESTORE" in messages
    assert "weak" in messages.lower() or "default" in messages.lower()
    # Never leak secrets into logs
    assert "tajlandia-internal-secret-token" not in messages
    assert "password" not in messages.lower() or "weak" in messages.lower()


def test_config_status_no_secrets_and_shape():
    status = get_admin_config_status()
    assert set(status.keys()) >= {
        "audit_logging",
        "lead_saving",
        "firestore_project_shared",
        "google_redirect_host",
        "database_configured",
        "report_storage",
    }
    assert isinstance(status["audit_logging"], bool)
    assert isinstance(status["lead_saving"], bool)
    assert isinstance(status["firestore_project_shared"], bool)
    assert isinstance(status["database_configured"], bool)
    assert isinstance(status["google_redirect_host"], str)
    assert status["report_storage"] in ("ephemeral", "s3", "persistent")
    blob = str(status).lower()
    assert "password" not in blob
    assert "://postgres" not in blob
    assert "xyz123" not in blob
    assert "api_key" not in blob
    assert "@" not in status["google_redirect_host"]  # hostname only
    assert "/" not in status["google_redirect_host"]


def test_config_status_endpoint_requires_admin():
    """Endpoint is wired to require_admin_or_super_admin (not public)."""
    import inspect
    from app.api.v1.admin import admin_config_status_endpoint
    from app.core.auth import require_admin_or_super_admin

    sig = inspect.signature(admin_config_status_endpoint)
    staff_param = sig.parameters["staff"]
    dep = staff_param.default
    dependency = getattr(dep, "dependency", None)
    assert dependency is require_admin_or_super_admin


def test_redact_masks_database_url_with_credentials():
    dirty = "connect with postgresql://postgres:xyz123%4024@127.0.0.1:5432/tajlandia_db please"
    clean = redact_admin_sensitive_output(dirty)
    assert "xyz123" not in clean
    assert "REDACTED" in clean
    assert "postgresql://postgres:xyz123" not in clean
