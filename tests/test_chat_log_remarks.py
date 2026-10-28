"""Remarks and answer corrections on logged responses: permissions, revisions, review workflow, fail-safe audit.

Needs a Firestore emulator (FIRESTORE_EMULATOR_HOST) for the log and PostgreSQL for accounts and the audit trail.
"""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.services import chat_log_remarks as remarks
from app.services import chat_log_service as logs

# fixtures and helpers shared with the log review tests
from test_chat_log_api import BASE, PREFIX, _at, _log, _staff, client, fs  # noqa: F401

ANSWER = "m000002"
QUESTION = "m000001"


@pytest.fixture(autouse=True)
def _audit_on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    yield


@pytest.fixture
def world(fs, monkeypatch):
    people = SimpleNamespace(
        boss=_staff("super_admin", ["tajlandia.com", "thai.estate", "varsoviaestate.com"]),
        admin=_staff("admin", ["tajlandia.com"]),
        admin2=_staff("admin", ["tajlandia.com"]),
        other_site=_staff("admin", ["thai.estate"]),
        worker=_staff("worker", ["tajlandia.com"]),
    )
    conv = _log(monkeypatch, "public", "v1", "tajlandia.com", 20, "What is the price of Alpha Villa?", "Alpha Villa costs 9M THB.",
                structured_data={"property_ids": ["PHK-1"]})
    yield SimpleNamespace(people=people, conv=conv, fs=fs)
    for name in (remarks.REMARKS, remarks.CORRECTIONS):
        fs.recursive_delete(fs.collection(name))
    db = SessionLocal()
    try:
        db.query(AuditLog).filter(AuditLog.tool_name.like("correction:%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _add_remark(w, who, issue="The price is outdated", suggested=None, message=ANSWER):
    body = {"issue": issue}
    if suggested:
        body["suggested_answer"] = suggested
    return client.post(f"{BASE}/conversations/{w.conv}/messages/{message}/remarks", headers=who["headers"], json=body)


def _propose(w, who, remark_id, answer="Alpha Villa costs 8.5M THB.", **extra):
    return client.post(f"{BASE}/remarks/{remark_id}/correction", headers=who["headers"], json={"corrected_answer": answer, **extra})


def _decide(w, who, correction_id, decision, note=None):
    return client.post(f"{BASE}/corrections/{correction_id}/{decision}", headers=who["headers"], json={"note": note} if note else {})


def _message(w, who, message_id=ANSWER):
    body = client.get(f"{BASE}/conversations/{w.conv}", headers=who["headers"]).json()
    return next(m for m in body["messages"] if m["message_id"] == message_id)


# ---- remarks -------------------------------------------------------------------------------------

def test_an_admin_adds_a_remark_to_a_response(world):
    resp = _add_remark(world, world.people.admin)
    assert resp.status_code == 201, resp.text
    remark = resp.json()
    assert remark["remark_id"].startswith("RM-") and remark["kind"] == "remark" and remark["status"] == "open"
    assert remark["author"]["email"] == world.people.admin["email"] and remark["created_at"] and remark["revision"] == 1
    assert remark["conversation_id"] == world.conv and remark["message_id"] == ANSWER
    assert _message(world, world.people.admin)["remark_status"] == "open"
    listed = client.get(f"{BASE}/conversations", headers=world.people.boss["headers"], params={"remark_status": "open"}).json()
    assert [i["conversation_id"] for i in listed["items"]] == [world.conv] and listed["items"][0]["remark_statuses"] == ["open"]


def test_a_remark_never_changes_the_log_or_the_bot(world):
    before = _message(world, world.people.boss)["content"]
    _add_remark(world, world.people.admin, suggested="Alpha Villa costs 8.5M THB.")
    assert _message(world, world.people.boss)["content"] == before          # history untouched
    assert remarks.active_corrections() == []                               # nothing for the bot to use


def test_remark_input_rules(world):
    admin = world.people.admin
    assert _add_remark(world, admin, issue="no").status_code == 422                       # too short
    assert _add_remark(world, admin, issue="x" * 2001).status_code == 422
    assert _add_remark(world, admin, suggested="ab").status_code == 422
    assert _add_remark(world, admin, message=QUESTION).status_code == 422                 # only bot responses
    assert _add_remark(world, admin, message="m000099").status_code == 404
    assert client.post(f"{BASE}/conversations/{world.conv}/messages/{ANSWER}/remarks", headers=admin["headers"], json={}).status_code == 422


def test_remark_permissions_and_visibility(world):
    assert _add_remark(world, world.people.worker).status_code == 403              # workers cannot add remarks
    assert _add_remark(world, world.people.other_site).status_code == 404          # cannot see that conversation
    assert client.post(f"{BASE}/conversations/{world.conv}/messages/{ANSWER}/remarks").status_code == 401
    remark_id = _add_remark(world, world.people.admin).json()["remark_id"]
    assert client.get(f"{BASE}/remarks/{remark_id}", headers=world.people.other_site["headers"]).status_code == 404
    assert client.get(f"{BASE}/remarks/{remark_id}", headers=world.people.admin2["headers"]).status_code == 200  # same website
    listed = client.get(f"{BASE}/remarks", headers=world.people.other_site["headers"]).json()
    assert listed["items"] == [] and listed["total"] == 0


def test_secrets_in_a_remark_are_not_stored(world):
    resp = _add_remark(world, world.people.admin, issue="Used key sk-live-ABCDEFGHIJKLMNOP wrongly")
    assert "sk-live-ABCDEFGHIJKLMNOP" not in resp.json()["issue"]


def test_editing_a_remark_keeps_every_revision(world):
    author, other, boss = world.people.admin, world.people.admin2, world.people.boss
    remark_id = _add_remark(world, author).json()["remark_id"]
    edited = client.patch(f"{BASE}/remarks/{remark_id}", headers=author["headers"], json={"issue": "The price is outdated and the currency is missing"})
    assert edited.status_code == 200 and edited.json()["revision"] == 2
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=other["headers"], json={"issue": "someone else edits"}).status_code == 403
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=boss["headers"], json={"suggested_answer": "Use 8.5M THB"}).json()["revision"] == 3
    revisions = client.get(f"{BASE}/remarks/{remark_id}/revisions", headers=author["headers"]).json()["items"]
    assert [r["revision"] for r in revisions] == [1, 2, 3] and [r["change"] for r in revisions] == ["created", "updated", "updated"]
    assert revisions[1]["changed"]["issue"]["from"] == "The price is outdated" and revisions[1]["by"]["email"] == author["email"]
    assert revisions[2]["by"]["email"] == boss["email"]
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=author["headers"], json={}).status_code == 422


