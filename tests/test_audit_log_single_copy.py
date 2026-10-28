"""
Activity logs and data history send each piece of data once.

A staged listing change is stored with the same data in several places (`fields` and `fields_to_update`, the preview table at
the top and again under `change_diff`, the payload again inside the result) and an action written to both PostgreSQL and
Firestore comes back twice. The API now keeps each piece where it first appears; short values such as "500 THB" are never removed.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.db.models import AuditLog, User
from app.db.session import SessionLocal
from app.main import app
from app.services.audit_service import dedupe_log_sections

client = TestClient(app)
RUN = uuid.uuid4().hex[:6]
TABLE = ("| Field | Current | New |\n|---|---|---|\n| price_numeric_min | 3,000,000 THB | 4,000,000 THB |\n"
         "| status | Available | Sold Out |")


def _staged_payload():
    fields = {"price_numeric_min": 4_000_000, "status": "Sold Out", "price_note": "500 THB"}
    return {
        "property_id": "prop-1", "mode": "update",
        "fields": dict(fields), "fields_to_update": dict(fields),
        "diff_markdown": TABLE, "diff_preview": TABLE,
        "change_diff": {"diff_markdown": TABLE, "canonical_fields": dict(fields), "rows": [{"field": "status", "new": "Sold Out"}],
                        "risk_warnings": []},
        "risk_warnings": [],
    }


def _count(text, needle):
    return text.count(needle)


# ---- the rule itself -----------------------------------------------------------------------------------------------

def test_each_piece_appears_once_across_arguments_payload_and_result():
    payload = _staged_payload()
    result = str({"property_id": "prop-1", "updated_fields": dict(payload["fields"]), "fields_to_update": dict(payload["fields"]),
                  "diff_markdown": TABLE})
    arguments, proposed, result_text = dedupe_log_sections({"property_id": "prop-1", "fields": dict(payload["fields"])}, payload, result, "SUCCESS")
    everything = json.dumps([arguments, proposed, result_text])
    assert _count(everything, "3,000,000 THB") == 1                     # the preview table, once
    assert _count(everything, '"price_numeric_min": 4000000') == 1      # the fields object, once
    assert "diff_preview" not in proposed and "fields" not in proposed or "fields_to_update" in proposed
    assert proposed["fields_to_update"] if "fields_to_update" in proposed else arguments["fields"]


def test_the_panels_key_fields_to_update_is_kept_and_fields_is_the_one_dropped():
    _a, proposed, _r = dedupe_log_sections({}, _staged_payload(), "done", "SUCCESS")
    assert "fields_to_update" in proposed and "fields" not in proposed


def test_the_preview_table_is_not_repeated_under_change_diff():
    _a, proposed, _r = dedupe_log_sections({}, _staged_payload(), "done", "SUCCESS")
    assert _count(json.dumps(proposed), "3,000,000 THB") == 1
    assert "diff_preview" not in proposed
    assert "diff_markdown" not in proposed.get("change_diff", {})


def test_short_values_are_never_removed_even_when_they_repeat():
    payload = {"fields_to_update": {"price_note": "500 THB"}, "change_diff": {"rows": [{"field": "price_note", "new": "500 THB"}]}}
    _a, proposed, _r = dedupe_log_sections({}, payload, "ok", "SUCCESS")
    assert proposed["fields_to_update"]["price_note"] == "500 THB"
    assert proposed["change_diff"]["rows"][0]["new"] == "500 THB"


def test_empty_sections_are_dropped_and_the_result_stays_a_non_empty_text():
    first, second, result = dedupe_log_sections({}, {"risk_warnings": [], "note": None, "fields_to_update": {"a": 1}}, "", "SUCCESS")
    assert second == {"fields_to_update": {"a": 1}} and first == {}
    payload_copy = {"fields_to_update": {"a": 1}}
    _f, _s, again = dedupe_log_sections({}, payload_copy, json.dumps(payload_copy), "SUCCESS")
    assert again == "SUCCESS"                                            # the result only repeated the payload


def test_plain_text_results_and_unrelated_data_are_untouched():
    first, second, result = dedupe_log_sections({"q": "x"}, {"k": "v"}, "Executed update on prop-1", "SUCCESS")
    assert (first, second, result) == ({"q": "x"}, {"k": "v"}, "Executed update on prop-1")


# ---- through the endpoints -----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_headers():
    email = f"audit_{RUN}@tajlandia.com"
    with SessionLocal() as db:
        db.add(User(email=email, name="a", role="admin", password_hash=hash_password("Audit#12345"), is_active=True,
                    allowed_websites=["tajlandia.com"]))
        db.commit()
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "Audit#12345"}).json()["access_token"]
    yield {"Authorization": f"Bearer {token}"}
    with SessionLocal() as db:
        db.query(AuditLog).filter(AuditLog.staff_user_id.like(f"audit_{RUN}%")).delete(synchronize_session=False)
        db.query(User).filter(User.email == email).delete(synchronize_session=False)
        db.commit()


def _row(tool="update_listing", suffix="a"):
    payload = _staged_payload()
    result = str({"property_id": "prop-1", "updated_fields": dict(payload["fields"]), "diff_markdown": TABLE})
    with SessionLocal() as db:
        row = AuditLog(staff_user_id=f"audit_{RUN}_{suffix}@tajlandia.com", tool_name=tool,
                       tool_arguments={"property_id": "prop-1", "fields": dict(payload["fields"])},
                       proposed_payload=payload, confirmed_by_staff=True, execution_result=result, status="SUCCESS",
                       pending_action_id=f"pa-{RUN}-{suffix}", created_at=datetime(2026, 10, 15, 11, 2, 33, tzinfo=timezone.utc))
        db.add(row)
        db.commit()
        return str(row.id)


def test_activity_logs_endpoint_sends_each_piece_once(admin_headers):
    _row(suffix="e1")
    res = client.get("/api/v1/admin/activity-logs", headers=admin_headers, params={"admin_email": f"audit_{RUN}_e1", "source": "postgres"})
    assert res.status_code == 200, res.text
    log = res.json()["logs"][0]
    text = json.dumps([log["arguments"], log["proposed_payload"], log["execution_result"]])
    assert text.count("3,000,000 THB") == 1
    assert text.count('"price_numeric_min": 4000000') == 1
    assert log["execution_result"] and log["status"] == "SUCCESS" and log["confirmed_by_staff"] is True
    assert {"id", "admin_email", "action", "created_at", "source"} <= set(log)


def test_data_history_endpoint_sends_each_piece_once(admin_headers):
    _row(suffix="e2")
    res = client.get("/api/v1/admin/data-history", headers=admin_headers, params={"updated_by": f"audit_{RUN}_e2"})
    assert res.status_code == 200, res.text
    item = res.json()["history"][0]
    text = json.dumps([item["changes_summary"], item["full_payload"], item["execution_result"]])
    assert text.count("3,000,000 THB") == 1
    assert item["execution_result"] and item["target_id"] == "prop-1"


def test_the_same_activity_from_both_stores_is_listed_once(admin_headers, monkeypatch):
    _row(suffix="e3")
    payload = _staged_payload()
    twin = {"id": f"fs-{RUN}", "adminEmail": f"audit_{RUN}_e3@tajlandia.com", "action": "update_listing",
            "targetType": "action", "targetId": f"pa-{RUN}-e3", "createdAt": "2026-10-15T11:02:33.400000+00:00",
            "details": {"status": "SUCCESS", "confirmed": True, "arguments": {"property_id": "prop-1"}, "payload": payload}}
    monkeypatch.setattr("app.services.firestore_service.get_audit_logs_from_firestore", lambda **kw: [twin])
    res = client.get("/api/v1/admin/activity-logs", headers=admin_headers, params={"admin_email": f"audit_{RUN}_e3", "source": "all"})
    assert res.status_code == 200, res.text
    mine = [r for r in res.json()["logs"] if f"audit_{RUN}_e3" in r["admin_email"]]
    assert len(mine) == 1 and mine[0]["source"] == "postgres"


def test_a_firestore_only_row_does_not_repeat_its_payload_in_the_result(admin_headers, monkeypatch):
    payload = _staged_payload()
    only = {"id": f"fs2-{RUN}", "adminEmail": f"audit_{RUN}_e4@tajlandia.com", "action": "update_listing", "createdAt": "2026-10-16T09:00:00+00:00",
            "details": {"status": "SUCCESS", "confirmed": True, "arguments": {"property_id": "prop-1"}, "payload": payload}}
    monkeypatch.setattr("app.services.firestore_service.get_audit_logs_from_firestore", lambda **kw: [only])
    res = client.get("/api/v1/admin/activity-logs", headers=admin_headers, params={"admin_email": f"audit_{RUN}_e4", "source": "firestore"})
    log = next(r for r in res.json()["logs"] if r["id"] == f"fs2-{RUN}")
    text = json.dumps([log["arguments"], log["proposed_payload"], log["execution_result"]])
    assert text.count("3,000,000 THB") == 1
    assert log["execution_result"]


def test_two_different_activities_at_the_same_second_are_both_kept(admin_headers):
    _row(suffix="e5")
    with SessionLocal() as db:
        db.add(AuditLog(staff_user_id=f"audit_{RUN}_e5@tajlandia.com", tool_name="delete_listing", tool_arguments={"property_id": "prop-2"},
                        proposed_payload={}, confirmed_by_staff=True, execution_result="archived prop-2", status="SUCCESS",
                        pending_action_id=f"pa-{RUN}-e5b", created_at=datetime(2026, 10, 15, 11, 2, 33, tzinfo=timezone.utc)))
        db.commit()
    res = client.get("/api/v1/admin/activity-logs", headers=admin_headers, params={"admin_email": f"audit_{RUN}_e5", "source": "postgres"})
    assert sorted(r["action"] for r in res.json()["logs"]) == ["delete_listing", "update_listing"]
