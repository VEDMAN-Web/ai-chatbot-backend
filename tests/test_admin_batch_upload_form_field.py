"""The bulk-upload modal must send the form field the endpoint expects, and show readable errors."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.api.v1 import admin as admin_api
from app.core.auth import AuthenticatedStaff

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "app.js"


def _client(monkeypatch):
    staff = AuthenticatedStaff(user_id="a", email="a@x.com", role="admin", allowed_websites=[], selected_websites=[])
    main.app.dependency_overrides[admin_api.require_admin_or_super_admin] = lambda: staff
    seen = {}

    def fake_execute(**kwargs):
        seen.update(kwargs)
        return {"operation_id": "batch_test", "items_affected": len(kwargs["records"])}

    monkeypatch.setattr("app.services.admin_batch_service.execute_batch_property_operation", fake_execute)
    return TestClient(main.app), seen


def test_endpoint_takes_the_action_form_field_and_rejects_operation_type(monkeypatch):
    client, seen = _client(monkeypatch)
    try:
        files = {"file": ("list.csv", b"name,price\nVilla A,1000000\n", "text/csv")}
        ok = client.post("/api/v1/admin/properties/batch-file", files=files, data={"action": "create"})
        assert ok.status_code == 200, ok.text
        assert seen["action_type"] == "create"

        bad = client.post("/api/v1/admin/properties/batch-file", files=files, data={"operation_type": "create"})
        assert bad.status_code == 422
        assert bad.json()["detail"][0]["loc"] == ["body", "action"]
    finally:
        main.app.dependency_overrides.pop(admin_api.require_admin_or_super_admin, None)


def test_bulk_modal_posts_the_action_field():
    source = APP_JS.read_text(encoding="utf-8")
    assert "formData.append('action', selectedBatchActionType)" in source
    assert "formData.append('operation_type'" not in source


def test_bulk_modal_never_prints_an_error_object_as_text():
    source = APP_JS.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "function formatApiError(" in source
    handler = re.search(r"/api/v1/admin/properties/batch-file.*?catch \(err\)", source, re.S).group(0)
    assert "formatApiError(data.detail" in handler
    assert "new Error(data.detail ||" not in handler
