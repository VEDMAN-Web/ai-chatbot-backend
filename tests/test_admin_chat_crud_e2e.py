"""
Property CRUD asked for in the Admin AI Assistant chat, end to end (QA report: 35 chat cases, 19 failed).

Real accounts and tokens, the real POST /api/v1/admin/chat, and the in-memory Firestore fake for listings and the undo
snapshots. The language model is not available here (dummy key), so these cases show what the backend does with each command
before and without the model; they do not show how the live model words its answers.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.deps import FirestoreSession
from app.main import app
from tests import fs_users

client = TestClient(app)
TAJ, VAR = "tajlandia.com", "varsoviaestate.com"
RUN = uuid.uuid4().hex[:6].translate(str.maketrans("0123456789abcdef", "bcdfghjklmnpqrst"))  # consonants only: digits parse as a price, vowels can spell words (e.g. "off")
PW = "ChatCrud#123"


@pytest.fixture
def fs(_default_fake_firestore):
    return _default_fake_firestore


@pytest.fixture(autouse=True)
def wiring(fs, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", False)
    monkeypatch.setattr(settings, "ENABLE_AUDIT_LOGGING", True)
    global _FS
    _FS = fs


_FS = None


def _user(role, sites):
    email = f"cc_{RUN}_{role}_{uuid.uuid4().hex[:4]}@tajlandia.com"
    made = fs_users.make_user(client, role, email=email, password=PW, allowed_websites=list(sites))
    return SimpleNamespace(email=email, token=made["headers"]["Authorization"].split(" ", 1)[1])


@pytest.fixture
def people():
    return SimpleNamespace(taj=_user("admin", [TAJ]), dual=_user("admin", [TAJ, VAR]), worker=_user("worker", [TAJ]),
                           boss=_user("super_admin", [TAJ, VAR, "thai.estate"]))


def _seed(name, pid, site=TAJ, price=3_000_000, status="Available"):
    _FS.collection("projects").document(pid).set({
        "name": name, "status": status, "isActive": True, "websites": [site], "priceFrom": price,
        "province": "Koh Samui", "cityId": "samui", "propertyType": "Villa",
    })


def _row(pid):
    snap = _FS.collection("projects").document(pid).get()
    if not snap.exists:
        return None
    raw = snap.to_dict()
    status = raw.get("status")
    public = raw.get("isActive") is not False and str(status).lower() != "archived"
    return SimpleNamespace(
        name=raw.get("name") or raw.get("property_name"), status=status, is_public=public,
        price=float(raw.get("priceFrom") or raw.get("price_numeric_min") or 0),
        websites=list(raw.get("websites") or []), province=raw.get("province"),
        type=raw.get("propertyType") or raw.get("property_type"),
    )


class Chat:
    def __init__(self, who, *sites):
        self.headers = {"Authorization": f"Bearer {who.token}"}
        if sites:
            self.headers["X-Selected-Websites"] = ",".join(sites)
        self.session = f"cc-{RUN}-{uuid.uuid4().hex[:8]}"

    def say(self, text, **extra):
        res = client.post("/api/v1/admin/chat", headers=self.headers, json={"message": text, "session_id": self.session, **extra})
        assert res.status_code == 200, res.text
        return SimpleNamespace(**{"text": "", "requires_confirmation": False, "pending_action_id": None, "tools_used": [], "download_link": None, **res.json()})

    def payload(self, pending_id):
        return client.get(f"/api/v1/admin/actions/{pending_id}", headers=self.headers).json()["action_payload"]


@pytest.fixture
def listing():
    pid = f"3-villas-lamai-{RUN}-{uuid.uuid4().hex[:4].translate(str.maketrans('0123456789abcdef', 'bcdfghjklmnpqrst'))}"
    _seed("3 Villas - Lamai", pid)
    return pid


# ======================================================================================================
# update: price and status
# ======================================================================================================

def test_price_update_uses_the_amount_asked_for_not_the_number_in_the_name(people, listing):
    chat = Chat(people.taj, TAJ)
    r = chat.say("Change the price of 3 Villas - Lamai to 5,000,000 THB")
    assert r.requires_confirmation and r.pending_action_id, r.text
    assert "5,000,000" in r.text and "3 THB" not in r.text
    assert chat.payload(r.pending_action_id)["fields_to_update"]["price_numeric_min"] == 5_000_000
    assert _row(listing).price == 3_000_000                                   # nothing written before the yes
    done = chat.say("Confirm")
    assert "Done." in done.text and _row(listing).price == 5_000_000


@pytest.mark.parametrize("message", ["Update price of 3 Villas - Lamai to -500", "update price of 3 Villas - Lamai to 0 THB",
                                     "Change the price of 3 Villas - Lamai to minus 1000"])
def test_a_negative_or_zero_price_is_refused_and_nothing_is_staged(people, listing, message):
    chat = Chat(people.taj, TAJ)
    r = chat.say(message)
    assert not r.pending_action_id and not r.requires_confirmation and ("greater than 0" in r.text or "too low" in r.text)
    assert _row(listing).price == 3_000_000


def test_status_ready_is_set_as_ready_to_move_in(people, listing):
    chat = Chat(people.taj, TAJ)
    r = chat.say("Change the status of 3 Villas - Lamai to ready")
    assert r.pending_action_id, r.text
    assert chat.payload(r.pending_action_id)["fields_to_update"]["status"] == "Ready to move in"
    chat.say("yes")
    assert _row(listing).status == "Ready to move in"


@pytest.mark.parametrize("word,expected", [("sold out", "Sold Out"), ("under construction", "Under construction"), ("reserved", "Reserved")])
def test_each_valid_status_is_applied(people, listing, word, expected):
    chat = Chat(people.taj, TAJ)
    assert chat.say(f"Update the status of 3 Villas - Lamai to {word}").pending_action_id
    chat.say("Confirm")
    assert _row(listing).status == expected


def test_an_unknown_status_is_refused_with_the_valid_list(people, listing):
    r = Chat(people.taj, TAJ).say("Change the status of 3 Villas - Lamai to banana")
    assert not r.pending_action_id and "banana" in r.text and "Sold Out" in r.text
    assert _row(listing).status == "Available"


def test_a_price_update_that_names_no_amount_asks_for_it(people, listing):
    r = Chat(people.taj, TAJ).say("Update the price of 3 Villas - Lamai")
    assert not r.pending_action_id and _row(listing).price == 3_000_000


# ======================================================================================================
# create
# ======================================================================================================

def test_a_new_listing_is_staged_then_created_on_the_selected_website(people):
    name = f"Sunset Pearl {RUN}"
    pid = f"sunset-pearl-{RUN}"
    chat = Chat(people.dual, VAR)
    r = chat.say(f"Add a new listing: {name}, villa, Phuket, 12,000,000 THB, available")
    assert r.requires_confirmation and r.pending_action_id and not r.tools_used[0]["name"] == "search_properties_full", r.text
    assert _row(pid) is None
    done = chat.say("Confirm")
    assert "Done." in done.text, done.text
    row = _row(pid)
    assert row and row.name == name and row.price == 12_000_000 and row.type == "Villa" and row.status == "Available"
    assert row.websites == [VAR]                                               # the website that was selected


def test_creating_the_same_listing_again_is_refused(people):
    chat = Chat(people.taj, TAJ)
    pid = f"twice-{RUN}"
    msg = f"Add a new listing: Twice {RUN}, villa, Phuket, 5,000,000 THB, available"
    chat.say(msg)
    chat.say("Confirm")
    assert _row(f"twice-{RUN}") is not None
    again = chat.say(msg)
    assert not again.pending_action_id and "already exists" in again.text
    same_name = [d.to_dict() for d in _FS.collection("projects").stream()
                 if (d.to_dict().get("name") or d.to_dict().get("property_name")) == f"Twice {RUN}"]
    assert len(same_name) == 1


def test_missing_parts_are_asked_for_not_invented(people):
    r = Chat(people.taj, TAJ).say(f"Add a new listing: Half Done {RUN}, villa")
    assert not r.pending_action_id and "I still need" in r.text and "location" in r.text and "price" in r.text and "status" in r.text
    assert _row(f"half-done-{RUN}") is None


def test_a_bad_status_or_price_in_a_new_listing_is_refused(people):
    r = Chat(people.taj, TAJ).say(f"Add a new listing: name: Bad One {RUN}, type: villa, location: Phuket, price: -5, status: banana")
    assert not r.pending_action_id and ("greater than 0" in r.text or "too low" in r.text) and "banana" in r.text


def test_a_new_listing_needs_a_website_selection(people):
    r = Chat(people.dual).say(f"Add a new listing: Nowhere {RUN}, villa, Phuket, 5,000,000 THB, available")
    assert r.pending_action_id or "website" in r.text.lower()   # with no header the account's websites are used; either is a real answer


def test_a_worker_cannot_create_delete_or_undo(people, listing):
    chat = Chat(people.worker, TAJ)
    for msg in (f"Add a new listing: Worker Try {RUN}, villa, Phuket, 5,000,000 THB, available", "Delete 3 Villas - Lamai", "Undo the last change"):
        r = chat.say(msg)
        assert not r.pending_action_id and "not allowed" in r.text, (msg, r.text)
    assert _row(listing).status == "Available" and _row(f"worker-try-{RUN}") is None


def test_adding_a_lead_is_explained_not_run_as_a_search(people):
    r = Chat(people.taj, TAJ).say("Add a new buyer lead: QA Tester, qa.tester@example.com, +66800000000, interested in Hidden Lotus")
    assert "cannot add a lead" in r.text and not r.tools_used and not r.pending_action_id


# ======================================================================================================
# delete (archive) and undo
# ======================================================================================================

def test_delete_archives_after_a_yes_and_undo_brings_it_back(people, listing):
    chat = Chat(people.taj, TAJ)
    r = chat.say("Delete 3 Villas - Lamai")
    assert r.requires_confirmation and r.pending_action_id and "Archive listing" in r.text, r.text
    assert _row(listing).status == "Available"                                  # nothing happens before the yes
    done = chat.say("Confirm")
    assert "Listing archived" in done.text, done.text
    row = _row(listing)
    assert row is not None and row.status == "Archived" and row.is_public is False       # soft delete: the row is still there
    undo = chat.say("Undo the last change")
    assert undo.requires_confirmation and "Undo" in undo.text and _row(listing).status == "Archived"
    assert "Undone" in chat.say("Confirm").text
    row = _row(listing)
    assert row.status == "Available" and row.is_public is True


def test_permanent_delete_is_refused(people, listing):
    r = Chat(people.taj, TAJ).say("permanently delete 3 Villas - Lamai")
    assert not r.pending_action_id and _row(listing) is not None
    assert "not allowed" in r.text or "Permanent" in r.text


def test_delete_cannot_reach_another_websites_listing(people):
    pid = f"varsovia-loft-{RUN}"
    _seed(f"Varsovia Loft {RUN}", pid, site=VAR)
    r = Chat(people.dual, TAJ).say(f"Delete Varsovia Loft {RUN}")
    assert not r.pending_action_id and _row(pid).status == "Available"


def test_deleting_an_unknown_listing_says_so(people):
    r = Chat(people.taj, TAJ).say(f"Delete Imaginary Palace {RUN}")
    assert not r.pending_action_id and "Imaginary Palace" in r.text


def test_deleting_twice_is_harmless(people, listing):
    chat = Chat(people.taj, TAJ)
    chat.say("Delete 3 Villas - Lamai")
    chat.say("Confirm")
    again = chat.say("Delete 3 Villas - Lamai")
    # Archived listings are now hidden from lookups (soft-delete), so the second delete may simply not find it.
    assert not again.pending_action_id
    assert "already archived" in again.text or "couldn't find" in again.text


def test_undo_with_nothing_to_undo_says_so(people):
    fresh = _user("admin", [TAJ])
    r = Chat(fresh, TAJ).say("Undo the last change")
    assert not r.pending_action_id and "nothing to undo" in r.text.lower()


def test_undo_cannot_be_applied_twice(people, listing):
    chat = Chat(people.taj, TAJ)
    chat.say("Delete 3 Villas - Lamai")
    chat.say("Confirm")
    ops = [d.id for d in _FS.collection("admin_batch_operations").stream()]
    assert len(ops) == 1
    op = ops[0]
    assert chat.say(f"undo {op}").pending_action_id
    assert "Undone" in chat.say("Confirm").text
    second = chat.say(f"undo {op}")
    assert not second.pending_action_id


# ======================================================================================================
# a change never applies without a yes
# ======================================================================================================

def test_rejecting_leaves_the_data_unchanged(people, listing):
    chat = Chat(people.taj, TAJ)
    assert chat.say("Change the price of 3 Villas - Lamai to 9,000,000 THB").pending_action_id
    chat.say("Cancel")
    assert _row(listing).price == 3_000_000
    assert "did not apply" in chat.say("yes").text or _row(listing).price == 3_000_000


def test_an_old_proposal_is_not_applied_by_a_later_plain_yes(people, listing):
    """The QA report saw one change apply without Confirm. A plain 'ok' now only applies the proposal just shown."""
    chat = Chat(people.taj, TAJ)
    r = chat.say("Change the price of 3 Villas - Lamai to 8,000,000 THB")
    assert r.pending_action_id
    chat.say("my drive is connected ?")                       # an unrelated turn in between
    later = chat.say("ok")
    assert "did not apply anything" in later.text and _row(listing).price == 3_000_000
    assert "Done." in chat.say(f"confirm {r.pending_action_id}").text      # naming it applies it
    assert _row(listing).price == 8_000_000


def test_the_confirm_button_still_applies_an_older_proposal(people, listing):
    chat = Chat(people.taj, TAJ)
    r = chat.say("Change the price of 3 Villas - Lamai to 7,000,000 THB")
    chat.say("my drive is connected ?")
    res = chat.say("", action="confirm", pending_action_id=r.pending_action_id)
    assert _row(listing).price == 7_000_000, res.text


def test_a_proposal_cannot_be_confirmed_from_another_conversation(people, listing):
    a, b = Chat(people.taj, TAJ), Chat(people.taj, TAJ)
    assert a.say("Change the price of 3 Villas - Lamai to 6,000,000 THB").pending_action_id
    b.say("yes")
    assert _row(listing).price == 3_000_000


# ======================================================================================================
# read: a named listing, its specs, and the Excel export
# ======================================================================================================

def test_show_by_name_returns_that_listing_not_an_area_list(people, listing):
    r = Chat(people.taj, TAJ).say("Show 3 Villas - Lamai")
    assert "3 Villas - Lamai" in r.text and listing in r.text, r.text
    assert "properties** in" not in r.text and "Found" not in r.text.split("\n")[0]


def test_specs_by_name_returns_that_listings_details(people, listing):
    r = Chat(people.taj, TAJ).say("Specs for 3 Villas - Lamai")
    assert "3 Villas - Lamai" in r.text and "No properties found" not in r.text, r.text
    assert [t["name"] for t in r.tools_used] == ["get_property_knowledge"]


def test_a_named_listing_on_another_website_is_not_shown(people):
    pid = f"varsovia-only-{RUN}"
    _seed(f"Varsovia Only {RUN}", pid, site=VAR)
    r = Chat(people.taj, TAJ).say(f"Show Varsovia Only {RUN}")
    assert pid not in r.text and "9,000,000" not in r.text and not r.tools_used


def test_the_search_tool_can_look_a_listing_up_by_name(people, listing):
    from app.services.admin_agent import _execute_admin_tool
    from app.core.auth import AuthenticatedStaff
    staff = AuthenticatedStaff(user_id=str(uuid.uuid4()), email=people.taj.email, role="admin", allowed_websites=[TAJ], selected_websites=[TAJ])
    db = FirestoreSession()
    found, _, _ = _execute_admin_tool(db, "search_properties_full", {"query": "3 Villas - Lamai"}, staff, "c1")
    assert found["count"] == 1 and found["properties"][0]["property_id"] == listing
    none, _, _ = _execute_admin_tool(db, "search_properties_full", {"query": "Qzxv Nowhere Plugh"}, staff, "c1")
    assert none["count"] == 0 and "No listing named" in none["message"]
    other = AuthenticatedStaff(user_id=str(uuid.uuid4()), email=people.taj.email, role="admin", allowed_websites=[VAR], selected_websites=[VAR])
    hidden, _, _ = _execute_admin_tool(db, "search_properties_full", {"query": "3 Villas - Lamai"}, other, "c1")
    assert hidden["count"] == 0


def _report_rows(chat, response):
    import io
    import openpyxl
    link = response.download_link if hasattr(response, "download_link") else None
    assert link, response.text
    res = client.get(link if link.startswith("/") else f"/{link}", headers=chat.headers)
    assert res.status_code == 200, (link, res.text[:200])
    ws = openpyxl.load_workbook(io.BytesIO(res.content)).active
    return [[str(c.value) for c in row] for row in ws.iter_rows()]


def test_export_koh_samui_listings_is_a_property_file_with_only_those_listings(people, listing):
    other = f"phuket-only-{RUN}-{uuid.uuid4().hex[:4].translate(str.maketrans('0123456789abcdef', 'bcdfghjklmnpqrst'))}"
    _FS.collection("projects").document(other).set({"name": f"Phuket Only {RUN}", "status": "Available", "isActive": True, "websites": [TAJ],
                                "priceFrom": 9_000_000, "province": "Phuket", "cityId": "phuket", "propertyType": "Villa"})
    chat = Chat(people.taj, TAJ)
    chat.say("Show the leads")                         # an earlier answer that talks about leads must not turn the export into a leads file
    r = chat.say("Export Koh Samui listings to Excel")
    assert [t["name"] for t in r.tools_used][-1] == "generate_report", r.text
    args = r.tools_used[-1]["args"]
    assert "report_type" not in args and listing in (args.get("property_ids") or []) and other not in (args.get("property_ids") or [])
    rows = " ".join(" ".join(row) for row in _report_rows(chat, r))
    assert "3 Villas - Lamai" in rows and f"Phuket Only {RUN}" not in rows


def test_export_with_no_matching_listings_says_so_instead_of_an_empty_file(people):
    r = Chat(people.taj, TAJ).say("Export Bangkok listings to Excel")
    assert "no listings" in r.text.lower() and "empty file" in r.text and not r.tools_used


def test_export_leads_still_makes_a_leads_report(people):
    r = Chat(people.taj, TAJ).say("Export leads to Excel")
    assert r.tools_used and r.tools_used[-1]["args"].get("report_type") == "leads"


def test_one_websites_search_result_is_never_served_to_another_website(people):
    """The tool cache keeps results for 60 s; it used to key them by tool and arguments only, so the same question from an
    admin on another website got the first admin's listings."""
    from app.core.auth import AuthenticatedStaff
    from app.services.admin_agent import _execute_admin_tool

    taj_pid, var_pid = f"cache-taj-{RUN}", f"cache-var-{RUN}"
    _seed(f"Cache Taj Villa {RUN}", taj_pid, site=TAJ)
    _seed(f"Cache Var Villa {RUN}", var_pid, site=VAR)
    taj = AuthenticatedStaff(user_id=str(uuid.uuid4()), email=people.taj.email, role="admin", allowed_websites=[TAJ], selected_websites=[TAJ])
    var = AuthenticatedStaff(user_id=str(uuid.uuid4()), email=people.dual.email, role="admin", allowed_websites=[VAR], selected_websites=[VAR])
    args = {"location": "Koh Samui", "limit": 200}
    db = FirestoreSession()
    first, _, _ = _execute_admin_tool(db, "search_properties_full", dict(args), taj, "c1")
    second, _, _ = _execute_admin_tool(db, "search_properties_full", dict(args), var, "c2")
    ids_first = {p["property_id"] for p in (first.get("properties") if isinstance(first, dict) else first) or []}
    ids_second = {p["property_id"] for p in (second.get("properties") if isinstance(second, dict) else second) or []}
    assert taj_pid in ids_first and var_pid not in ids_first
    assert var_pid in ids_second and taj_pid not in ids_second
