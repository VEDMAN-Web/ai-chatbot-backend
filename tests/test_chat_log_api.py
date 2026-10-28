"""Chat log review API: access by role and website, filters, search, sorting, pagination, ordering.

Needs a Firestore emulator (FIRESTORE_EMULATOR_HOST) for the log and PostgreSQL for the staff accounts.
"""
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionLocal
from app.services import chat_log_service as logs

client = TestClient(app)
PREFIX = "cl_test_"
PASSWORD = "ChatLogPass123!"
BASE = "/api/v1/admin/chat-logs"


def _at(day, hour=10):
    return datetime(2026, 10, day, hour, 0, tzinfo=timezone.utc)


@pytest.fixture
def fs(monkeypatch):
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Firestore emulator not available (set FIRESTORE_EMULATOR_HOST)")
    from google.cloud import firestore

    firestore_client = firestore.Client(project="chat-log-api-tests")
    monkeypatch.setattr(settings, "ENABLE_CHAT_LOG_STORE", True)
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(logs, "_client", lambda: firestore_client)
    for target in ("app.services.firestore_auth_service.get_firestore_client", "app.services.firestore_service.get_firestore_client"):
        monkeypatch.setattr(target, lambda *a, **kw: None)
    yield firestore_client
    firestore_client.recursive_delete(firestore_client.collection(logs.LOGS))
    db = SessionLocal()
    try:
        for user in db.query(User).filter(User.email.like(f"{PREFIX}%")).all():
            db.delete(user)
        db.commit()
    finally:
        db.close()


def _staff(role, websites):
    email = f"{PREFIX}{role}_{uuid.uuid4().hex[:6]}@tajlandia.com"
    db = SessionLocal()
    try:
        row = User(email=email, name=role, role=role, password_hash=hash_password(PASSWORD), is_active=True, allowed_websites=websites)
        db.add(row)
        db.commit()
        uid = str(row.id)
    finally:
        db.close()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"id": uid, "email": email, "role": role, "headers": {"Authorization": f"Bearer {token}"}}


def _log(monkeypatch, agent, session, site, day, question, answer, actor=None, **kw):
    monkeypatch.setattr(logs, "_now", lambda: _at(day))
    conv = SimpleNamespace(agent_type=agent, session_id=session, site_origin=site, preferences={"language": "en"})
    if actor:
        conv.log_actor = {"user_id": actor["id"], "email": actor["email"], "role": actor["role"]}
    assert logs.record_turn(conv, question, answer, **kw)
    return f"{agent}__{session}"


@pytest.fixture
def world(fs, monkeypatch):
    """Two staff members per website plus a super admin and a worker, and six logged conversations."""
    people = SimpleNamespace(
        boss=_staff("super_admin", ["tajlandia.com", "thai.estate", "varsoviaestate.com"]),
        admin_taj=_staff("admin", ["tajlandia.com"]),
        admin_thai=_staff("admin", ["thai.estate"]),
        worker=_staff("worker", ["tajlandia.com"]),
    )
    keys = SimpleNamespace(
        p1=_log(monkeypatch, "public", "v1", "tajlandia.com", 20, "Show villas in Phuket", "Here are two villas.",
                structured_data={"property_ids": ["PHK-1"]}),
        p2=_log(monkeypatch, "public", "v2", "thai.estate", 22, "pool condos in Samui", "One condo found.",
                structured_data={"property_ids": ["SMU-1"]}),
        p3=_log(monkeypatch, "public", "v3", "varsoviaestate.com", 25, "Hello", "Hi there"),
        a1=_log(monkeypatch, "admin", "a1", "tajlandia.com", 24, "list properties in Layan", "Found 3 properties.", actor=people.admin_taj),
        a2=_log(monkeypatch, "admin", "a2", "tajlandia.com", 26, "generate a report", "Report ready.", actor=people.boss),
        a3=_log(monkeypatch, "admin", "a3", "tajlandia.com", 23, "what is the roof material", "Not documented.", actor=people.worker),
    )
    return SimpleNamespace(people=people, keys=keys)


def _list(who, **params):
    return client.get(f"{BASE}/conversations", headers=who["headers"], params=params)


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [item["conversation_id"] for item in resp.json()["items"]]


# ---- who can see what ------------------------------------------------------------------------

