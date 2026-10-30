"""
Activity logs and data history send each piece of data once.

A staged listing change is stored with the same data in several places (`fields` and `fields_to_update`, the preview table at
the top and again under `change_diff`, the payload again inside the result). The API keeps each piece where it first appears;
short values such as "500 THB" are never removed.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import audit_repo
from app.services.audit_service import dedupe_log_sections
from tests import fs_users

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

@pytest.fixture
def admin_headers():
    email = f"audit_{RUN}@tajlandia.com"
    return fs_users.make_user(client, "admin", email=email, password="Audit#12345", allowed_websites=["tajlandia.com"])["headers"]


def _row(tool="update_listing", suffix="a"):
    payload = _staged_payload()
    result = str({"property_id": "prop-1", "updated_fields": dict(payload["fields"]), "diff_markdown": TABLE})
    row = audit_repo.create(
        None, staff_user_id=f"audit_{RUN}_{suffix}@tajlandia.com", tool_name=tool,
        tool_arguments={"property_id": "prop-1", "fields": dict(payload["fields"])},
        proposed_payload=payload, confirmed_by_staff=True, execution_result=result, status="SUCCESS",
        pending_action_id=f"pa-{RUN}-{suffix}",
    )
    return str(row.id)


def test_activity_logs_endpoint_sends_each_piece_once(admin_headers):
    _row(suffix="e1")
    res = client.get("/api/v1/admin/activity-logs", headers=admin_headers, params={"admin_email": f"audit_{RUN}_e1", "source": "firestore"})
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


def test_two_different_activities_close_together_are_both_kept(admin_headers):
    _row(suffix="e5")
    audit_repo.create(
        None, staff_user_id=f"audit_{RUN}_e5@tajlandia.com", tool_name="delete_listing",
        tool_arguments={"property_id": "prop-2"}, proposed_payload={}, confirmed_by_staff=True,
        execution_result="archived prop-2", status="SUCCESS", pending_action_id=f"pa-{RUN}-e5b",
    )
    res = client.get("/api/v1/admin/activity-logs", headers=admin_headers, params={"admin_email": f"audit_{RUN}_e5", "source": "firestore"})
    assert sorted(r["action"] for r in res.json()["logs"]) == ["delete_listing", "update_listing"]
