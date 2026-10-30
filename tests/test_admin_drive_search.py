"""
"find this folder named varsovia" in the Admin AI Assistant: looks in the asker's own Drive (read-only) and says what it found,
instead of answering with "Found 10 properties across all areas". Google is faked (no network).
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.fs_users import audit_by_tool, make_user
from app.main import app
from app.services.admin_drive_chat import detect_drive_search
from app.services.google_services import SCOPES

client = TestClient(app)
RUN = uuid.uuid4().hex[:6].translate(str.maketrans("0123456789", "ghijklmnop"))  # letters only: digits in a name get parsed as a price
FOLDER = "application/vnd.google-apps.folder"


# ---- what counts as a Drive search --------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,query,kind", [
    ('Okay, now find this folder named "varsovia"', "varsovia", "folder"),
    ("find the folder named Varsovia", "Varsovia", "folder"),
    ("search my drive for Oferta", "Oferta", "any"),
    ("search google drive for varsovia pdf", "varsovia pdf", "any"),
    ("list the files in folder varsovia", "varsovia", "folder"),
    ("find file Oferta_Tajlandia_Boleslaw_Bednarz.pdf in my drive", "Oferta_Tajlandia_Boleslaw_Bednarz.pdf", "file"),
    ("where is the folder 'Varsovia 2026'", "Varsovia 2026", "folder"),
    ("find folder", None, "folder"),
])
def test_drive_searches_are_recognised(message, query, kind):
    found = detect_drive_search(message)
    assert found is not None and found.query == query and found.kind == kind


@pytest.mark.parametrize("message", [
    "is my drive connected ?", "is my drive connected or not please check", "read file Oferta.pdf from Drive",
    "add the listing from this Drive file", "summarize the file from my drive", "show villas in Phuket", "export to pdf",
    "find villas under 5M", "Show 3 Villas - Lamai", "send the file by email", "check google workspace status",
])
def test_other_messages_are_not_drive_searches(message):
    assert detect_drive_search(message) is None


# ---- through the chat ---------------------------------------------------------------------------------------------------

class FakeDrive:
    built = []
    queries = []
    files_by_folder = {}
    folders = []
    fail = False

    def __init__(self, creds):
        FakeDrive.built.append(creds)

    def files(self):
        return self

    def list(self, q="", **kw):
        FakeDrive.queries.append(q)
        if FakeDrive.fail:
            raise RuntimeError("boom")
        if " in parents" in q:
            folder_id = q.split("'")[1]
            return SimpleNamespace(execute=lambda: {"files": FakeDrive.files_by_folder.get(folder_id, [])})
        needle = q.split("name contains '")[1].split("'")[0].lower()
        hits = [f for f in FakeDrive.folders if needle in f["name"].lower()]
        if "mimeType = '" + FOLDER + "'" in q:
            hits = [f for f in hits if f["mimeType"] == FOLDER]
        return SimpleNamespace(execute=lambda: {"files": hits})

    def __getattr__(self, name):
        if name in ("create", "update", "delete", "copy", "emptyTrash"):
            raise AssertionError(f"Drive write call attempted: {name}")
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def fake_google(monkeypatch):
    import googleapiclient.discovery as discovery
    FakeDrive.built, FakeDrive.queries, FakeDrive.fail = [], [], False
    FakeDrive.folders = [
        {"id": "fold1", "name": "varsovia", "mimeType": FOLDER, "modifiedTime": "2026-10-14T09:00:00Z", "webViewLink": "https://drive.example/fold1"},
        {"id": "file9", "name": "varsovia notes.txt", "mimeType": "text/plain", "modifiedTime": "2026-10-01T09:00:00Z", "size": "2048"},
    ]
    FakeDrive.files_by_folder = {"fold1": [
        {"id": "f1", "name": "Oferta_Tajlandia_Boleslaw_Bednarz_od_Marcina_2026-10-14.pdf", "mimeType": "application/pdf", "size": str(2_900_000)},
        {"id": "f2", "name": "Price list.xlsx", "mimeType": "application/vnd.ms-excel", "size": "30000"},
    ]}
    monkeypatch.setattr(discovery, "build", lambda service, version, credentials=None, **kw: FakeDrive(credentials))
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "GOOGLE_SHARED_CREDENTIAL_FALLBACK", False)


def _account(role="admin", connected=True):
    prefs = {"google_workspace": {"connected": True, "google_email": f"{role}@gmail.example", "refresh_token": f"refresh-{role}",
                                  "access_token": "a", "scopes": SCOPES}} if connected else {}
    made = make_user(client, role, f"dr_{RUN}_", allowed_websites=["tajlandia.com"], password="Drive#12345", preferences=prefs)
    return made["email"], {**made["headers"], "X-Selected-Websites": "tajlandia.com"}


def _ask(headers, text):
    res = client.post("/api/v1/admin/chat", headers=headers, json={"message": text, "session_id": f"dr-{RUN}-{uuid.uuid4().hex[:6]}"})
    assert res.status_code == 200, res.text
    return SimpleNamespace(**{"text": "", "tools_used": [], **res.json()})


def test_the_screenshot_request_finds_the_folder_and_lists_what_is_inside():
    _email, h = _account()
    r = _ask(h, 'Okay, now find this folder named "varsovia"')
    assert "properties" not in r.text and "Found **1** folder" in r.text, r.text
    assert "varsovia" in r.text and "Oferta_Tajlandia_Boleslaw_Bednarz_od_Marcina_2026-10-14.pdf" in r.text and "2.8 MB" in r.text
    assert "Price list.xlsx" in r.text and "https://drive.example/fold1" in r.text
    assert [t["name"] for t in r.tools_used] == ["search_drive_files"]
    assert "varsovia notes.txt" not in r.text                                   # a folder search does not list files with that name


def test_it_uses_the_askers_own_google_account_and_only_reads():
    for role in ("super_admin", "admin", "worker"):
        FakeDrive.built.clear()
        _email, h = _account(role)
        assert "varsovia" in _ask(h, "find folder named varsovia").text
        assert [c.refresh_token for c in FakeDrive.built] == [f"refresh-{role}"]
    assert all("trashed = false" in q for q in FakeDrive.queries)


def test_a_file_search_returns_files_not_folders():
    _e, h = _account()
    r = _ask(h, "search my drive for varsovia")
    assert "varsovia notes.txt" in r.text and "Found **2**" in r.text


def test_no_match_says_so_and_is_not_a_property_list():
    _e, h = _account()
    r = _ask(h, "find folder named does-not-exist-xyz")
    assert "found no folder" in r.text and "properties" not in r.text


def test_an_unconnected_account_is_told_to_connect_and_never_reaches_drive():
    _e, h = _account(connected=False)
    r = _ask(h, "find folder named varsovia")
    assert "not connected" in r.text and FakeDrive.built == []


def test_a_drive_error_is_reported_not_turned_into_no_results():
    FakeDrive.fail = True
    _e, h = _account()
    r = _ask(h, "find folder named varsovia")
    assert "could not be searched" in r.text and "found no" not in r.text


def test_a_missing_name_is_asked_for():
    _e, h = _account()
    r = _ask(h, "find folder")
    assert "Which folder or file" in r.text and FakeDrive.queries == []


def test_a_quote_in_the_name_cannot_break_the_drive_query():
    _e, h = _account()
    _ask(h, "find folder named \"o'brien\\' or name contains 'x\"")
    assert all(q.count("'") % 2 == 0 or "\\'" in q for q in FakeDrive.queries)


def test_the_search_is_audited():
    email, h = _account()
    _ask(h, "find folder named varsovia")
    rows = [r for r in audit_by_tool("search_drive_files") if r["staff_user_id"] == email]
    assert len(rows) == 1 and rows[0]["status"] == "SUCCESS" and rows[0]["tool_arguments"]["query"] == "varsovia"


# ---- the generic property dump ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("message", ["blah blah do the thing", "ok next"])
def test_a_message_that_asks_for_no_property_search_does_not_get_ten_random_properties(message):
    _e, h = _account()
    r = _ask(h, message)
    # either safe answer is fine: "did not run a search", or (for a bare "ok ...") "nothing waiting for confirmation"
    assert ("did not run a search" in r.text or "nothing waiting for confirmation" in r.text)
    assert "Found" not in r.text and not r.tools_used


@pytest.mark.parametrize("message", ["show all properties", "villas in Phuket", "list listings", "2 bedroom condo under 5M"])
def test_real_property_searches_still_search(message):
    _e, h = _account()
    r = _ask(h, message)
    assert "did not run a search" not in r.text
