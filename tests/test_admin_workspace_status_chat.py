"""
"my drive is connected ?" asked in the Admin Panel's AI Assistant is answered from the account, in every role,
and Google/Drive wording is never refused as off-topic. (The model is not called for these answers.)
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests import fs_users
from app.main import app
from app.services.admin_agent import _granted_google_services, _is_workspace_status_question
from app.services.google_services import SCOPES

client = TestClient(app)
RUN = uuid.uuid4().hex[:6].translate(str.maketrans("0123456789", "ghijklmnop"))  # letters only: digits in a name get parsed as a price
PASSWORD = "WsStatus#123"
QUESTIONS = ["my drive is connected ?", "is my google drive connected", "Is my Gmail connected?", "check google workspace status",
             "is my calendar linked?"]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)


def _account(role, connected):
    email = f"ws_{RUN}_{role}_{'on' if connected else 'off'}_{uuid.uuid4().hex[:5]}@tajlandia.com"
    prefs = {}
    if connected:
        prefs["google_workspace"] = {"connected": True, "google_email": f"{role}.drive@gmail.example", "refresh_token": "r",
                                     "access_token": "a", "scopes": SCOPES}
    return fs_users.make_user(
        client, role, email=email, password=PASSWORD, preferences=prefs,
        allowed_websites=["tajlandia.com", "varsoviaestate.com"],
    )["headers"]


def _ask(headers, text):
    res = client.post("/api/v1/admin/chat", headers=headers, json={"message": text, "session_id": f"ws-{RUN}-{uuid.uuid4().hex[:6]}"})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize("role", ["super_admin", "admin", "worker"])
@pytest.mark.parametrize("question", QUESTIONS)
def test_a_connected_account_is_told_so(role, question):
    body = _ask(_account(role, True), question)
    text = body["text"]
    assert "off-topic" not in text.lower() and "real estate portfolio" not in text.lower()
    assert text.startswith("Yes") and f"{role}.drive@gmail.example" in text
    assert "Drive: read-only" in text
    assert [t["name"] for t in body["tools_used"]] == ["check_google_workspace_status"]


@pytest.mark.parametrize("role", ["super_admin", "admin", "worker"])
def test_an_unconnected_account_is_told_how_to_connect(role):
    text = _ask(_account(role, False), "my drive is connected ?")["text"]
    assert text.startswith("No") and "Connect" in text and "off-topic" not in text.lower()


def test_one_account_never_sees_anothers_google_email():
    on, off = _account("admin", True), _account("worker", False)
    assert "admin.drive@gmail.example" not in _ask(off, "is my google drive connected")["text"]
    assert "admin.drive@gmail.example" in _ask(on, "is my google drive connected")["text"]


@pytest.mark.parametrize("message,expected", [
    ("my drive is connected ?", True), ("is my google drive connected", True), ("check google workspace status", True),
    ("read the Varsovia PDF from my drive", False), ("send an email to the client", False),
    ("add the listing from this Drive file", False), ("how many properties do we have", False),
])
def test_only_status_questions_take_the_status_path(message, expected):
    assert _is_workspace_status_question(message) is expected


def test_services_come_from_the_scopes_google_granted():
    assert _granted_google_services(SCOPES) == ["drive", "gmail", "calendar"]
    assert _granted_google_services(["https://www.googleapis.com/auth/drive.readonly"]) == ["drive"]
    assert _granted_google_services(None) == [] and _granted_google_services([]) == []


def test_other_questions_are_still_refused_as_off_topic():
    body = _ask(_account("admin", True), "what is the capital of india")
    assert "off-topic" in body["text"].lower()