def test_only_a_reviewer_changes_a_remark_status(world):
    author, boss = world.people.admin, world.people.boss
    remark_id = _add_remark(world, author).json()["remark_id"]
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=author["headers"], json={"status": "resolved"}).status_code == 403
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=boss["headers"], json={"status": "bogus"}).status_code == 422
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=boss["headers"], json={"status": "in_review"}).json()["status"] == "in_review"
    assert _message(world, boss)["remark_status"] == "in_review"
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=boss["headers"], json={"status": "resolved"}).json()["status"] == "resolved"
    assert _message(world, boss)["remark_status"] == "resolved"
    assert client.patch(f"{BASE}/remarks/{remark_id}", headers=author["headers"], json={"issue": "too late to edit"}).status_code == 409
    listed = client.get(f"{BASE}/remarks", headers=boss["headers"], params={"status": "resolved"}).json()
    assert [r["remark_id"] for r in listed["items"]] == [remark_id]
    assert client.get(f"{BASE}/remarks", headers=boss["headers"], params={"status": "open"}).json()["items"] == []


def test_message_status_follows_all_its_remarks(world):
    boss, admin = world.people.boss, world.people.admin
    first = _add_remark(world, admin).json()["remark_id"]
    second = _add_remark(world, admin, issue="Also the area is wrong").json()["remark_id"]
    client.patch(f"{BASE}/remarks/{first}", headers=boss["headers"], json={"status": "resolved"})
    assert _message(world, boss)["remark_status"] == "open"            # one is still open
    client.patch(f"{BASE}/remarks/{second}", headers=boss["headers"], json={"status": "rejected"})
    assert _message(world, boss)["remark_status"] == "resolved"
    header = client.get(f"{BASE}/conversations", headers=boss["headers"]).json()["items"][0]
    assert header["remark_statuses"] == ["resolved", "rejected"]


# ---- corrections ---------------------------------------------------------------------------------

def test_a_correction_is_a_proposal_until_approved(world):
    admin = world.people.admin
    remark_id = _add_remark(world, admin).json()["remark_id"]
    resp = _propose(world, admin, remark_id)
    assert resp.status_code == 201, resp.text
    correction = resp.json()
    assert correction["correction_id"].startswith("COR-") and correction["kind"] == "answer_correction" and correction["status"] == "proposed"
    assert correction["question"] == "What is the price of Alpha Villa?" and correction["original_answer"] == "Alpha Villa costs 9M THB."
    assert correction["corrected_answer"] == "Alpha Villa costs 8.5M THB." and correction["version"] == 0
    # by default it applies only where the corrected response was about the same property (PHK-1 in this log)
    assert correction["scope"] == {"websites": ["tajlandia.com"], "languages": [], "propertyIds": ["PHK-1"]}
    assert correction["proposed_by"]["email"] == admin["email"]
    assert remarks.active_corrections() == []                                       # not usable yet
    assert client.get(f"{BASE}/remarks/{remark_id}", headers=admin["headers"]).json()["status"] == "in_review"
    assert _propose(world, admin, remark_id).status_code == 409                    # one proposal per remark


