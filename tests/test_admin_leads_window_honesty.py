"""
TASKS 1–4: Leads window defaults, report all-leads, claim-verifier fact stability,
lead-saving visibility.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import get_admin_config_status, log_config_warnings
from app.core import config as config_mod
from app.services.admin_reply_helpers import (
    parse_admin_leads_period,
    format_leads_date_window,
    format_leads_zero_with_offer,
    format_leads_lead_saving_note,
)
from app.services.admin_claim_verifier import (
    detect_unfair_lead_retraction,
    verify_admin_claims,
    build_deterministic_reply_from_tools,
)
from app.services.report_service import (
    is_possible_test_lead,
    serialize_lead_row,
    generate_report,
    InMemoryStorageProvider,
    LEADS_REPORT_CAP,
)


# ---------------------------------------------------------------------------
# TASK 1 — period parsing / messaging
# ---------------------------------------------------------------------------

def test_no_period_is_all_time():
    spec = parse_admin_leads_period("show leads")
    assert spec.days == 0
    assert spec.explicit is False
    assert spec.label == "all_time"

    spec2 = parse_admin_leads_period("generate a leads report")
    assert spec2.days == 0


def test_this_week_is_explicit_seven_days():
    spec = parse_admin_leads_period("show leads from this week")
    assert spec.days == 7
    assert spec.explicit is True
    label = format_leads_date_window(
        days=7,
        window_start=datetime(2026, 10, 12),
        window_end=datetime(2026, 10, 19),
        language="en",
    )
    assert "last 7 days" in label
    assert "2026-10-12" in label and "2026-10-19" in label


def test_zero_result_message_offers_wider_window():
    msg = format_leads_zero_with_offer(
        window_label="last 7 days (2026-10-12 to 2026-10-19)",
        most_recent_date="2026-10-11",
        most_recent_days_ago=8,
        wider_days=30,
        wider_count=50,
        language="en",
    )
    assert "0 leads" in msg.lower() or "No leads" in msg
    assert "2026-10-11" in msg
    assert "8 days ago" in msg
    assert "30" in msg and "50" in msg
    assert "Want the last 30 days" in msg or "30 days" in msg


def test_get_buyer_leads_defaults_all_time_not_seven(monkeypatch):
    """Tool handler: omitted days → all-time (days=0), not hidden 7."""
    from app.services import admin_agent as aa

    captured = {}

    class FakeLead:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    leads_rows = [
        FakeLead(
            id="1",
            contact_name="Alexander Smith",
            contact_email="alex@corp.com",
            contact_phone="+1",
            preferred_location="Phuket",
            budget_max=None,
            budget_min=None,
            lead_score=80,
            status="new",
            buying_signals=[],
            created_at=datetime(2026, 10, 11, tzinfo=timezone.utc),
        )
    ]

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows
            self._filters = []

        def filter(self, *a, **k):
            self._filters.append((a, k))
            return self

        def order_by(self, *a):
            return self

        def limit(self, n):
            self._limit = n
            return self

        def count(self):
            return len(self._rows)

        def all(self):
            return list(self._rows)

        def first(self):
            return self._rows[0] if self._rows else None

    class FakeDB:
        def query(self, model):
            return FakeQuery(leads_rows)

    monkeypatch.setattr(aa.settings, "ENABLE_LEAD_SAVING", False)
    staff = MagicMock()
    staff.email = "admin@test.com"
    staff.selected_websites = None
    monkeypatch.setattr(aa, "record_audit_log", lambda **k: None)

    res, _, _ = aa._execute_admin_tool(
        FakeDB(), "get_buyer_leads", {"limit": 10}, staff, "c1"
    )
    assert res["days"] == 0 or res.get("all_time") is True
    assert res["total"] >= 1
    assert "all leads" in (res.get("summary") or res.get("window_label") or "").lower() or res.get("all_time")


# ---------------------------------------------------------------------------
# TASK 2 — leads report
# ---------------------------------------------------------------------------

def test_is_possible_test_lead_flags_example_com():
    assert is_possible_test_lead(email="qa@example.com", name="QA Tester") is True
    assert is_possible_test_lead(email="buyer@corp.com", name="Alexander Smith") is False


def test_leads_report_no_filters_includes_all(monkeypatch, tmp_path):
    from app.services import report_service as rs

    monkeypatch.setattr(rs.settings, "REPORT_STORAGE_DIR", str(tmp_path / "reports"))
    store = InMemoryStorageProvider()

    class L:
        def __init__(self, i, name, email, created):
            self.id = str(i)
            self.contact_name = name
            self.contact_email = email
            self.contact_phone = ""
            self.preferred_location = "Phuket"
            self.budget_max = 1_000_000
            self.budget_min = None
            self.lead_score = 50
            self.status = "new"
            self.buying_signals = []
            self.created_at = created

    rows = [
        L(1, "A One", "a@corp.com", datetime(2026, 9, 1, tzinfo=timezone.utc)),
        L(2, "B Two", "test@example.com", datetime(2026, 10, 11, tzinfo=timezone.utc)),
        L(3, "C Three", "c@corp.com", datetime(2026, 10, 5, tzinfo=timezone.utc)),
    ]

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a):
            return self

        def limit(self, n):
            self._n = n
            return self

        def count(self):
            return len(rows)

        def all(self):
            return rows[: getattr(self, "_n", len(rows))]

        def first(self):
            return rows[0]

    class FakeDB:
        def query(self, model):
            return FakeQuery()

    out = generate_report(
        FakeDB(),
        "pdf",
        report_type="leads",
        days=0,
        storage=store,
    )
    assert out.get("storage_ok") is True
    assert out.get("total_leads") == 3
    assert "all stored" in (out.get("period_label") or "").lower() or out["total_leads"] == 3
    # test-entry flag on example.com lead
    row = serialize_lead_row(rows[1])
    assert row["possible_test_entry"] is True
    assert row.get("test_entry_tag") == "possible test entry"


def test_leads_report_empty_period_offers_wider(monkeypatch, tmp_path):
    from app.services import report_service as rs

    monkeypatch.setattr(rs.settings, "REPORT_STORAGE_DIR", str(tmp_path / "reports"))

    class FakeQuery:
        def __init__(self, empty=True):
            self.empty = empty

        def filter(self, *a, **k):
            return FakeQuery(empty=True)

        def order_by(self, *a):
            return self

        def limit(self, n):
            return self

        def count(self):
            return 0 if self.empty else 12

        def all(self):
            return []

        def first(self):
            return SimpleNamespace(
                created_at=datetime(2026, 10, 11, tzinfo=timezone.utc)
            )

    class FakeDB:
        def query(self, model):
            # first calls for period query return empty; wider uses same — simplify
            return FakeQuery(empty=True)

    # Patch wider count path: after empty, generate_report queries again for wider
    calls = {"n": 0}

    class TrackingDB:
        def query(self, model):
            calls["n"] += 1
            q = FakeQuery(empty=True)
            # When counting wider window (later calls), return some count
            if calls["n"] > 2:
                q = FakeQuery(empty=False)
                q.all = lambda: []
            return q

    out = generate_report(
        TrackingDB(),
        "pdf",
        report_type="leads",
        days=7,
        storage=InMemoryStorageProvider(),
    )
    assert out.get("empty_period") is True
    assert out.get("file_url") is None
    assert "Want the last" in (out.get("message") or "") or "most recent" in (out.get("message") or "").lower()
    assert "2026-10-11" in (out.get("message") or "") or out.get("most_recent_lead_date") == "2026-10-11"


# ---------------------------------------------------------------------------
# TASK 3 — claim verifier fact stability
# ---------------------------------------------------------------------------

def test_narrower_zero_does_not_retract_earlier_lead():
    prior = {"alexander smith"}
    reply = (
        "There are 0 leads in the last 7 days. "
        "Alexander Smith was probably demo data / not verified."
    )
    tool_results = [{
        "total": 0,
        "leads": [],
        "days": 7,
        "window_label": "last 7 days (2026-10-12 to 2026-10-19)",
    }]
    viol = detect_unfair_lead_retraction(
        reply, tool_results, prior_verified_leads=prior
    )
    assert any(v.startswith("unfair_retract") for v in viol)

    result = verify_admin_claims(
        reply, tool_results, prior_verified_leads=prior
    )
    assert result.ok is False
    assert any("unfair_retract" in h for h in result.hard_mismatches)


def test_example_com_must_not_be_called_demo():
    reply = "This lead uses example.com so it is demo / fake data."
    viol = detect_unfair_lead_retraction(reply, [{"leads": []}], prior_verified_leads=set())
    assert "demo_label_from_example_email" in viol

    ok_reply = (
        "Email domain looks like a placeholder — verify before contacting "
        "(example.com)."
    )
    viol2 = detect_unfair_lead_retraction(ok_reply, [], prior_verified_leads=set())
    assert "demo_label_from_example_email" not in viol2


# ---------------------------------------------------------------------------
# TASK 4 — lead saving visibility
# ---------------------------------------------------------------------------

def test_lead_saving_note_when_off():
    note = format_leads_lead_saving_note(
        lead_saving_on=False,
        last_lead_date="2026-10-11",
        language="en",
    )
    assert note is not None
    assert "lead saving is OFF" in note
    assert "2026-10-11" in note
    assert format_leads_lead_saving_note(lead_saving_on=True, last_lead_date="x") is None


def test_config_status_and_startup_cover_lead_saving(caplog, monkeypatch):
    status = get_admin_config_status()
    assert "lead_saving" in status
    assert isinstance(status["lead_saving"], bool)

    monkeypatch.setattr(config_mod.settings, "ENABLE_LEAD_SAVING", False)
    monkeypatch.setattr(config_mod.settings, "ENABLE_AUDIT_LOGGING", True)
    monkeypatch.setattr(config_mod.settings, "ADMIN_FIRESTORE_PROJECT_ID", "x")
    monkeypatch.setattr(config_mod.settings, "INTERNAL_API_SECRET", "a" * 32)
    monkeypatch.setattr(config_mod.settings, "SECRET_KEY", "b" * 32)
    monkeypatch.setattr(config_mod.settings, "GOOGLE_REDIRECT_URI", "")
    import logging
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        log_config_warnings()
    assert any("Lead saving is OFF" in r.message for r in caplog.records)


def test_deterministic_reply_includes_window_and_saving_note():
    text = build_deterministic_reply_from_tools(
        [{
            "name": "get_buyer_leads",
            "result": {
                "total": 2,
                "leads": [
                    {
                        "contact_name": "Alexander Smith",
                        "lead_score": 80,
                        "preferred_location": "Phuket",
                        "budget": "Flexible",
                        "contact_email": "a@corp.com",
                    }
                ],
                "summary": "Showing all leads (2 total), newest first",
                "all_time": True,
                "days": 0,
                "lead_saving_note": "Note: lead saving is OFF, so new inquiries after 2026-10-11 are not being stored.",
            },
        }],
        language="en",
    )
    assert "Showing all leads" in text or "2" in text
    assert "lead saving is OFF" in text
