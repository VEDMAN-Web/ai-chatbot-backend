"""
Bulk Property Operations (upload Excel/CSV/PDF/DOCX, Update / Delete / Create, 6-day undo and history), end to end.

The Admin Panel modal calls: POST /admin/properties/batch-file, POST /admin/actions/{id}/confirm,
GET /admin/operations/history, POST /admin/operations/{id}/undo. They are called here with real files and real accounts.
Firestore is the emulator (a unique project per run); PostgreSQL is the throwaway database. Nothing live is touched.
"""
import io
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import PendingAction, User
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)
TAJ, VAR, THAI = "tajlandia.com", "varsoviaestate.com", "thai.estate"
RUN = uuid.uuid4().hex[:6]
PASSWORD = "BulkE2e#123"
BASE = "/api/v1/admin"


@pytest.fixture(scope="module")
def fs():
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Firestore emulator not available (set FIRESTORE_EMULATOR_HOST)")
    from google.cloud import firestore
    return firestore.Client(project=f"bulk-e2e-{RUN}")


@pytest.fixture(autouse=True)
def wiring(fs, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    for target in ("app.services.admin_batch_service.get_firestore_client",
                   "app.services.admin_batch_service.get_snapshot_client",
                   "app.services.firestore_service.get_firestore_client"):
        monkeypatch.setattr(target, lambda *a, **kw: fs)
    monkeypatch.setattr("app.services.firestore_auth_service.get_firestore_client", lambda *a, **kw: None)


def _user(role, sites):
    email = f"bulk_{RUN}_{role}_{uuid.uuid4().hex[:4]}@tajlandia.com"
    with SessionLocal() as db:
        db.add(User(email=email, name=role, role=role, password_hash=hash_password(PASSWORD), is_active=True,
                    allowed_websites=list(sites), domain=TAJ))
        db.commit()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return SimpleNamespace(email=email, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(scope="module")
def people():
    ppl = SimpleNamespace(boss=_user("super_admin", [TAJ, VAR, THAI]), taj=_user("admin", [TAJ]),
                          var=_user("admin", [VAR]), worker=_user("worker", [TAJ]))
    yield ppl
    with SessionLocal() as db:
        db.query(PendingAction).filter(PendingAction.staff_user_id.like(f"bulk_{RUN}_%")).delete(synchronize_session=False)
        db.query(User).filter(User.email.like(f"bulk_{RUN}_%")).delete(synchronize_session=False)
        db.commit()


def _pid(tag):
    return f"PROP-E2E-{RUN.upper()}-{tag}-{uuid.uuid4().hex[:4].upper()}"


def _cid(tag):
    return _pid(tag).lower()


def _seed(fs, pid, site, status="Available", name=None):
    fs.collection("projects").document(pid).set({
        "name": name or f"Seed {pid}", "status": status, "websites": [site], "isActive": True,
        "price": 5_000_000, "location": "Phuket",
    })


def _doc(fs, pid):
    snap = fs.collection("projects").document(pid).get()
    return snap.to_dict() if snap.exists else None


# ---- building files ------------------------------------------------------------------------------------------------

def _csv(rows, header="property_id,name,status"):
    return ("\n".join([header] + rows) + "\n").encode()


def _xlsx(rows, header=("property_id", "name", "status")):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(r.split(","))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx(rows, header=("property_id", "name", "status")):
    import docx
    d = docx.Document()
    t = d.add_table(rows=1, cols=len(header))
    for i, h in enumerate(header):
        t.rows[0].cells[i].text = h
    for r in rows:
        cells = t.add_row().cells
        for i, v in enumerate(r.split(",")):
            cells[i].text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _pdf(lines):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 780
    for line in lines:
        c.drawString(60, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def _scanned_pdf():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(50, 50, 400, 600, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue()


MIME = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pdf": "application/pdf"}


def _upload(who, content, name, action, *sites):
    headers = dict(who.headers)
    if sites:
        headers["X-Selected-Websites"] = ",".join(sites)
    ext = name.rsplit(".", 1)[-1]
    return client.post(f"{BASE}/properties/batch-file", headers=headers,
                       files={"file": (name, content, MIME.get(ext, "application/octet-stream"))}, data={"action": action})


def _confirm(who, pending_id):
    return client.post(f"{BASE}/actions/{pending_id}/confirm", headers=who.headers, json={})


def _history(who):
    return client.get(f"{BASE}/operations/history", headers=who.headers)


def _undo(who, op_id):
    return client.post(f"{BASE}/operations/{op_id}/undo", headers=who.headers)


def _apply(who, content, name, action, *sites):
    up = _upload(who, content, name, action, *sites)
    assert up.status_code == 202, up.text
    done = _confirm(who, up.json()["pending_action_id"])
    assert done.status_code == 200, done.text
    assert "execution_result" in done.json(), done.text
    return done.json()["execution_result"]


# ======================================================================================================
# every supported file type reaches a preview, and nothing is written before the yes
# ======================================================================================================

@pytest.mark.parametrize("kind", ["csv", "xlsx", "docx"])
def test_table_files_update_stage_then_apply(fs, people, kind):
    a, b = _pid("T1"), _pid("T2")
    _seed(fs, a, TAJ), _seed(fs, b, TAJ)
    rows = [f"{a},Seed {a},Sold Out", f"{b},Seed {b},Planned"]
    content = {"csv": _csv, "xlsx": _xlsx, "docx": _docx}[kind](rows)
    up = _upload(people.taj, content, f"update.{kind}", "update", TAJ)
    assert up.status_code == 202, up.text
    body = up.json()
    assert body["status"] == "PROPOSED" and body["item_count"] == 2
    assert _doc(fs, a)["status"] == "Available" and _doc(fs, b)["status"] == "Available"      # nothing written yet
    done = _confirm(people.taj, body["pending_action_id"])
    assert done.status_code == 200, done.text
    assert _doc(fs, a)["status"] == "Sold Out" and _doc(fs, b)["status"] == "Planned"


def test_pdf_with_property_ids_stages_an_update(fs, people):
    a = _pid("P1")
    _seed(fs, a, TAJ)
    up = _upload(people.taj, _pdf(["Offer list", f"{a}"]), "offer.pdf", "update", TAJ)
    assert up.status_code == 202, up.text
    assert [i["property_id"] for i in up.json()["items"]] == [a]


def test_scanned_pdf_is_refused_with_a_clear_message(people):
    up = _upload(people.taj, _scanned_pdf(), "scan.pdf", "update", TAJ)
    assert up.status_code == 400
    assert "ocr" in up.json()["detail"].lower() or "scanned" in up.json()["detail"].lower()


@pytest.mark.parametrize("name,content", [("list.exe", b"MZ..."), ("list.txt", b"nothing useful here"), ("empty.csv", b"")])
def test_unusable_files_are_refused(people, name, content):
    assert _upload(people.taj, content, name, "update", TAJ).status_code == 400


def test_a_csv_with_only_headers_is_refused(people):
    assert _upload(people.taj, _csv([]), "headers.csv", "update", TAJ).status_code == 400


# ======================================================================================================
# update / delete / create, history and undo
# ======================================================================================================

def test_update_history_undo_and_a_second_undo(fs, people):
    a = _pid("U1")
    _seed(fs, a, TAJ)
    result = _apply(people.taj, _csv([f"{a},Seed {a},Sold Out"]), "u.csv", "update", TAJ)
    op = result["operation_id"]
    assert _doc(fs, a)["status"] == "Sold Out"
    hist = _history(people.taj).json()["operations"]
    row = next(o for o in hist if o["operation_id"] == op)
    assert row["can_undo"] is True and row["affected_ids"] == [a] and row["action_type"] == "update"
    undone = _undo(people.taj, op)
    assert undone.status_code == 200, undone.text
    assert _doc(fs, a)["status"] == "Available"
    assert _undo(people.taj, op).status_code == 400            # cannot be undone twice


def test_delete_archives_and_never_removes_the_document(fs, people):
    a = _pid("D1")
    _seed(fs, a, TAJ)
    result = _apply(people.taj, _csv([f"{a},Seed {a},"], header="property_id,name,status"), "d.csv", "delete", TAJ)
    doc = _doc(fs, a)
    assert doc is not None and doc["isActive"] is False, "delete must be a soft delete"
    assert _undo(people.taj, result["operation_id"]).status_code == 200
    assert _doc(fs, a)["isActive"] is True


CREATE_HEADER = "property_id,name,property_type,province,price,status"


def test_create_adds_new_listings_on_the_selected_website(fs, people):
    a = _cid("C1")
    result = _apply(people.taj, _csv([f"{a},Brand New Villa {a},Villa,Phuket,4500000,Available"], CREATE_HEADER),
                    "c.csv", "create", TAJ)
    doc = _doc(fs, a)
    assert doc is not None and doc["websites"] == [TAJ]
    assert _undo(people.taj, result["operation_id"]).status_code == 200
    assert _doc(fs, a) is None                                   # undo of a create removes what it created


def test_create_never_overwrites_an_existing_listing(fs, people):
    a = _cid("C2")
    _seed(fs, a, TAJ, name="Original Name")
    up = _upload(people.taj, _csv([f"{a},Imposter Name,Villa,Phuket,1000,Available"], CREATE_HEADER), "c2.csv", "create", TAJ)
    if up.status_code == 202:
        _confirm(people.taj, up.json()["pending_action_id"])
    assert _doc(fs, a)["name"] == "Original Name"
    assert _doc(fs, a)["price"] == 5_000_000


def test_create_skips_a_name_that_already_exists(fs, people):
    a, b = _cid("C3"), _cid("C3B")
    name = f"Duplicate Name Villa {RUN} c3"
    _seed(fs, a, TAJ, name=name)
    up = _upload(people.taj, _csv([f"{b},{name},Villa,Phuket,4500000,Available"], CREATE_HEADER), "c3.csv", "create", TAJ)
    if up.status_code == 202:
        _confirm(people.taj, up.json()["pending_action_id"])
    assert _doc(fs, b) is None, "a second listing with an existing name was created from a file"


def test_create_with_the_same_id_twice_in_one_file_makes_one_listing(fs, people):
    a = _cid("C4")
    row = f"{a},Twice Villa {a},Villa,Phuket,4500000,Available"
    up = _upload(people.taj, _csv([row, row], CREATE_HEADER), "c4.csv", "create", TAJ)
    if up.status_code == 202:
        assert _confirm(people.taj, up.json()["pending_action_id"]).status_code == 200
    docs = [d for d in fs.collection("projects").where("name", "==", f"Twice Villa {a}").stream()]
    assert len(docs) <= 1


def test_create_with_an_invalid_row_writes_nothing(fs, people):
    good, bad = _cid("C5"), _cid("C5B")
    up = _upload(people.taj, _csv([f"{good},Fine Villa {good},Villa,Phuket,4500000,Available",
                                   f"{bad},Bad Villa {bad},Villa,Phuket,-5,Available"], CREATE_HEADER), "c5.csv", "create", TAJ)
    if up.status_code == 202:
        _confirm(people.taj, up.json()["pending_action_id"])
    assert _doc(fs, bad) is None and _doc(fs, good) is None      # all or nothing


# ======================================================================================================
# who may do what, on which website
# ======================================================================================================

def test_a_worker_cannot_upload_confirm_undo_or_see_history(fs, people):
    assert _upload(people.worker, _csv(["x,y,z"]), "w.csv", "update", TAJ).status_code == 403
    assert _history(people.worker).status_code == 403
    assert _undo(people.worker, "batch_000000000000").status_code == 403


def test_no_token_is_401(people):
    assert client.post(f"{BASE}/properties/batch-file", files={"file": ("a.csv", b"a", "text/csv")}, data={"action": "update"}).status_code == 401
    assert client.get(f"{BASE}/operations/history").status_code == 401


def test_an_admin_cannot_change_or_delete_another_websites_listings(fs, people):
    v = _pid("S1")
    _seed(fs, v, VAR)
    for action, rows in (("update", [f"{v},Seed {v},Sold Out"]), ("delete", [f"{v},Seed {v},"])):
        up = _upload(people.taj, _csv(rows), f"s.{action}.csv", action, TAJ)
        if up.status_code == 202:
            _confirm(people.taj, up.json()["pending_action_id"])
        doc = _doc(fs, v)
        assert doc["status"] == "Available" and doc["isActive"] is True, f"{action} reached another website's listing"


def test_a_mixed_file_only_touches_the_admins_own_website(fs, people):
    mine, theirs = _pid("S2"), _pid("S2B")
    _seed(fs, mine, TAJ), _seed(fs, theirs, VAR)
    up = _upload(people.taj, _csv([f"{mine},Seed {mine},Sold Out", f"{theirs},Seed {theirs},Sold Out"]), "mix.csv", "update", TAJ)
    if up.status_code == 202:
        _confirm(people.taj, up.json()["pending_action_id"])
    assert _doc(fs, mine)["status"] == "Sold Out"
    assert _doc(fs, theirs)["status"] == "Available"


def test_create_cannot_target_a_website_outside_the_account(fs, people):
    a = _cid("S3")
    header = CREATE_HEADER + ",websites"
    up = _upload(people.taj, _csv([f"{a},Cross Site Villa {a},Villa,Phuket,4500000,Available,{THAI}"], header), "s3.csv", "create", TAJ)
    if up.status_code == 202:
        _confirm(people.taj, up.json()["pending_action_id"])
    doc = _doc(fs, a)
    assert doc is None or THAI not in (doc.get("websites") or [])


def test_super_admin_may_work_on_any_website(fs, people):
    v = _pid("S4")
    _seed(fs, v, VAR)
    _apply(people.boss, _csv([f"{v},Seed {v},Sold Out"]), "boss.csv", "update", VAR)
    assert _doc(fs, v)["status"] == "Sold Out"


def test_another_websites_admin_cannot_undo_or_see_the_operation(fs, people):
    a = _pid("H1")
    _seed(fs, a, TAJ)
    op = _apply(people.taj, _csv([f"{a},Seed {a},Sold Out"]), "h.csv", "update", TAJ)["operation_id"]
    assert op not in [o["operation_id"] for o in _history(people.var).json()["operations"]]
    assert _undo(people.var, op).status_code in (403, 404)
    assert _doc(fs, a)["status"] == "Sold Out"
    assert op in [o["operation_id"] for o in _history(people.boss).json()["operations"]]


def test_only_the_uploader_can_confirm(fs, people):
    a = _pid("H2")
    _seed(fs, a, TAJ)
    up = _upload(people.taj, _csv([f"{a},Seed {a},Sold Out"]), "o.csv", "update", TAJ)
    assert _confirm(people.boss, up.json()["pending_action_id"]).status_code == 403
    assert _doc(fs, a)["status"] == "Available"


def test_confirming_twice_applies_once(fs, people):
    a = _pid("H3")
    _seed(fs, a, TAJ)
    up = _upload(people.taj, _csv([f"{a},Seed {a},Sold Out"]), "t.csv", "update", TAJ).json()
    assert _confirm(people.taj, up["pending_action_id"]).status_code == 200
    assert _confirm(people.taj, up["pending_action_id"]).status_code >= 400


# ======================================================================================================
# snapshots: persistent, expiring
# ======================================================================================================

def test_the_undo_works_from_the_stored_snapshot_alone(fs, people):
    """Nothing is held in memory: the operation record lives in Firestore, so a restarted server can still undo."""
    a = _pid("R1")
    _seed(fs, a, TAJ)
    op = _apply(people.taj, _csv([f"{a},Seed {a},Sold Out"]), "r.csv", "update", TAJ)["operation_id"]
    snap = fs.collection("admin_batch_operations").document(op).get().to_dict()
    assert snap["status"] == "APPLIED" and snap["previous_snapshot"] and snap["expire_at"]
    fresh_client = TestClient(app)          # a new client = no state carried over
    assert fresh_client.post(f"{BASE}/operations/{op}/undo", headers=people.taj.headers).status_code == 200


def test_an_expired_operation_cannot_be_undone_and_is_hidden(fs, people):
    a = _pid("R2")
    _seed(fs, a, TAJ)
    op = _apply(people.taj, _csv([f"{a},Seed {a},Sold Out"]), "e.csv", "update", TAJ)["operation_id"]
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    fs.collection("admin_batch_operations").document(op).update({"expire_at": past, "expires_at": past})
    assert _undo(people.taj, op).status_code == 400
    assert op not in [o["operation_id"] for o in _history(people.taj).json()["operations"]]
    assert _doc(fs, a)["status"] == "Sold Out"


def test_undoing_an_unknown_operation_is_a_clean_error(people):
    res = _undo(people.taj, "batch_doesnotexist")
    assert res.status_code in (400, 404)
