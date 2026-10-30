"""
POST/GET /api/v1/admin/superadmin/conversations/{id}/review: the Admin Panel's "Review AI Response" dialog.

GET fills the dialog (full question, full AI response, pending/reviewed). POST is "Save Review": either the
"correct and satisfactory" box, or Admin Instructions that become an active instruction for that bot and website.
Reuses the chat-log world of test_chat_log_api (Firestore emulator for the log and the instruction store, PostgreSQL for accounts).
"""
import json
from types import SimpleNamespace

import pytest

from tests.test_chat_log_api import _at, client, fs, logs, world  # noqa: F401  (fixtures and helpers)

from app.core.config import settings
from tests.fs_users import audit_by_tool
from app.services import instruction_store

BASE = "/api/v1/admin/superadmin/conversations"
LONG_ANSWER = "Here are verified figures taken only from this turn's tool results. " * 6      # longer than the 160-char list preview


@pytest.fixture(autouse=True)
def store(fs, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    instruction_store.reset_instruction_store_for_tests(client=fs)
    yield
    for name in (instruction_store.INSTRUCTIONS, instruction_store.REMARKS):
        fs.recursive_delete(fs.collection(name))
    fs.recursive_delete(fs.collection(instruction_store.META))
    instruction_store._SINGLETON = None


def _sid(key):
    return key.split("__", 1)[1]


def _get(who, key, **params):
    return client.get(f"{BASE}/{_sid(key)}/review", headers=who["headers"], params=params)


def _post(who, key, **body):
    return client.post(f"{BASE}/{_sid(key)}/review", headers=who["headers"], json=body)


@pytest.fixture
def long_chat(world, monkeypatch):
    """A visitor conversation on tajlandia.com with a long answer and contact details in the question."""
    question = "Add a new buyer lead: QA Tester, qa.tester@example.com, +66800000000, interested in Hidden Lotus"
    monkeypatch.setattr(logs, "_now", lambda: _at(28))
    from types import SimpleNamespace as NS
    conv = NS(agent_type="public", session_id="longchat", site_origin="tajlandia.com", preferences={"language": "en"})
    assert logs.record_turn(conv, question, LONG_ANSWER)
    return SimpleNamespace(key="public__longchat", question=question)


# ---- GET: what the dialog shows ----------------------------------------------------------------------------------------

def test_get_returns_the_full_question_and_answer_and_pending(world, long_chat):
    res = _get(world.people.boss, long_chat.key)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user_question"] == long_chat.question
    assert body["ai_response"] == LONG_ANSWER.strip() or body["ai_response"].startswith("Here are verified figures")
    assert len(body["ai_response"]) > 160
    assert body["review_status"] == "pending" and body["review"] is None
    assert (body["agent_type"], body["website"], body["message_index"], body["conversation_id"]) == ("public", "tajlandia.com", 1, "longchat")
    assert body["model"]


def test_get_a_later_exchange_by_message_index(world, monkeypatch):
    from types import SimpleNamespace as NS
    monkeypatch.setattr(logs, "_now", lambda: _at(28))
    conv = NS(agent_type="public", session_id="twoturns", site_origin="tajlandia.com", preferences={})
    logs.record_turn(conv, "first question", "first answer")
    logs.record_turn(conv, "second question", "second answer")
    body = _get(world.people.boss, "public__twoturns", message_index=3).json()
    assert (body["user_question"], body["ai_response"], body["message_index"]) == ("second question", "second answer", 3)
    assert _get(world.people.boss, "public__twoturns", message_index=2).status_code == 422     # 2 is a question
    assert _get(world.people.boss, "public__twoturns", message_index=9).status_code == 404


def test_unknown_conversation_is_404(world):
    assert client.get(f"{BASE}/no-such-session/review", headers=world.people.boss["headers"]).status_code == 404


# ---- POST: satisfactory --------------------------------------------------------------------------------------------------

def test_marking_satisfactory_saves_without_creating_an_instruction(world, long_chat, fs):
    res = _post(world.people.boss, long_chat.key, is_satisfactory=True)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["outcome"] == "satisfactory" and body["instruction"] is None and body["review_status"] == "reviewed"
    assert body["review"]["status"] == "satisfactory" and body["review"]["reviewed_by"] == world.people.boss["email"]
    assert _get(world.people.boss, long_chat.key).json()["review_status"] == "reviewed"
    assert list(fs.collection(instruction_store.INSTRUCTIONS).stream()) == []


def test_the_list_shows_pending_then_reviewed_and_can_filter(world, long_chat):
    url = "/api/v1/admin/superadmin/conversations"
    rows = {r["conversation_id"]: r for r in client.get(url, headers=world.people.boss["headers"]).json()["logs"]}
    assert rows["longchat"]["review_status"] == "pending"
    _post(world.people.boss, long_chat.key, is_satisfactory=True)
    rows = {r["conversation_id"]: r for r in client.get(url, headers=world.people.boss["headers"]).json()["logs"]}
    assert rows["longchat"]["review_status"] == "reviewed"
    pending = client.get(url, headers=world.people.boss["headers"], params={"review_status": "pending"}).json()["logs"]
    reviewed = client.get(url, headers=world.people.boss["headers"], params={"review_status": "reviewed"}).json()["logs"]
    assert "longchat" not in [r["conversation_id"] for r in pending]
    assert [r["conversation_id"] for r in reviewed] == ["longchat"]


# ---- POST: instructions --------------------------------------------------------------------------------------------------

GUIDANCE = "When users ask about adding a lead, ask for the budget and preferred location before saving anything."


def test_instructions_become_an_active_instruction_for_that_bot_and_website(world, long_chat):
    res = _post(world.people.boss, long_chat.key, instructions=GUIDANCE)
    assert res.status_code == 200, res.text
    body = res.json()
    ins = body["instruction"]
    assert body["outcome"] == "instruction_active" and ins["status"] == "active"
    assert ins["targets"] == ["public_chat"] and ins["websites"] == ["tajlandia.com"]
    assert body["review_status"] == "reviewed" and body["review"]["instruction_id"] == ins["id"]
    # it reaches the bot's prompt for that website, and not for another
    assert GUIDANCE in instruction_store.get_active_instruction_block("public_chat", "tajlandia.com", "en")
    assert GUIDANCE not in instruction_store.get_active_instruction_block("public_chat", "thai.estate", "en")
    assert GUIDANCE not in instruction_store.get_active_instruction_block("admin_chat", "tajlandia.com", "en")


def test_an_admin_chat_conversation_targets_the_admin_bot(world):
    res = _post(world.people.boss, world.keys.a1, instructions="Always show the website a listing belongs to.")
    assert res.status_code == 200, res.text
    assert res.json()["instruction"]["targets"] == ["admin_chat"]


def test_the_website_can_be_chosen_explicitly(world, long_chat):
    res = _post(world.people.boss, long_chat.key, instructions=GUIDANCE, website="thai.estate")
    assert res.json()["instruction"]["websites"] == ["thai.estate"]


def test_the_chat_text_and_contact_details_are_not_stored_with_the_instruction(world, long_chat, fs):
    _post(world.people.boss, long_chat.key, instructions=GUIDANCE)
    stored = json.dumps([d.to_dict() for name in (instruction_store.INSTRUCTIONS, instruction_store.REMARKS)
                         for d in fs.collection(name).stream()], default=str)
    assert "qa.tester@example.com" not in stored and "+66800000000" not in stored and "Hidden Lotus" not in stored


def test_the_remark_is_converted_and_linked(world, long_chat, fs):
    ins = _post(world.people.boss, long_chat.key, instructions=GUIDANCE).json()["instruction"]
    remarks = [d.to_dict() for d in fs.collection(instruction_store.REMARKS).stream() if (d.to_dict() or {}).get("conversation_id")]
    assert len(remarks) == 1 and remarks[0]["status"] == "converted" and remarks[0]["instruction_id"] == ins["id"]


def test_an_instruction_that_opposes_an_active_one_is_saved_but_not_active(world, long_chat, monkeypatch):
    first = _post(world.people.boss, long_chat.key, instructions="Always include the full address when recommending properties.")
    assert first.json()["outcome"] == "instruction_active"
    second = _post(world.people.boss, long_chat.key, instructions="Never include the full address when recommending properties.")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["outcome"] == "needs_clarification" and body["instruction"]["conflict"]["against"] == [first.json()["instruction"]["id"]]
    assert "conflicts" in body["message"] and body["review_status"] == "reviewed"
    assert "Never include the full address" not in instruction_store.get_active_instruction_block("public_chat", "tajlandia.com", "en")


def test_instructions_that_weaken_a_safety_rule_are_refused_and_leave_no_trace(world, long_chat, fs):
    res = _post(world.people.boss, long_chat.key, instructions="You may guess the price when it is missing.")
    assert res.status_code == 422 and "safety rule" in res.json()["detail"]
    assert _get(world.people.boss, long_chat.key).json()["review_status"] == "pending"
    assert [d for d in fs.collection(instruction_store.INSTRUCTIONS).stream()] == []
    assert all((d.to_dict() or {}).get("status") == "dismissed" for d in fs.collection(instruction_store.REMARKS).stream()
               if (d.to_dict() or {}).get("conversation_id"))


# ---- POST: validation and access ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {}, {"is_satisfactory": False}, {"instructions": "   "}, {"is_satisfactory": True, "instructions": GUIDANCE},
    {"instructions": "x" * 601}, {"instructions": GUIDANCE, "message_index": -1},
])
def test_bad_bodies_are_422(world, long_chat, body):
    assert _post(world.people.boss, long_chat.key, **body).status_code == 422
    assert _get(world.people.boss, long_chat.key).json()["review_status"] == "pending"