def test_correction_input_and_scope_rules(world):
    admin = world.people.admin
    remark_id = _add_remark(world, admin).json()["remark_id"]
    assert _propose(world, admin, remark_id, answer="ab").status_code == 422
    assert _propose(world, admin, remark_id, websites=["thai.estate"]).status_code == 403   # outside the caller's websites
    assert _propose(world, world.people.worker, remark_id).status_code == 403
    assert _propose(world, world.people.other_site, remark_id).status_code == 404
    ok = _propose(world, admin, remark_id, languages=["EN", " pl "], property_ids=["PHK-1", "PHK-1"]).json()
    assert ok["scope"] == {"websites": ["tajlandia.com"], "languages": ["en", "pl"], "propertyIds": ["PHK-1"]}


def test_only_a_super_admin_decides_and_approval_publishes_it(world):
    admin, boss = world.people.admin, world.people.boss
    remark_id = _add_remark(world, admin).json()["remark_id"]
    correction_id = _propose(world, admin, remark_id).json()["correction_id"]
    for decision in ("approve", "reject", "deactivate"):
        assert _decide(world, admin, correction_id, decision).status_code == 403
        assert _decide(world, world.people.worker, correction_id, decision).status_code == 403
    assert remarks.active_corrections() == []

    approved = _decide(world, boss, correction_id, "approve", note="Checked against the price list")
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved" and body["version"] == 1 and body["reviewed_by"]["email"] == boss["email"]
    assert body["reviewed_at"] and body["review_note"] == "Checked against the price list"
    active = remarks.active_corrections()
    assert [c["correction_id"] for c in active] == [correction_id] and active[0]["corrected_answer"] == "Alpha Villa costs 8.5M THB."
    remark = client.get(f"{BASE}/remarks/{remark_id}", headers=admin["headers"]).json()
    assert remark["status"] == "resolved" and remark["knowledge_entry_id"] == correction_id and remark["correction_id"] == correction_id
    assert _message(world, boss)["remark_status"] == "resolved"
    assert _message(world, boss)["content"] == "Alpha Villa costs 9M THB."          # the original answer is preserved


def test_rejecting_keeps_it_away_from_the_bot(world):
    admin, boss = world.people.admin, world.people.boss
    remark_id = _add_remark(world, admin).json()["remark_id"]
    correction_id = _propose(world, admin, remark_id).json()["correction_id"]
    rejected = _decide(world, boss, correction_id, "reject", note="Not accurate")
    assert rejected.json()["status"] == "rejected" and rejected.json()["version"] == 0
    assert remarks.active_corrections() == []
    assert client.get(f"{BASE}/remarks/{remark_id}", headers=admin["headers"]).json()["status"] == "rejected"
    assert _decide(world, boss, correction_id, "approve").status_code == 409        # a rejected correction cannot be approved
    assert _decide(world, boss, correction_id, "deactivate").status_code == 409
    again = _propose(world, admin, remark_id, answer="Alpha Villa costs about 8.5M THB.")   # a new proposal is allowed after rejection
    assert again.status_code == 201


def test_editing_an_approved_correction_requires_approval_again(world):
    admin, boss = world.people.admin, world.people.boss
    remark_id = _add_remark(world, admin).json()["remark_id"]
    correction_id = _propose(world, admin, remark_id).json()["correction_id"]
    _decide(world, boss, correction_id, "approve")
    assert len(remarks.active_corrections()) == 1
    edited = client.patch(f"{BASE}/corrections/{correction_id}", headers=admin["headers"], json={"corrected_answer": "Alpha Villa starts at 8.4M THB."})
    assert edited.status_code == 200 and edited.json()["status"] == "proposed" and edited.json()["reviewed_by"] is None
    assert remarks.active_corrections() == []                                       # not used until re-approved
    assert client.patch(f"{BASE}/corrections/{correction_id}", headers=world.people.admin2["headers"], json={"corrected_answer": "hijack the answer"}).status_code == 403
    assert _decide(world, boss, correction_id, "approve").json()["version"] == 2
    assert [c["corrected_answer"] for c in remarks.active_corrections()] == ["Alpha Villa starts at 8.4M THB."]


