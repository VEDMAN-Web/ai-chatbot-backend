"""Opening a past admin conversation must send the staff token, or the chat shows up empty."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main
from app.api.v1 import admin as admin_api
from app.core.auth import AuthenticatedStaff

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "app.js"


def test_admin_history_endpoint_is_staff_only_and_returns_the_messages(monkeypatch):
    client = TestClient(main.app)
    assert client.get("/api/v1/admin/chat/history/s1").status_code == 401

    staff = AuthenticatedStaff(user_id="a", email="a@x.com", role="admin", allowed_websites=[], selected_websites=[])
    main.app.dependency_overrides[admin_api.require_admin_or_employee] = lambda: staff
    conv = SimpleNamespace(
        id="c1",
        message_history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        preferences={},
        created_at=None,
    )
    monkeypatch.setattr("app.services.conversation_service.get_or_create_conversation", lambda **kw: conv)
    try:
        body = client.get("/api/v1/admin/chat/history/s1").json()
    finally:
        main.app.dependency_overrides.pop(admin_api.require_admin_or_employee, None)
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_sidebar_history_loader_sends_the_admin_token_and_reports_failures():
    source = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"async function loadHistorySession\(.*?\n\}\n", source, re.S).group(0)
    assert "adminAuthHeaders()" in body
    assert "mode === 'admin'" in body
    # A failed load must tell the user instead of leaving a blank chat.
    assert "messagesArea.innerHTML" in body.split("if (!res.ok)", 1)[1].split("const data", 1)[0]
