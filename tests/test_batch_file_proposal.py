"""POST /admin/properties/batch-file stages the change; nothing is written until the uploader confirms."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from tests import fs_users

client = TestClient(app)
PREFIX = "bf_test_"
PASSWORD = "BfTestPass123!"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    yield


def _user(role: str) -> dict:
    return fs_users.make_user(client, role, prefix=PREFIX, password=PASSWORD)


def _property(suffix: str) -> str:
    return fs_users.seed_property(f"{PREFIX}{suffix}", name=f"Test {suffix}")


def _csv(*rows: str) -> bytes:
    return ("property_id,name,status\n" + "\n".join(rows) + "\n").encode()


def _post(headers, content: bytes, action="update", name="batch.csv"):
    return client.post("/api/v1/admin/properties/batch-file", headers=headers,
                       files={"file": (name, content, "text/csv")}, data={"action": action})


@pytest.fixture
def writes(monkeypatch):
    """Any real bulk write is a test failure on the propose step; confirm swaps in a recorder."""
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"operation_id": "batch_fake", "status": "SUCCESS", "affected_count": len(kwargs["records"])}

    monkeypatch.setattr("app.services.admin_batch_service.execute_batch_property_operation", fake)
    return calls


def test_upload_is_staged_not_applied(writes):
    admin = _user("admin")
    pid = _property("one")
    resp = _post(admin["headers"], _csv(f"{pid},Test one,Sold Out"))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "PROPOSED" and body["requires_confirmation"] is True
    assert writes == []
    row = fs_users.pending_for(admin["email"])[0]
    assert row["status"] == "PENDING" and row["staff_user_id"] == admin["email"]
    assert row["tool_name"] == "execute_batch_property_action"


def test_only_the_uploader_can_confirm_and_only_previewed_rows_apply(writes):
    admin, other = _user("admin"), _user("super_admin")
    pid = _property("two")
    pending_id = _post(admin["headers"], _csv(f"{pid},Test two,Sold Out")).json()["pending_action_id"]
    assert client.post(f"/api/v1/admin/actions/{pending_id}/confirm", headers=other["headers"]).status_code == 403
    assert writes == []
    done = client.post(f"/api/v1/admin/actions/{pending_id}/confirm", headers=admin["headers"],
                       json={"confirmation_token": fs_users.pending_token(pending_id)})
    assert done.status_code == 200, done.text
    assert len(writes) == 1 and writes[0]["confirmed_by_staff"] is True
    assert [r["property_id"] for r in writes[0]["records"]] == [pid]


def test_workers_cannot_upload(writes):
    assert _post(_user("worker")["headers"], _csv("x,y,z")).status_code == 403


def test_invalid_action_empty_and_oversized_files_are_refused(writes):
    admin = _user("admin")
    assert _post(admin["headers"], _csv("a,b,c"), action="purge").status_code == 400
    assert _post(admin["headers"], b"").status_code == 400
    assert _post(admin["headers"], b"x" * (5 * 1024 * 1024 + 10)).status_code == 413


def test_row_cap(writes):
    admin = _user("admin")
    rows = [f"{PREFIX}r{i},Row {i},Sold" for i in range(501)]
    resp = _post(admin["headers"], _csv(*rows))
    assert resp.status_code == 400 and "limited to 500" in resp.json()["detail"]
    assert writes == []
