"""Pending actions: only the proposer decides, the token is not shown to others, a double confirm runs once."""
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests import fs_users

client = TestClient(app)
PREFIX = "pa_test_"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    yield


def _user(role: str) -> dict:
    return fs_users.make_user(client, role, prefix=PREFIX)


def _pending(owner_email: str) -> str:
    return fs_users.make_pending(owner_email)


def _status(action_id: str) -> str:
    return fs_users.pending_status(action_id)


def _confirm(action_id: str, who: dict):
    """The proposer confirms with the token the proposal returned."""
    return client.post(
        f"/api/v1/admin/actions/{action_id}/confirm",
        headers=who["headers"],
        json={"confirmation_token": fs_users.pending_token(action_id)},
    )


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(payload, staff_user_email=None, db=None):
        calls.append(payload)
        time.sleep(0.3)
        return {"delivered": True, "success": True, "completed": True, "is_mock": False, "provider": "fake"}

    monkeypatch.setattr("app.services.action_service._execute_send_email", fake_send)
    return calls


def test_another_staff_member_cannot_confirm(sent):
    owner, other = _user("worker"), _user("super_admin")
    action_id = _pending(owner["email"])
    resp = client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers=other["headers"])
    assert resp.status_code == 403
    assert sent == []
    assert _status(action_id) == "PENDING"


def test_another_staff_member_cannot_reject(sent):
    owner, other = _user("worker"), _user("admin")
    action_id = _pending(owner["email"])
    assert client.post(f"/api/v1/admin/actions/{action_id}/reject", headers=other["headers"]).status_code == 403
    assert _status(action_id) == "PENDING"


def test_the_proposer_can_confirm_once(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    ok = _confirm(action_id, owner)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "EXECUTED"
    again = _confirm(action_id, owner)
    assert again.status_code == 200 and again.json().get("idempotent") is True
    assert len(sent) == 1


def test_the_proposer_can_reject(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    assert client.post(f"/api/v1/admin/actions/{action_id}/reject", headers=owner["headers"]).status_code == 200
    assert _status(action_id) == "REJECTED" and sent == []


def test_wrong_token_is_still_refused_for_the_proposer(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    resp = client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers=owner["headers"], json={"confirmation_token": "act_wrong"})
    assert resp.status_code == 400 and sent == []


def test_inspect_hides_the_token_from_everyone_but_the_proposer():
    owner, worker, admin = _user("worker"), _user("worker"), _user("admin")
    action_id = _pending(owner["email"])
    mine = client.get(f"/api/v1/admin/actions/{action_id}", headers=owner["headers"])
    assert mine.status_code == 200 and mine.json()["confirmation_token"].startswith("act_")
    assert client.get(f"/api/v1/admin/actions/{action_id}", headers=worker["headers"]).status_code == 403
    seen_by_admin = client.get(f"/api/v1/admin/actions/{action_id}", headers=admin["headers"])
    assert seen_by_admin.status_code == 200 and seen_by_admin.json()["confirmation_token"] is None


def test_two_simultaneous_confirms_execute_once(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    codes = []

    def go():
        codes.append(_confirm(action_id, owner).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 400], codes
    assert len(sent) == 1


def _expiring_action(minutes: int, owner: str = f"{PREFIX}expiry@tajlandia.com") -> str:
    """Pending action that expires `minutes` from now (negative: already expired)."""
    return fs_users.make_pending(owner, payload={}, minutes=minutes)


def test_expiry_uses_utc_regardless_of_server_time_zone():
    from app.services.action_service import get_pending_action
    assert get_pending_action(None, _expiring_action(10)).status == "PENDING"
    assert get_pending_action(None, _expiring_action(-10)).status == "EXPIRED"


# ---- authentication and decision rules, end to end ---------------------------------------------

@pytest.mark.parametrize("method,suffix", [("post", "/confirm"), ("post", "/reject"), ("get", "")])
def test_no_credentials_means_401(method, suffix, sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    assert getattr(client, method)(f"/api/v1/admin/actions/{action_id}{suffix}").status_code == 401
    assert _status(action_id) == "PENDING" and sent == []


def test_forged_or_unknown_bearer_tokens_are_401(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    for token in ("forged-token", "dev-token-" + owner["email"], "act_" + uuid.uuid4().hex):
        resp = client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401, token
    assert sent == []


def test_a_logged_out_session_can_no_longer_confirm(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    assert client.post("/api/v1/auth/logout", headers=owner["headers"]).status_code == 200
    assert client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers=owner["headers"]).status_code == 401
    assert _status(action_id) == "PENDING" and sent == []


def test_a_blocked_account_cannot_confirm_even_with_a_live_session(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    fs_users.deactivate(owner["email"])
    assert client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers=owner["headers"]).status_code == 403
    assert _status(action_id) == "PENDING" and sent == []


def test_an_expired_action_cannot_be_confirmed(sent):
    owner = _user("worker")
    action_id = _expiring_action(-10, owner["email"])
    resp = _confirm(action_id, owner)
    assert resp.status_code == 400 and "expired" in resp.json()["detail"].lower()
    assert _status(action_id) == "EXPIRED" and sent == []


def test_an_action_that_has_not_expired_can_be_confirmed(sent):
    owner = _user("worker")
    action_id = _expiring_action(10, owner["email"])
    assert _confirm(action_id, owner).status_code == 200
    assert len(sent) == 1


def test_a_token_from_a_different_action_is_refused(sent):
    owner = _user("worker")
    first, second = _pending(owner["email"]), _pending(owner["email"])
    other_token = fs_users.pending_token(second)
    resp = client.post(f"/api/v1/admin/actions/{first}/confirm", headers=owner["headers"], json={"confirmation_token": other_token})
    assert resp.status_code == 400
    assert _status(first) == "PENDING" and _status(second) == "PENDING" and sent == []


def test_the_right_token_is_accepted(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    token = fs_users.pending_token(action_id)
    resp = client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers=owner["headers"], json={"confirmation_token": token})
    assert resp.status_code == 200 and len(sent) == 1


def test_an_unknown_action_is_404(sent):
    owner = _user("worker")
    assert client.post(f"/api/v1/admin/actions/{uuid.uuid4()}/confirm", headers=owner["headers"]).status_code == 404


def test_a_rejected_action_cannot_be_confirmed_afterwards(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    assert client.post(f"/api/v1/admin/actions/{action_id}/reject", headers=owner["headers"]).status_code == 200
    assert client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers=owner["headers"]).status_code == 400
    assert sent == []


def test_rejecting_twice_changes_nothing(sent):
    owner = _user("worker")
    action_id = _pending(owner["email"])
    assert client.post(f"/api/v1/admin/actions/{action_id}/reject", headers=owner["headers"]).status_code == 200
    # A second reject (double click) is harmless: the action stays rejected and nothing runs.
    client.post(f"/api/v1/admin/actions/{action_id}/reject", headers=owner["headers"])
    assert _status(action_id) == "REJECTED" and sent == []
