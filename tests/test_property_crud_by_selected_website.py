"""
Property create / read / update / delete by role and by the website(s) selected in the Admin Panel.

Two entry points are exercised with real accounts and real tokens (in-memory Firestore fake):
  * REST  /api/v1/admin/properties        (the panel's forms)
  * CHAT  propose with the admin-chat tool, confirm with POST /api/v1/admin/actions/{id}/confirm
          (what happens when the admin asks the bot to add or change a listing)

Accounts: super admin (all three sites), admin for tajlandia only, admin for tajlandia + varsoviaestate, worker (tajlandia).
The selected website travels in the X-Selected-Websites header, exactly as the panel sends it.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedStaff
from app.core.config import settings
from app.core.deps import FirestoreSession
from app.repositories import users_repo
from app.main import app
from tests import fs_users
from app.services.admin_agent import _execute_admin_tool

client = TestClient(app)
TAJ, VAR, THAI = "tajlandia.com", "varsoviaestate.com", "thai.estate"
PASSWORD = "CrudSite#123"
RUN = uuid.uuid4().hex[:6].translate(str.maketrans("0123456789", "ghijklmnop"))  # letters only: digits in a name get parsed as a price
URL = "/api/v1/admin/properties"


@pytest.fixture(autouse=True)
def no_sync(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)


def _make_user(role, sites):
    email = f"crud_{RUN}_{role}_{uuid.uuid4().hex[:4]}@tajlandia.com"
    made = fs_users.make_user(client, role, email=email, password=PASSWORD, allowed_websites=list(sites))
    uid = str(users_repo.get_by_email(None, email).id)
    return SimpleNamespace(id=uid, email=email, role=role, sites=list(sites),
                           token=made["headers"]["Authorization"].split(" ", 1)[1])


def _h(who, *selected):
    headers = {"Authorization": f"Bearer {who.token}"}
    if selected:
        headers["X-Selected-Websites"] = ",".join(selected)
    return headers


@pytest.fixture
def people():
    return SimpleNamespace(
        boss=_make_user("super_admin", [TAJ, VAR, THAI]),
        taj=_make_user("admin", [TAJ]),
        dual=_make_user("admin", [TAJ, VAR]),
        worker=_make_user("worker", [TAJ]),
    )


def _pid(tag):
    return f"crud-{RUN}-{tag}-{uuid.uuid4().hex[:4]}"


def _body(pid, name=None, **extra):
    body = {"property_id": pid, "property_name": name or f"Villa {pid}", "property_type": "Villa",
            "province": "Phuket", "price_numeric_min": 2_500_000, "status": "Available"}
    body.update(extra)
    return body


def _create(who, *selected, **body):
    return client.post(f"{URL}?allow_without_undo=true", headers=_h(who, *selected), json=body)


def _raw(pid):
    from app.services import firestore_service as fsvc
    snap = fsvc.get_admin_firestore_client().collection("projects").document(pid).get()
    return snap.to_dict() if snap.exists else None


def _row(pid):
    raw = _raw(pid)
    if raw is None:
        return None
    status = raw.get("status")
    public = raw.get("isActive") is not False and str(status).lower() != "archived"
    return SimpleNamespace(
        websites=list(raw.get("websites") or []), status=status, is_public=public,
        name=raw.get("name") or raw.get("property_name"),
        price=raw.get("priceFrom") if raw.get("priceFrom") is not None else raw.get("price_numeric_min"),
    )


def _count(name_like):
    from app.services import firestore_service as fsvc
    wanted = name_like.strip().lower()
    return sum(
        1 for d in fsvc.get_admin_firestore_client().collection("projects").stream()
        if str((d.to_dict() or {}).get("name") or (d.to_dict() or {}).get("property_name") or "").strip().lower() == wanted
    )


# ======================================================================================================
# REST: create
# ======================================================================================================

def test_create_lands_on_the_selected_website_only(people):
    pid = _pid("c1")
    res = _create(people.dual, VAR, **_body(pid))
    assert res.status_code == 201, res.text
    assert _row(pid).websites == [VAR]


def test_create_with_two_selected_websites_lands_on_both(people):
    pid = _pid("c2")
    assert _create(people.dual, TAJ, VAR, **_body(pid)).status_code == 201
    assert sorted(_row(pid).websites) == sorted([TAJ, VAR])


def test_create_with_no_selection_uses_every_website_the_admin_has(people):
    pid = _pid("c3")
    assert _create(people.dual, **_body(pid)).status_code == 201
    assert sorted(_row(pid).websites) == sorted([TAJ, VAR])


def test_create_for_a_website_not_selected_is_refused(people):
    pid = _pid("c4")
    res = _create(people.dual, TAJ, **_body(pid, websites=[VAR]))
    assert res.status_code == 400 and _row(pid) is None


def test_create_for_a_website_outside_the_account_is_refused(people):
    pid = _pid("c5")
    assert _create(people.taj, TAJ, **_body(pid, websites=[THAI])).status_code in (400, 403)
    assert _row(pid) is None


def test_selecting_a_website_the_admin_does_not_have_is_403(people):
    pid = _pid("c6")
    assert _create(people.taj, VAR, **_body(pid)).status_code == 403
    assert _row(pid) is None


def test_super_admin_can_create_on_any_website(people):
    for site in (TAJ, VAR, THAI):
        pid = _pid("c7")
        assert _create(people.boss, site, **_body(pid)).status_code == 201
        assert _row(pid).websites == [site]


def test_worker_cannot_create(people):
    pid = _pid("c8")
    assert _create(people.worker, TAJ, **_body(pid)).status_code == 403
    assert _row(pid) is None


def test_no_token_is_401(people):
    assert client.post(URL, json=_body(_pid("c9"))).status_code == 401


# ---- duplicates ------------------------------------------------------------------------------------

def test_the_same_id_twice_is_a_conflict_and_leaves_one_row(people):
    pid = _pid("d1")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    again = _create(people.taj, TAJ, **_body(pid, name="Another Name Entirely"))
    assert again.status_code == 409
    assert _row(pid).name == f"Villa {pid}"


def test_the_same_name_with_a_new_id_is_a_conflict(people):
    name = f"Unique Sunset Villa {RUN} d2"
    assert _create(people.taj, TAJ, **_body(_pid("d2"), name=name)).status_code == 201
    assert _create(people.taj, TAJ, **_body(_pid("d2b"), name=name)).status_code == 409
    assert _count(name) == 1


def test_the_same_name_with_different_case_and_spacing_is_a_conflict(people):
    name = f"Quiet Garden Residence {RUN} d3"
    assert _create(people.taj, TAJ, **_body(_pid("d3"), name=name)).status_code == 201
    assert _create(people.taj, TAJ, **_body(_pid("d3b"), name=f"  {name.upper()}  ")).status_code == 409


def test_a_duplicate_is_refused_even_across_websites(people):
    """An admin who sees only varsovia must not be able to create a twin of a tajlandia listing either."""
    name = f"Shared Name Residence {RUN} d4"
    assert _create(people.dual, TAJ, **_body(_pid("d4"), name=name)).status_code == 201
    assert _create(people.dual, VAR, **_body(_pid("d4b"), name=name)).status_code == 409


# ---- validation --------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"property_name": ""}, {"property_type": ""}, {"price_numeric_min": 0}, {"price_numeric_min": -5},
    {"status": "Not A Status"}, {"property_id": "Bad ID With Spaces"},
])
def test_invalid_create_is_422_and_stores_nothing(people, change):
    body = _body(_pid("v1"))
    body.update(change)
    pid = body["property_id"]
    res = _create(people.taj, TAJ, **body)
    assert res.status_code == 422, res.text
    assert _row(pid) is None


# ======================================================================================================
# REST: read
# ======================================================================================================

def test_list_shows_only_the_selected_website(people):
    a, b = _pid("r1"), _pid("r1b")
    assert _create(people.dual, TAJ, **_body(a)).status_code == 201
    assert _create(people.dual, VAR, **_body(b)).status_code == 201
    def listed(site):
        rows = []
        for offset in range(0, 2000, 200):
            res = client.get(URL, headers=_h(people.dual, site), params={"limit": 200, "offset": offset})
            assert res.status_code == 200, res.text
            page = res.json()["properties"]
            rows += page
            if len(page) < 200:
                break
        return rows

    rows_taj, rows_var = listed(TAJ), listed(VAR)
    ids_taj, ids_var = {p["property_id"] for p in rows_taj}, {p["property_id"] for p in rows_var}
    assert a in ids_taj and b not in ids_taj
    assert b in ids_var and a not in ids_var
    assert all(TAJ in p["websites"] for p in rows_taj) and all(VAR in p["websites"] for p in rows_var)


def test_get_one_on_another_website_is_refused(people):
    pid = _pid("r2")
    assert _create(people.dual, VAR, **_body(pid)).status_code == 201
    assert client.get(f"{URL}/{pid}", headers=_h(people.dual, VAR)).status_code == 200
    assert client.get(f"{URL}/{pid}", headers=_h(people.dual, TAJ)).status_code in (403, 404)
    assert client.get(f"{URL}/{pid}", headers=_h(people.taj, TAJ)).status_code in (403, 404)   # no access to varsovia at all


def test_worker_can_read_their_own_website(people):
    pid = _pid("r3")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    assert client.get(f"{URL}/{pid}", headers=_h(people.worker, TAJ)).status_code == 200
    assert client.get(URL, headers=_h(people.worker, TAJ)).status_code == 200


# ======================================================================================================
# REST: update
# ======================================================================================================

def test_update_on_the_selected_website_changes_only_what_was_sent(people):
    pid = _pid("u1")
    assert _create(people.dual, VAR, **_body(pid)).status_code == 201
    res = client.put(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.dual, VAR), json={"price_numeric_min": 3_100_000})
    assert res.status_code == 200, res.text
    row = _row(pid)
    assert row.price == 3_100_000 and row.websites == [VAR] and row.name == f"Villa {pid}"


def test_update_cannot_reach_a_listing_of_a_website_that_is_not_selected(people):
    pid = _pid("u2")
    assert _create(people.dual, VAR, **_body(pid)).status_code == 201
    res = client.put(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.dual, TAJ), json={"price_numeric_min": 1_000_000})
    assert res.status_code in (403, 404)
    assert _row(pid).price == 2_500_000


def test_update_cannot_reach_a_website_the_admin_does_not_have(people):
    pid = _pid("u3")
    assert _create(people.dual, VAR, **_body(pid)).status_code == 201
    res = client.put(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.taj, TAJ), json={"price_numeric_min": 1_000_000})
    assert res.status_code in (403, 404)
    assert _row(pid).price == 2_500_000


def test_update_cannot_move_a_listing_to_a_website_outside_the_account(people):
    pid = _pid("u4")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    res = client.put(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.taj, TAJ), json={"websites": [THAI]})
    assert res.status_code in (400, 403, 422)
    assert _row(pid).websites == [TAJ]


def test_update_cannot_rename_into_a_duplicate(people):
    first, second = _pid("u5"), _pid("u5b")
    name = f"Taken Name Villa {RUN} u5"
    assert _create(people.taj, TAJ, **_body(first, name=name)).status_code == 201
    assert _create(people.taj, TAJ, **_body(second)).status_code == 201
    res = client.put(f"{URL}/{second}?allow_without_undo=true", headers=_h(people.taj, TAJ), json={"property_name": name})
    assert res.status_code in (409, 422), res.text
    assert _row(second).name == f"Villa {second}"


def test_worker_cannot_update(people):
    pid = _pid("u6")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    res = client.put(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.worker, TAJ), json={"price_numeric_min": 1})
    assert res.status_code == 403 and _row(pid).price == 2_500_000


def test_update_of_a_missing_property_is_404(people):
    assert client.put(f"{URL}/crud-{RUN}-nothing?allow_without_undo=true", headers=_h(people.taj, TAJ),
                      json={"price_numeric_min": 5}).status_code == 404


# ======================================================================================================
# REST: delete (archive, never permanent)
# ======================================================================================================

def test_delete_archives_and_keeps_the_row(people):
    pid = _pid("x1")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    res = client.delete(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.taj, TAJ))
    assert res.status_code == 200, res.text
    row = _row(pid)
    assert row is not None and row.status == "Archived" and row.is_public is False


def test_permanent_delete_is_refused(people):
    pid = _pid("x2")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    res = client.delete(f"{URL}/{pid}?hard_delete=true&confirm_property_id={pid}&allow_without_undo=true", headers=_h(people.taj, TAJ))
    assert res.status_code == 403 and _row(pid) is not None


def test_delete_cannot_reach_another_website(people):
    pid = _pid("x3")
    assert _create(people.dual, VAR, **_body(pid)).status_code == 201
    assert client.delete(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.dual, TAJ)).status_code in (403, 404)
    assert client.delete(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.taj, TAJ)).status_code in (403, 404)
    assert _row(pid).status == "Available"


def test_worker_cannot_delete(people):
    pid = _pid("x4")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    assert client.delete(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.worker, TAJ)).status_code == 403
    assert _row(pid).status == "Available"


def test_deleting_twice_is_harmless(people):
    pid = _pid("x5")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    assert client.delete(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.taj, TAJ)).status_code == 200
    again = client.delete(f"{URL}/{pid}?allow_without_undo=true", headers=_h(people.taj, TAJ))
    assert again.status_code in (200, 404, 409)
    assert _row(pid).status == "Archived"


# ======================================================================================================
# CHAT: the admin asks the bot; the change waits for a yes, then lands on the selected website
# ======================================================================================================

def _staff(who, *selected):
    return AuthenticatedStaff(user_id=who.id, email=who.email, name=who.role, role=who.role,
                              allowed_websites=list(who.sites), selected_websites=list(selected or who.sites))


def _propose(who, selected, fields, pid, mode="create"):
    args = {"property_id": pid, "mode": mode, "fields_to_update": fields}
    return _execute_admin_tool(db=FirestoreSession(), tool_name="create_or_update_listing", tool_args=args,
                               staff_user=_staff(who, *selected), conversation_id=str(uuid.uuid4()))


def _fields(name=None, **extra):
    base = {"property_name": name or f"Chat Villa {uuid.uuid4().hex[:6]}", "property_type": "Villa",
            "province": "Phuket", "price_numeric_min": 4_000_000, "status": "Available"}
    base.update(extra)
    return base


def _confirm(who, action_id, *selected):
    return client.post(f"/api/v1/admin/actions/{action_id}/confirm", headers=_h(who, *selected),
                       json={"confirmation_token": fs_users.pending_token(action_id)})


def _pending_count(who):
    return len(fs_users.pending_for(who.email))


def test_chat_create_waits_for_a_yes_and_lands_on_the_selected_website(people):
    pid = _pid("k1")
    result, staged, action_id = _propose(people.dual, [VAR], _fields(), pid)
    assert staged and result["status"] == "PROPOSED", result
    assert _row(pid) is None                                        # nothing written before the yes
    res = _confirm(people.dual, action_id, VAR)
    assert res.status_code == 200, res.text
    assert _row(pid) is not None
    assert _row(pid).websites == [VAR], "the listing must land on the website the admin had selected"


def test_chat_create_lands_on_the_selected_website_not_the_environment_default(people):
    """The environment default is not what the admin selected: select each site in turn and check where it lands."""
    for site in (TAJ, VAR):
        pid = _pid("k1b")
        _r, staged, action_id = _propose(people.dual, [site], _fields(), pid)
        assert staged and _confirm(people.dual, action_id, site).status_code == 200
        assert _row(pid).websites == [site]
    pid = _pid("k1c")
    _r, staged, action_id = _propose(people.boss, [THAI], _fields(), pid)
    assert staged and _confirm(people.boss, action_id, THAI).status_code == 200
    assert _row(pid).websites == [THAI]


def test_chat_create_with_two_selected_websites(people):
    pid = _pid("k2")
    _result, staged, action_id = _propose(people.dual, [TAJ, VAR], _fields(), pid)
    assert staged and _confirm(people.dual, action_id).status_code == 200
    assert sorted(_row(pid).websites) == sorted([TAJ, VAR])


def test_chat_create_for_an_explicit_website_outside_the_account_is_refused(people):
    pid = _pid("k3")
    result, staged, action_id = _propose(people.taj, [TAJ], _fields(websites=[THAI]), pid)
    if staged:  # if it was staged, the confirm must still refuse it
        res = _confirm(people.taj, action_id)
        assert res.status_code >= 400, res.text
    assert _row(pid) is None or THAI not in _row(pid).websites


def test_chat_create_for_a_website_that_is_not_selected_is_refused(people):
    pid = _pid("k4")
    result, staged, action_id = _propose(people.dual, [TAJ], _fields(websites=[VAR]), pid)
    if staged:
        assert _confirm(people.dual, action_id).status_code >= 400
    assert _row(pid) is None or _row(pid).websites == [TAJ]


def test_chat_create_with_an_existing_name_is_not_staged_as_a_new_listing(people):
    name = f"Chat Duplicate Villa {RUN} k5"
    assert _create(people.taj, TAJ, **_body(_pid("k5"), name=name)).status_code == 201
    pid = _pid("k5b")
    result, staged, action_id = _propose(people.taj, [TAJ], _fields(name=name), pid)
    if staged:
        _confirm(people.taj, action_id)
    assert _count(name) == 1, "a second listing with the same name was created through chat"


def test_chat_create_with_an_existing_id_updates_instead_of_duplicating(people):
    pid = _pid("k6")
    assert _create(people.taj, TAJ, **_body(pid)).status_code == 201
    _r, staged, action_id = _propose(people.taj, [TAJ], {"price_numeric_min": 9_000_000}, pid)
    assert staged and _confirm(people.taj, action_id).status_code == 200
    assert _row(pid).price == 9_000_000 and _row(pid).websites == [TAJ]


def test_chat_update_of_a_listing_on_another_website_is_forbidden(people):
    pid = _pid("k7")
    assert _create(people.dual, VAR, **_body(pid)).status_code == 201
    result, staged, _ = _propose(people.dual, [TAJ], {"price_numeric_min": 1_000_000}, pid, mode="update")
    assert not staged and result["status"] == "FORBIDDEN"
    result, staged, _ = _propose(people.taj, [TAJ], {"price_numeric_min": 1_000_000}, pid, mode="update")
    assert not staged and result["status"] == "FORBIDDEN"
    assert _row(pid).price == 2_500_000


def test_chat_confirm_cannot_be_used_to_write_to_a_website_the_account_lost(people):
    """Proposed while the admin had varsovia; the account is then limited to tajlandia; the confirm must not write."""
    pid = _pid("k8")
    _r, staged, action_id = _propose(people.dual, [VAR], _fields(), pid)
    assert staged
    from app.services.firestore_auth_service import update_admin_in_firestore
    update_admin_in_firestore(email=people.dual.email, allowed_websites=[TAJ])
    try:
        res = client.post(f"/api/v1/admin/actions/{action_id}/confirm",
                          headers={"Authorization": f"Bearer {people.dual.token}"},
                          json={"confirmation_token": fs_users.pending_token(action_id)})
        assert res.status_code >= 400 or (_row(pid) is None or VAR not in _row(pid).websites)
    finally:
        update_admin_in_firestore(email=people.dual.email, allowed_websites=[TAJ, VAR])


def test_chat_confirm_twice_writes_once(people):
    pid = _pid("k9")
    _r, staged, action_id = _propose(people.taj, [TAJ], _fields(), pid)
    assert staged and _confirm(people.taj, action_id).status_code == 200
    again = _confirm(people.taj, action_id)
    assert again.status_code >= 400 or again.json().get("idempotent") is True   # the second yes does not write again
    assert _count(_row(pid).name) == 1


def test_chat_proposal_can_only_be_confirmed_by_who_proposed_it(people):
    pid = _pid("k10")
    _r, staged, action_id = _propose(people.dual, [TAJ], _fields(), pid)
    assert staged
    assert _confirm(people.taj, action_id).status_code == 403
    assert _row(pid) is None


def test_chat_worker_cannot_propose_or_confirm_changes(people):
    pid = _pid("k11")
    before = _pending_count(people.worker)
    result, staged, _ = _propose(people.worker, [TAJ], _fields(), pid)
    assert not staged and result["status"] == "FORBIDDEN"
    assert _pending_count(people.worker) == before and _row(pid) is None


def test_chat_rejecting_writes_nothing(people):
    pid = _pid("k12")
    _r, staged, action_id = _propose(people.taj, [TAJ], _fields(), pid)
    assert staged
    res = client.post(f"/api/v1/admin/actions/{action_id}/reject", headers=_h(people.taj, TAJ), json={})
    assert res.status_code == 200 and _row(pid) is None
    assert _confirm(people.taj, action_id).status_code >= 400
    assert _row(pid) is None


def test_chat_incomplete_create_is_not_staged(people):
    pid = _pid("k13")
    result, staged, _ = _propose(people.taj, [TAJ], {"property_name": "Only A Name"}, pid)
    assert not staged and result["status"] == "VALIDATION_FAILED"
    assert _row(pid) is None