def test_no_credentials_is_401(world):
    assert client.get(f"{BASE}/conversations").status_code == 401
    assert client.get(f"{BASE}/conversations/{world.keys.p1}").status_code == 401


def test_super_admin_sees_every_conversation_newest_first(world):
    k = world.keys
    assert _ids(_list(world.people.boss)) == [k.a2, k.p3, k.a1, k.a3, k.p2, k.p1]


def test_an_admin_sees_visitors_on_their_website_and_their_own_admin_chats_only(world):
    k = world.keys
    assert sorted(_ids(_list(world.people.admin_taj))) == sorted([k.p1, k.a1])
    assert sorted(_ids(_list(world.people.admin_thai))) == sorted([k.p2])


def test_a_worker_sees_only_their_own_admin_chat(world):
    assert _ids(_list(world.people.worker)) == [world.keys.a3]


def test_opening_a_conversation_the_caller_may_not_see_is_404(world):
    k = world.keys
    for key in (k.p2, k.p3, k.a2, k.a3):  # other website / other staff member's chat
        assert client.get(f"{BASE}/conversations/{key}", headers=world.people.admin_taj["headers"]).status_code == 404
    assert client.get(f"{BASE}/conversations/{k.p1}", headers=world.people.worker["headers"]).status_code == 404
    assert client.get(f"{BASE}/conversations/{k.p1}", headers=world.people.admin_taj["headers"]).status_code == 200


def test_a_website_filter_outside_the_callers_access_is_403(world):
    assert _list(world.people.admin_taj, website="thai.estate").status_code == 403
    assert _list(world.people.admin_taj, website="tajlandia.com").status_code == 200


def test_slash_in_an_id_is_not_found_not_a_nested_lookup(world):
    resp = client.get(f"{BASE}/conversations/{world.keys.p1}%2Fmessages%2Fm000001", headers=world.people.boss["headers"])
    assert resp.status_code in (404, 422)
    assert _ids(_list(world.people.boss, conversation_id=f"{world.keys.p1}/messages/m000001")) == []


# ---- filters, search, sort, paging -------------------------------------------------------------

def test_filter_by_agent_type_role_user_and_conversation_id(world):
    boss, k = world.people.boss, world.keys
    assert sorted(_ids(_list(boss, agent_type="public"))) == sorted([k.p1, k.p2, k.p3])
    assert sorted(_ids(_list(boss, agent_type="admin"))) == sorted([k.a1, k.a2, k.a3])
    assert _ids(_list(boss, role="worker")) == [k.a3]
    assert _ids(_list(boss, role="public", website="thai.estate")) == [k.p2]
    assert _ids(_list(boss, user=world.people.admin_taj["email"].split("@")[0])) == [k.a1]
    assert _ids(_list(boss, user=world.people.admin_taj["id"])) == [k.a1]
    assert _ids(_list(boss, user="v2")) == [k.p2]                       # a visitor is found by session id
    assert _ids(_list(boss, conversation_id=k.a2)) == [k.a2]
    assert _ids(_list(boss, conversation_id="admin__does-not-exist")) == []


def test_filter_by_property_and_dates(world):
    boss, k = world.people.boss, world.keys
    assert _ids(_list(boss, property_id="SMU-1")) == [k.p2]
    assert _ids(_list(boss, property_id="NOPE-1")) == []
    assert _ids(_list(boss, date_from="2026-10-23T00:00:00Z", date_to="2026-10-25T23:59:59Z")) == [k.p3, k.a1, k.a3]
    assert _ids(_list(boss, date_from="2026-10-26T00:00:00Z")) == [k.a2]
    assert _ids(_list(boss, date_to="2026-10-20T23:59:59Z")) == [k.p1]


def test_search_questions_and_answers(world):
    boss, k = world.people.boss, world.keys
    assert _ids(_list(boss, q="villas")) == [k.p1]            # in the question and the answer
    assert _ids(_list(boss, q="found 3 properties")) == [k.a1]  # a phrase, in an answer
    assert _ids(_list(boss, q="ROOF material")) == [k.a3]       # not case sensitive
    assert _ids(_list(boss, q="zzzunmatched")) == []
    assert _ids(_list(world.people.admin_taj, q="condos")) == []  # exists, but on a website the caller cannot see


