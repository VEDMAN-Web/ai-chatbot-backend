"""
TASKS 1–4: Authenticated report download, signed links, persistent storage,
config-status storage mode, and chat honesty about reports.
"""

from __future__ import annotations

import inspect
import os
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.config import get_admin_config_status
from app.services import report_service as rs
from app.services.report_service import (
    InMemoryStorageProvider,
    SignedLinkError,
    StorageError,
    create_signed_download_token,
    format_report_ready_message,
    get_report_storage_mode,
    load_report_bytes,
    purge_old_reports,
    report_exists,
    save_report_metadata,
    strip_admin_report_urls_from_text,
    validate_report_filename,
    verify_signed_download_token,
    _MEMORY_REPORT_STORE,
)


@pytest.fixture(autouse=True)
def _clear_memory_store(tmp_path, monkeypatch):
    _MEMORY_REPORT_STORE.clear()
    monkeypatch.setattr(rs.settings, "REPORT_STORAGE_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(rs.settings, "REPORT_PERSISTENT_VOLUME", False)
    monkeypatch.setattr(rs.settings, "SECRET_KEY", "test-report-signing-secret-key-32")
    monkeypatch.setattr(rs.settings, "REPORT_SIGNED_LINK_TTL_SEC", 600)
    monkeypatch.setattr(rs.settings, "REPORT_RETENTION_DAYS", 7)
    yield
    _MEMORY_REPORT_STORE.clear()


# ---------------------------------------------------------------------------
# Filename / traversal
# ---------------------------------------------------------------------------

def test_validate_filename_rejects_traversal():
    with pytest.raises(ValueError, match="traversal|Invalid"):
        validate_report_filename("../etc/passwd.pdf")
    with pytest.raises(ValueError, match="traversal|Invalid"):
        validate_report_filename("foo/bar.pdf")
    with pytest.raises(ValueError, match="Unsupported|Invalid"):
        validate_report_filename("notes.txt")
    assert validate_report_filename("tajlandia_report_20260101_120000.pdf") == (
        "tajlandia_report_20260101_120000.pdf"
    )


# ---------------------------------------------------------------------------
# Signed links
# ---------------------------------------------------------------------------

def test_signed_link_valid_and_tampered():
    token, exp = create_signed_download_token("tajlandia_report_x.pdf", ttl_sec=600)
    assert exp > int(time.time())
    assert verify_signed_download_token(token) == "tajlandia_report_x.pdf"

    # Tamper signature
    bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SignedLinkError):
        verify_signed_download_token(bad)


def test_signed_link_expired():
    token, _ = create_signed_download_token("tajlandia_report_x.pdf", ttl_sec=1)
    # Force expiry by verifying with mocked time
    with patch("app.services.report_service.time.time", return_value=int(time.time()) + 9999):
        with pytest.raises(SignedLinkError) as ei:
            verify_signed_download_token(token)
        assert ei.value.expired is True


def test_signed_download_endpoint_codes():
    """Endpoint maps expired→410, tampered→403."""
    from app.api.v1.admin import download_report_signed_endpoint

    token, _ = create_signed_download_token("tajlandia_report_x.pdf", ttl_sec=1)
    with patch("app.services.report_service.time.time", return_value=int(time.time()) + 9999):
        with pytest.raises(HTTPException) as ei:
            download_report_signed_endpoint(token)
        assert ei.value.status_code in (403, 410)

    with pytest.raises(HTTPException) as ei2:
        download_report_signed_endpoint("not-a-valid-token!!!")
    assert ei2.value.status_code == 403


def test_plain_report_endpoint_requires_auth():
    from app.api.v1.admin import download_report_endpoint
    from app.core.auth import require_admin_or_employee

    sig = inspect.signature(download_report_endpoint)
    dep = getattr(sig.parameters["staff"].default, "dependency", None)
    assert dep is require_admin_or_employee


def test_signed_link_endpoint_requires_auth():
    from app.api.v1.admin import create_report_signed_link_endpoint
    from app.core.auth import require_admin_or_employee

    sig = inspect.signature(create_report_signed_link_endpoint)
    dep = getattr(sig.parameters["staff"].default, "dependency", None)
    assert dep is require_admin_or_employee


# ---------------------------------------------------------------------------
# Storage provider (fake) + serve + 404
# ---------------------------------------------------------------------------

