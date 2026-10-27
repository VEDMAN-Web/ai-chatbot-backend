"""Persistent chat log (Firestore): messages are stored in order with actor, evidence and status, without secrets.

These tests need a Firestore emulator (FIRESTORE_EMULATOR_HOST); they are skipped when none is available.
"""
import json
import os
import threading
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import chat_log_service as logs
from app.services import conversation_service


@pytest.fixture
def fs(monkeypatch):
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Firestore emulator not available (set FIRESTORE_EMULATOR_HOST)")
    from google.cloud import firestore

    client = firestore.Client(project="chat-log-tests")
    monkeypatch.setattr(settings, "ENABLE_CHAT_LOG_STORE", True)
    monkeypatch.setattr(logs, "_client", lambda: client)
    yield client
    client.recursive_delete(client.collection(logs.LOGS))


def _conversation(agent="public", session="s-1", site="tajlandia.com", actor=None):
    conv = SimpleNamespace(agent_type=agent, session_id=session, site_origin=site, preferences={"language": "en"})
    if actor:
        conv.log_actor = actor
    return conv


def _header(fs, key):
    return fs.collection(logs.LOGS).document(key).get().to_dict()


def _messages(fs, key):
    ref = fs.collection(logs.LOGS).document(key).collection(logs.MESSAGES)
    return [d.to_dict() for d in ref.order_by("seq").stream()]


KEY = "public__s-1"