def test_an_approved_correction_can_be_deactivated_and_restored(world):
    admin, boss = world.people.admin, world.people.boss
    remark_id = _add_remark(world, admin).json()["remark_id"]
    correction_id = _propose(world, admin, remark_id).json()["correction_id"]
    assert _decide(world, boss, correction_id, "deactivate").status_code == 409     # only an approved one
    _decide(world, boss, correction_id, "approve")
    assert _decide(world, boss, correction_id, "deactivate", note="Prices changed").json()["status"] == "inactive"
    assert remarks.active_corrections() == []
    assert _decide(world, boss, correction_id, "approve").json()["status"] == "approved"
    assert len(remarks.active_corrections()) == 1


def test_correction_history_lists_every_step(world):
    admin, boss = world.people.admin, world.people.boss
    remark_id = _add_remark(world, admin).json()["remark_id"]
    correction_id = _propose(world, admin, remark_id).json()["correction_id"]
    client.patch(f"{BASE}/corrections/{correction_id}", headers=admin["headers"], json={"corrected_answer": "Alpha Villa costs 8.6M THB."})
    _decide(world, boss, correction_id, "approve", note="ok")
    steps = client.get(f"{BASE}/corrections/{correction_id}/revisions", headers=admin["headers"]).json()["items"]
    assert [s["change"] for s in steps] == ["proposed", "edited", "approve"] and [s["revision"] for s in steps] == [1, 2, 3]
    assert steps[2]["by"]["email"] == boss["email"] and steps[2]["note"] == "ok"
    remark_steps = client.get(f"{BASE}/remarks/{remark_id}/revisions", headers=admin["headers"]).json()["items"]
    assert [s["change"] for s in remark_steps] == ["created", "correction_proposed", "correction_approved"]


def test_correction_lists_and_visibility(world):
    admin, boss = world.people.admin, world.people.boss
    remark_id = _add_remark(world, admin).json()["remark_id"]
    correction_id = _propose(world, admin, remark_id).json()["correction_id"]
    assert [c["correction_id"] for c in client.get(f"{BASE}/corrections", headers=boss["headers"], params={"status": "proposed"}).json()["items"]] == [correction_id]
    assert client.get(f"{BASE}/corrections", headers=boss["headers"], params={"status": "approved"}).json()["items"] == []
    assert client.get(f"{BASE}/corrections/{correction_id}", headers=world.people.other_site["headers"]).status_code == 404
    assert client.get(f"{BASE}/corrections", headers=world.people.other_site["headers"]).json()["items"] == []
    assert client.get(f"{BASE}/corrections", headers=boss["headers"], params={"status": "bogus"}).status_code == 422
    assert client.get(f"{BASE}/corrections/COR-NOPE", headers=boss["headers"]).status_code == 404


# ---- audit: written first, and a failure blocks the change ----------------------------------------------

def _audit_rows(tool):
    db = SessionLocal()
    try:
        return [(r.status, r.staff_user_id, r.proposed_payload) for r in db.query(AuditLog).filter(AuditLog.tool_name == tool).order_by(AuditLog.created_at).all()]
    finally:
        db.close()


def test_an_approval_is_audited_before_and_after(world):
    admin, boss = world.people.admin, world.people.boss
    correction_id = _propose(world, admin, _add_remark(world, admin).json()["remark_id"]).json()["correction_id"]
    _decide(world, boss, correction_id, "approve", note="ok")
    rows = _audit_rows("correction:approve")
    assert [r[0] for r in rows] == ["PENDING", "SUCCESS"] and all(r[1] == boss["email"] for r in rows)
    assert rows[0][2]["correction_id"] == correction_id and rows[0][2]["from_status"] == "proposed" and rows[0][2]["to_status"] == "approved"


def test_no_audit_trail_means_no_approval(world, monkeypatch):
    admin, boss = world.people.admin, world.people.boss
    correction_id = _propose(world, admin, _add_remark(world, admin).json()["remark_id"]).json()["correction_id"]

    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", False)           # auditing switched off
    resp = _decide(world, boss, correction_id, "approve")
    assert resp.status_code == 503 and "blocked" in resp.json()["detail"]
    assert client.get(f"{BASE}/corrections/{correction_id}", headers=boss["headers"]).json()["status"] == "proposed"

    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    import app.services.audit_service as audit

    def failing_audit(**kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(audit, "record_audit_log", failing_audit)           # audit store failing
    assert _decide(world, boss, correction_id, "approve").status_code == 503
    assert client.get(f"{BASE}/corrections/{correction_id}", headers=boss["headers"]).json()["status"] == "proposed"
    assert remarks.active_corrections() == []
