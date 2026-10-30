"""
GET /api/v1/admin/superadmin/conversations: the list the Admin Panel's "AI Chat Logs" page loads, and the "View" step.

It reads the same chat log as /admin/chat-logs/conversations, so these tests reuse that module's world (staff per role and
website, six logged conversations). Needs the Firestore emulator for the log and PostgreSQL for the staff accounts.
Each row's `conversation_id` is the session id, because the panel only accepts a UUID there and opens it with the review route.
"""
import pytest
from types import SimpleNamespace

from tests.test_chat_log_api import _at, client, fs, logs, world  # noqa: F401  (fixtures and helpers)

URL = "/api/v1/admin/superadmin/conversations"


def _get(who, **params):
    return client.get(URL, headers=who["headers"], params=params)


def _sid(key):
    return key.split("__", 1)[1]


def _sessions(resp):
    assert resp.status_code == 200, resp.text
    return [row["conversation_id"] for row in resp.json()["logs"]]


def test_requires_a_login(world):
    assert client.get(URL).status_code == 401


@pytest.mark.parametrize("who", ["admin_taj", "admin_thai", "worker"])
def test_only_a_super_admin_may_list(world, who):
    assert _get(getattr(world.people, who)).status_code == 403


def test_super_admin_gets_the_rows_the_admin_panel_shows(world):
    k = world.keys
    res = _get(world.people.boss)
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["total"], body["limit"], body["offset"]) == (6, 25, 0)
    first = body["logs"][0]
    assert first["conversation_id"] == _sid(k.a2)          # newest first, the session id
    assert first["project"] == "tajlandia.com"
    assert first["user_message"] == "generate a report"    # the "Chat" column
    assert first["ai_response"] == "Report ready."         # the "AI Response" column
    assert first["role"] == "super_admin"
    assert first["user_email"] == world.people.boss["email"]
    assert first["model"]                                  # the "Model" badge
    assert first["created_at"]
    assert set(_sessions(res)) == {_sid(x) for x in (k.p1, k.p2, k.p3, k.a1, k.a2, k.a3)}


def test_the_model_is_the_one_configured_for_each_bot(world):
    from app.core.config import settings
    rows = {row["conversation_id"]: row for row in _get(world.people.boss).json()["logs"]}
    assert rows[_sid(world.keys.p1)]["model"] == settings.OPENAI_MODEL
    assert rows[_sid(world.keys.a1)]["model"] == (settings.ADMIN_MAIN_MODEL or settings.ANTHROPIC_MODEL)


def test_a_visitor_has_no_name_or_email(world):
    row = next(r for r in _get(world.people.boss).json()["logs"] if r["conversation_id"] == _sid(world.keys.p1))
    assert row["role"] == "public" and row["user_email"] is None and row["user_name"] is None


def test_the_staff_members_name_is_shown(world, monkeypatch):
    monkeypatch.setattr(logs, "_now", lambda: _at(27))
    conv = SimpleNamespace(agent_type="admin", session_id="named", site_origin="tajlandia.com", preferences={"language": "en"},
                           log_actor={"user_id": world.people.boss["id"], "email": world.people.boss["email"],
                                      "role": "super_admin", "name": "Master Administrator"})
    assert logs.record_turn(conv, "Show me villas in Phuket.", "Here are some villas in Phuket.")
    row = next(r for r in _get(world.people.boss).json()["logs"] if r["conversation_id"] == "named")
    assert row["user_name"] == "Master Administrator"


def test_paging_with_limit_and_offset(world):
    k = world.keys
    first = _get(world.people.boss, limit=2, offset=0).json()
    second = _get(world.people.boss, limit=2, offset=2).json()
    assert [e["conversation_id"] for e in first["logs"]] == [_sid(k.a2), _sid(k.p3)]
    assert [e["conversation_id"] for e in second["logs"]] == [_sid(k.a1), _sid(k.a3)]
    assert (second["total"], second["limit"], second["offset"]) == (6, 2, 2)
    past_end = _get(world.people.boss, limit=2, offset=10).json()
    assert past_end["logs"] == [] and past_end["total"] == 6


def test_search_role_website_and_date_filters(world):
    k = world.keys
    assert _sessions(_get(world.people.boss, search="villas")) == [_sid(k.p1)]
    assert _sessions(_get(world.people.boss, role="worker")) == [_sid(k.a3)]
    assert set(_sessions(_get(world.people.boss, website="thai.estate"))) == {_sid(k.p2)}
    assert set(_sessions(_get(world.people.boss, website="https://www.tajlandia.com"))) == {_sid(k.p1), _sid(k.a1), _sid(k.a2), _sid(k.a3)}
    assert set(_sessions(_get(world.people.boss, start_date="2026-10-25T00:00:00Z"))) == {_sid(k.p3), _sid(k.a2)}
    assert _get(world.people.boss, website="other.example").json()["total"] == 0


def test_the_model_filter_matches_part_of_the_model_name(world):
    from app.core.config import settings
    k = world.keys
    public_model = settings.OPENAI_MODEL
    got = set(_sessions(_get(world.people.boss, model=public_model[:4].upper())))
    assert {_sid(k.p1), _sid(k.p2), _sid(k.p3)} <= got
    assert _get(world.people.boss, model="no-such-model").json()["total"] == 0


def test_a_row_logged_before_answer_previews_still_shows_its_answer(world, fs):
    fs.collection(logs.LOGS).document(world.keys.p1).update({"firstAnswerPreview": __import__("google.cloud.firestore", fromlist=["x"]).DELETE_FIELD})
    row = next(r for r in _get(world.people.boss).json()["logs"] if r["conversation_id"] == _sid(world.keys.p1))
    assert row["ai_response"] == "Here are two villas."


def test_view_opens_a_listed_row_by_its_session_id(world):
    k = world.keys
    res = client.get(f"{URL}/{_sid(k.a2)}", headers=world.people.boss["headers"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert [m["content"] for m in body["messages"]] == ["generate a report", "Report ready."]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    # the full log key still works, and an unknown id is a clear 4xx rather than a crash
    assert client.get(f"{URL}/{k.a2}", headers=world.people.boss["headers"]).status_code == 200
    assert client.get(f"{URL}/no-such-session", headers=world.people.boss["headers"]).status_code in (400, 404)


def test_bad_paging_is_rejected(world):
    assert _get(world.people.boss, limit=0).status_code == 422
    assert _get(world.people.boss, limit=101).status_code == 422
    assert _get(world.people.boss, offset=-1).status_code == 422