def test_a_visitor_turn_is_stored_as_two_ordered_messages(fs):
    assert logs.record_turn(_conversation(), "Show me villas in Phuket", "Here are 2 villas.") is True
    head, msgs = _header(fs, KEY), _messages(fs, KEY)
    assert [m["seq"] for m in msgs] == [1, 2] and [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "Show me villas in Phuket" and msgs[1]["content"] == "Here are 2 villas."
    assert head["conversationId"] == KEY and head["agentType"] == "public" and head["messageCount"] == 2
    assert head["website"] == "tajlandia.com" and head["owner"]["role"] == "public" and head["owner"]["userId"] is None
    assert msgs[0]["createdAt"] <= msgs[1]["createdAt"] and head["lastMessageAt"] >= head["createdAt"]
    assert head["firstQuestionPreview"].startswith("Show me villas")


def test_the_staff_member_is_recorded_for_admin_chats(fs):
    actor = {"user_id": "u-42", "email": "ann@tajlandia.com", "role": "admin"}
    logs.record_turn(_conversation("admin", "adm-1", actor=actor), "List properties", "Found 3.")
    key = "admin__adm-1"
    head, msgs = _header(fs, key), _messages(fs, key)
    assert head["agentType"] == "admin" and head["owner"] == {"userId": "u-42", "email": "ann@tajlandia.com", "role": "admin"}
    assert head["actorUserIds"] == ["u-42"]
    assert all(m["actor"]["email"] == "ann@tajlandia.com" for m in msgs)


def test_later_turns_append_and_keep_the_history(fs):
    logs.record_turn(_conversation(), "first question", "first answer")
    created = _header(fs, KEY)["createdAt"]
    logs.record_turn(_conversation(), "second question", "second answer")
    head, msgs = _header(fs, KEY), _messages(fs, KEY)
    assert [m["seq"] for m in msgs] == [1, 2, 3, 4]
    assert [m["content"] for m in msgs] == ["first question", "first answer", "second question", "second answer"]
    assert head["messageCount"] == 4 and head["createdAt"] == created
    assert head["firstQuestionPreview"] == "first question" and head["lastQuestionPreview"] == "second question"


def test_evidence_properties_and_tools_are_recorded(fs):
    logs.record_turn(
        _conversation(), "price of Alpha?", "Alpha costs 8.5M THB.",
        tool_results=[{"tool": "search_properties_full", "status": "executed", "result": {"items": [{"property_id": "PHK-1"}]}}],
        structured_data={"properties": [{"property_id": "PHK-1"}, {"property_id": "PHK-2"}], "property_ids": ["PHK-2", "PHK-3"]},
    )
    head, answer = _header(fs, KEY), _messages(fs, KEY)[1]
    assert answer["propertyIds"] == ["PHK-1", "PHK-2", "PHK-3"] and set(head["propertyIds"]) == {"PHK-1", "PHK-2", "PHK-3"}
    assert answer["evidenceStatus"] == "database" and answer["responseStatus"] == "ok"
    assert {"type": "property", "id": "PHK-1"} in answer["sources"] and {"type": "tool", "name": "search_properties_full"} in answer["sources"]
    assert answer["toolSummary"] == [{"tool": "search_properties_full", "status": "executed"}]


def test_an_answer_with_no_source_is_marked_as_such(fs):
    logs.record_turn(_conversation(), "hello", "Hello! How can I help?")
    answer = _messages(fs, KEY)[1]
    assert answer["evidenceStatus"] == "none" and answer["propertyIds"] == [] and answer["sources"] == []


def test_a_failed_tool_marks_the_response_as_an_error(fs):
    logs.record_turn(_conversation(), "update price", "Failed to execute action.",
                     tool_results=[{"tool": "create_or_update_listing", "status": "failed"}])
    answer = _messages(fs, KEY)[1]
    assert answer["responseStatus"] == "error" and "Failed to execute" in answer["errorMessage"]


def test_credentials_are_never_stored(fs):
    logs.record_turn(
        _conversation(), "my key is sk-live-ABCDEFGHIJKLMNOP and Authorization: Bearer ya29.SECRETTOKENVALUE123",
        "Do not share keys. Link: https://x.test/cb?access_token=URLSECRETVALUE&ok=1")
    stored = json.dumps([_header(fs, KEY)] + _messages(fs, KEY), default=str)
    for secret in ("sk-live-ABCDEFGHIJKLMNOP", "ya29.SECRETTOKENVALUE123", "URLSECRETVALUE"):
        assert secret not in stored
    assert "ok=1" in stored


def test_very_long_content_is_truncated(fs):
    logs.record_turn(_conversation(), "x" * 50000, "ok")
    content = _messages(fs, KEY)[0]["content"]
    assert len(content) < 20200 and "truncated" in content


def test_simultaneous_turns_get_unique_message_numbers(fs):
    def go(i):
        logs.record_turn(_conversation(), f"question {i}", f"answer {i}")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    msgs = _messages(fs, KEY)
    assert [m["seq"] for m in msgs] == list(range(1, 9)) and _header(fs, KEY)["messageCount"] == 8
    assert sorted(m["content"] for m in msgs if m["role"] == "user") == sorted(f"question {i}" for i in range(4))


def test_the_switch_turns_logging_off(fs, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_CHAT_LOG_STORE", False)
    assert logs.record_turn(_conversation(), "q", "a") is False
    assert not fs.collection(logs.LOGS).document(KEY).get().exists


def test_missing_or_failing_firestore_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_CHAT_LOG_STORE", True)
    monkeypatch.setattr(logs, "_client", lambda: None)
    assert logs.record_turn(_conversation(), "q", "a") is False

    def boom():
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(logs, "_client", boom)
    assert logs.record_turn(_conversation(), "q", "a") is False


def test_conversation_service_logs_each_turn_and_survives_a_logging_failure(fs, monkeypatch):
    monkeypatch.setattr(conversation_service, "_firestore_sync_enabled", lambda agent: False)
    conv = _conversation(session="s-1")
    conversation_service.append_conversation_turn(None, conv, "hello there", "hi")
    assert [m["content"] for m in _messages(fs, KEY)] == ["hello there", "hi"]

    def boom(*a, **k):
        raise RuntimeError("log store down")

    monkeypatch.setattr(logs, "record_turn", boom)
    result = conversation_service.append_conversation_turn(None, conv, "still works", "yes")
    assert result is conv and conv.message_history[-1]["content"] == "yes"


def test_super_admin_review_and_remark_excerpts_read_the_chat_log(fs):
    from unittest.mock import MagicMock
    from app.services.instruction_store import InstructionStoreError, lookup_chat_excerpt, review_conversation

    logs.record_turn(_conversation(), "what is the price?", "From 8.5M THB.")
    db = MagicMock()
    db.query.return_value.all.return_value = []          # nothing in the old Postgres tables

    review = review_conversation(db, KEY)
    assert review["conversation_id"] == KEY
    assert [(m["role"], m["content"]) for m in review["messages"]] == [("user", "what is the price?"), ("assistant", "From 8.5M THB.")]

    assert lookup_chat_excerpt(None, KEY, 1) == {"role": "assistant", "content": "From 8.5M THB."}
    assert lookup_chat_excerpt(None, KEY, 2) is None and lookup_chat_excerpt(None, KEY, -1) is None
    assert lookup_chat_excerpt(None, "public__never-logged", 0) is None
    with pytest.raises(InstructionStoreError):
        review_conversation(db, "public__never-logged")