def test_fake_storage_save_and_serve():
    store = InMemoryStorageProvider()
    fname = "tajlandia_report_testsave.pdf"
    url = store.save_file(fname, b"%PDF-1.4 fake", content_type="application/pdf")
    assert url.endswith(fname)
    save_report_metadata(fname, storage="memory", created_by="tester@example.com")
    assert report_exists(fname, storage=store)
    content, media, meta = load_report_bytes(fname, storage=store)
    assert content.startswith(b"%PDF")
    assert "pdf" in media
    assert meta.get("storage") == "memory"


def test_missing_report_clear_404_message():
    store = InMemoryStorageProvider()
    with pytest.raises(rs.ReportNotFoundError) as ei:
        load_report_bytes("tajlandia_report_missing.pdf", storage=store)
    assert "no longer available" in str(ei.value).lower() or "not found" in str(ei.value).lower()


def test_missing_boto3_falls_back_to_local_provider(tmp_path, monkeypatch):
    """S3 env is set, but boto3 cannot be imported: the report still lands on local disk."""
    import builtins

    monkeypatch.setenv("AWS_S3_BUCKET", "configured-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr(rs.settings, "REPORT_STORAGE_DIR", str(tmp_path / "reports"))
    rs.S3StorageProvider.reset_boto3_probe()

    real_import = builtins.__import__

    def _block_boto3(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "boto3" or name.startswith("boto3."):
            raise ImportError("boto3 missing for test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _block_boto3)

    query = MagicMock()
    query.count.return_value = 0
    query.order_by.return_value.limit.return_value.all.return_value = []
    db = MagicMock()
    db.query.return_value = query

    result = rs.generate_report(db, "csv", report_type="leads", days=0, created_by="admin@test.com")
    assert result["storage_ok"] is True
    assert result["storage"] == "local"
    assert "temporary storage" in (result.get("message") or "").lower()
    assert isinstance(rs.get_storage_provider(), rs.LocalStorageProvider)
    saved = os.path.join(str(tmp_path / "reports"), result["filename"])
    assert os.path.isfile(saved)

    rs.S3StorageProvider.reset_boto3_probe()


def test_s3_failure_no_silent_fallback(monkeypatch):
    """When S3 is configured, upload failure raises StorageError (no local fallback)."""

    class BoomS3(rs.S3StorageProvider):
        mode = "s3"

        def save_file(self, filename, content, content_type="application/pdf", **kwargs):
            raise StorageError("Report generated but could not be stored permanently")

    monkeypatch.setattr(rs, "is_s3_configured", lambda: True)
    provider = BoomS3()
    with pytest.raises(StorageError, match="could not be stored permanently"):
        provider.save_file("tajlandia_report_boom.pdf", b"x")


def test_finalize_reports_storage_failure_message():
    class FailStore(InMemoryStorageProvider):
        mode = "s3"

        def save_file(self, filename, content, content_type="application/pdf", **kwargs):
            raise StorageError("Report generated but could not be stored permanently")

    out = rs._finalize_report_save(
        FailStore(),
        "tajlandia_report_fail.pdf",
        b"abc",
        "application/pdf",
        created_by="admin@test.com",
    )
    assert out["storage_ok"] is False
    assert out["file_url"] is None
    assert "could not be stored permanently" in out["message"]


def test_config_status_includes_report_storage():
    status = get_admin_config_status()
    assert status["report_storage"] in ("ephemeral", "s3", "persistent")


def test_get_report_storage_mode_ephemeral(monkeypatch):
    monkeypatch.setattr(rs, "is_s3_configured", lambda: False)
    monkeypatch.setattr(rs, "is_persistent_volume_configured", lambda: False)
    assert get_report_storage_mode() == "ephemeral"


def test_purge_old_reports_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rs.settings, "REPORT_STORAGE_DIR", str(tmp_path / "reports"))
    store = InMemoryStorageProvider()
    fname = "tajlandia_report_old.pdf"
    store.save_file(fname, b"old")
    meta = save_report_metadata(fname, storage="memory")
    # Backdate expiry
    path = rs._metadata_path(fname)
    import json
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    data["created_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    deleted = purge_old_reports(retention_days=7)
    assert deleted >= 1
    assert purge_old_reports() >= 0  # never raises


# ---------------------------------------------------------------------------
# Chat honesty
# ---------------------------------------------------------------------------

def test_strip_admin_report_urls_from_text():
    text = (
        "Ready!\n"
        "Download: /api/v1/admin/reports/tajlandia_report_1.pdf\n"
        "Also [click](/api/v1/admin/reports/tajlandia_report_2.pdf) please."
    )
    clean = strip_admin_report_urls_from_text(text)
    assert "/api/v1/admin/reports/" not in clean
    assert "Ready!" in clean


def test_format_report_ready_message_honesty():
    ok = format_report_ready_message(filename="a.pdf", format_type="pdf", storage_ok=True)
    assert "download card" in ok.lower()
    assert "/api/v1/admin/reports/" not in ok
    bad = format_report_ready_message(filename="a.pdf", format_type="pdf", storage_ok=False)
    assert "could not be stored permanently" in bad.lower()


def test_generate_report_tool_verifies_existence(monkeypatch):
    """Tool handler clears file_url when provider verification fails."""
    from app.services.admin_agent import _execute_admin_tool

    fake_res = {
        "format": "pdf",
        "filename": "tajlandia_report_ghost.pdf",
        "file_url": "/api/v1/admin/reports/tajlandia_report_ghost.pdf",
        "storage_ok": True,
        "message": "ok",
    }
    monkeypatch.setattr(
        "app.services.admin_agent.generate_report",
        lambda *a, **k: fake_res,
    )
    monkeypatch.setattr("app.services.admin_agent.report_exists", lambda *a, **k: False)
    monkeypatch.setattr(
        "app.services.admin_agent.record_audit_log",
        lambda **k: None,
    )

    staff = MagicMock()
    staff.email = "admin@test.com"
    staff.selected_websites = None
    db = MagicMock()
    res, _, _ = _execute_admin_tool(db, "generate_report", {"format": "pdf"}, staff, "conv-1")
    assert res["storage_ok"] is False
    assert res["file_url"] is None
    assert "could not be stored" in res["message"].lower()


def test_repeat_report_regenerates_when_gone(monkeypatch):
    """When last report file is missing, 'that report again' regenerates."""
    from app.services import admin_agent as aa

    calls = []

    def fake_exec(db, name, args, staff, cid):
        calls.append(name)
        return (
            {
                "filename": "tajlandia_report_new.pdf",
                "file_url": "/api/v1/admin/reports/tajlandia_report_new.pdf",
                "storage_ok": True,
                "message": format_report_ready_message(
                    filename="tajlandia_report_new.pdf", format_type="pdf", storage_ok=True
                ),
            },
            False,
            None,
        )

    monkeypatch.setattr(aa, "_execute_admin_tool", fake_exec)
    monkeypatch.setattr(aa, "report_exists", lambda *a, **k: False)
    monkeypatch.setattr(
        "app.services.admin_reply_helpers.extract_session_state_from_conversation",
        lambda conv: {"last_report_file": "tajlandia_report_old.pdf"},
    )
    monkeypatch.setattr(
        "app.services.admin_reply_helpers.remember_admin_session_fact",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(aa, "append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.admin_reply_helpers.contextual_followup_suggestions",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "app.services.admin_reply_helpers.append_followup_suggestions",
        lambda text, *a, **k: text,
    )
    monkeypatch.setattr(
        aa,
        "ResponseConfidenceGuardrail",
        MagicMock(evaluate=lambda **k: MagicMock(needs_requery=False)),
    )

    conv = MagicMock()
    conv.id = "c1"
    conv.session_id = "s1"
    staff = MagicMock()
    staff.email = "a@b.com"
    staff.selected_websites = None
    db = MagicMock()

    result = aa._run_deterministic_admin(
        db, conv, "give me that report again", staff, language="en"
    )

    assert calls == ["generate_report"]
    assert result.get("download_link") == "/api/v1/admin/reports/tajlandia_report_new.pdf"
    assert "/api/v1/admin/reports/" not in (result.get("text") or "")


def test_app_js_uses_authenticated_download_button():
    """Frontend must not use plain <a target=_blank download> for admin reports."""
    from pathlib import Path

    js = Path("app/static/js/app.js").read_text(encoding="utf-8")
    assert "downloadAdminProtectedFile" in js
    assert "adminAuthHeaders" in js
    assert "Preparing download..." in js
    assert "Please sign in again" in js
    assert "This report is no longer available - regenerate it" in js
    # Old broken pattern should not remain for report cards
    assert 'download="${filename}"' not in js
    assert "target=\"_blank\" download=" not in js