def test_remark_status_filter(world, fs):
    boss, k = world.people.boss, world.keys
    fs.collection(logs.LOGS).document(k.p1).update({"remarkStatuses": ["open"]})
    fs.collection(logs.LOGS).document(k.a1).update({"remarkStatuses": ["resolved"]})
    assert _ids(_list(boss, remark_status="open")) == [k.p1]
    assert _ids(_list(boss, remark_status="resolved")) == [k.a1]
    assert sorted(_ids(_list(boss, remark_status="none"))) == sorted([k.p2, k.p3, k.a2, k.a3])
    assert _list(boss, remark_status="bogus").status_code == 422


def test_sorting_and_paging(world):
    boss, k = world.people.boss, world.keys
    assert _ids(_list(boss, order="asc")) == [k.p1, k.p2, k.a3, k.a1, k.p3, k.a2]
    assert _ids(_list(boss, sort="created_at", order="asc"))[0] == k.p1
    first = _list(boss, page_size=4, page=1).json()
    second = _list(boss, page_size=4, page=2).json()
    assert first["total"] == 6 and len(first["items"]) == 4 and len(second["items"]) == 2
    assert not {i["conversation_id"] for i in first["items"]} & {i["conversation_id"] for i in second["items"]}
    assert _list(boss, page=9).json()["items"] == []
    assert _list(boss, sort="password").status_code == 422
    assert _list(boss, page_size=101).status_code == 422 and _list(boss, page=0).status_code == 422


def test_list_items_carry_what_a_reviewer_needs(world):
    item = _list(world.people.boss, conversation_id=world.keys.a1).json()["items"][0]
    assert item["agent_type"] == "admin" and item["website"] == "tajlandia.com" and item["message_count"] == 2
    assert item["user"] == {"user_id": world.people.admin_taj["id"], "email": world.people.admin_taj["email"], "role": "admin", "name": None}
    assert item["first_question"] == "list properties in Layan" and item["last_message_at"].startswith("2026-10-24")


# ---- opening a conversation --------------------------------------------------------------------

def test_messages_come_back_in_order_with_their_evidence(world, monkeypatch):
    _log(monkeypatch, "public", "v1", "tajlandia.com", 21, "and the price?", "From 8.5M THB.", tool_results=[{"tool": "get_property", "status": "ok"}])
    body = client.get(f"{BASE}/conversations/{world.keys.p1}", headers=world.people.admin_taj["headers"]).json()
    assert [m["seq"] for m in body["messages"]] == [1, 2, 3, 4] and body["total_messages"] == 4
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "assistant"]
    assert body["messages"][1]["property_ids"] == ["PHK-1"] and body["messages"][1]["evidence_status"] == "database"
    assert body["messages"][3]["tool_summary"] == [{"tool": "get_property", "status": "ok"}]
    assert body["conversation"]["message_count"] == 4
    paged = client.get(f"{BASE}/conversations/{world.keys.p1}", headers=world.people.admin_taj["headers"], params={"page": 2, "page_size": 3}).json()
    assert [m["seq"] for m in paged["messages"]] == [4]


def test_a_reviewer_without_view_all_sees_only_their_own_messages_in_a_shared_admin_session(world, monkeypatch):
    _log(monkeypatch, "admin", "a1", "tajlandia.com", 27, "someone else typing", "reply", actor=world.people.boss)
    mine = client.get(f"{BASE}/conversations/{world.keys.a1}", headers=world.people.admin_taj["headers"]).json()
    assert [m["content"] for m in mine["messages"] if m["role"] == "user"] == ["list properties in Layan"]
    everything = client.get(f"{BASE}/conversations/{world.keys.a1}", headers=world.people.boss["headers"]).json()
    assert len(everything["messages"]) == 4


def test_an_unknown_conversation_is_404(world):
    assert client.get(f"{BASE}/conversations/public__nope", headers=world.people.boss["headers"]).status_code == 404


def test_a_stopped_log_store_is_503(world, monkeypatch):
    monkeypatch.setattr(logs, "_client", lambda: None)
    assert _list(world.people.boss).status_code == 503
    assert client.get(f"{BASE}/conversations/{world.keys.p1}", headers=world.people.boss["headers"]).status_code == 503