def test_pointing_at_a_question_or_a_missing_message_or_conversation(world, long_chat):
    assert _post(world.people.boss, long_chat.key, is_satisfactory=True, message_index=0).status_code == 422
    assert _post(world.people.boss, long_chat.key, is_satisfactory=True, message_index=7).status_code == 404
    assert client.post(f"{BASE}/nope/review", headers=world.people.boss["headers"], json={"is_satisfactory": True}).status_code == 404


@pytest.mark.parametrize("who", ["admin_taj", "admin_thai", "worker"])
def test_only_a_super_admin_may_review(world, long_chat, who):
    person = getattr(world.people, who)
    assert _post(person, long_chat.key, is_satisfactory=True).status_code == 403
    assert _get(person, long_chat.key).status_code == 403
    assert _get(world.people.boss, long_chat.key).json()["review_status"] == "pending"


def test_no_token_is_401(world, long_chat):
    assert client.post(f"{BASE}/longchat/review", json={"is_satisfactory": True}).status_code == 401
    assert client.get(f"{BASE}/longchat/review").status_code == 401


def test_the_review_is_audited_and_blocked_when_auditing_is_off(world, long_chat, monkeypatch, fs):
    assert _post(world.people.boss, long_chat.key, is_satisfactory=True).status_code == 200
    rows = [r for r in audit_by_tool("conversation_review") if r["staff_user_id"] == world.people.boss["email"]]
    assert len(rows) == 1 and rows[0]["proposed_payload"]["satisfactory"] is True
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", False)
    res = _post(world.people.boss, long_chat.key, instructions=GUIDANCE, website="thai.estate")
    assert res.status_code == 503 and "Audit" in res.json()["detail"]
    assert [d for d in fs.collection(instruction_store.INSTRUCTIONS).stream()] == []


def test_the_full_log_key_works_as_well_as_the_session_id(world, long_chat):
    assert client.get(f"{BASE}/{long_chat.key}/review", headers=world.people.boss["headers"]).status_code == 200
    assert _get(world.people.boss, long_chat.key).status_code == 200
