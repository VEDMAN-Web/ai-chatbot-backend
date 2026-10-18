"""
Admin claim verifier — soft vs hard mismatches, templates, redaction.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.admin_claim_verifier import (
    build_deterministic_reply_from_tools,
    reply_claims_success,
    tool_results_allow_success_claim,
    verify_admin_claims,
    verify_and_correct_admin_reply,
)


def test_wrong_count_corrected_via_template():
    tool_results = [
        {
            "result": {
                "count": 5,
                "properties": [
                    {"property_id": "a", "name": "Alpha"},
                    {"property_id": "b", "name": "Beta"},
                    {"property_id": "c", "name": "Gamma"},
                    {"property_id": "d", "name": "Delta"},
                    {"property_id": "e", "name": "Echo"},
                ],
            }
        }
    ]
    bad = "We found **12 properties** in Phuket matching your filters."
    first = verify_admin_claims(bad, tool_results)
    assert first.ok is False
    assert any(h.startswith("wrong_count") for h in first.hard_mismatches)

    audits = []

    def capture_audit(**kwargs):
        audits.append(kwargs)
        return SimpleNamespace(id="aud_v")

    with patch("app.services.audit_service.record_audit_log", side_effect=capture_audit):
        out = verify_and_correct_admin_reply(
            bad,
            tool_results,
            language="en",
            db=MagicMock(),
            staff_user_id="admin@tajlandia.com",
            conversation_id="c1",
            regenerate_fn=lambda _inst: "Still claiming 12 properties somehow.",
        )

    assert out.intervention == "templated"
    assert out.final_text
    assert "5" in (out.final_text or "")
    assert audits and audits[0].get("tool_name") == "admin_claim_verifier"


def test_wrong_lead_count_is_hard_and_corrected():
    tool_results = [
        {
            "result": {
                "total": 3,
                "leads": [
                    {"lead_id": "1", "contact_name": "A", "contact_email": "a@x.com", "lead_score": 80, "preferred_location": "Phuket", "budget": "Not specified"},
                    {"lead_id": "2", "contact_name": "B", "contact_email": "b@x.com", "lead_score": 70, "preferred_location": "Samui", "budget": "5,000,000 THB"},
                    {"lead_id": "3", "contact_name": "C", "contact_email": "c@x.com", "lead_score": 60, "preferred_location": "Bangkok", "budget": "Flexible"},
                ],
            }
        }
    ]
    bad = "I found 9 leads from this week."
    v = verify_admin_claims(bad, tool_results)
    assert v.ok is False
    assert any("wrong_count" in h for h in v.hard_mismatches)

    with patch("app.services.audit_service.record_audit_log", return_value=SimpleNamespace(id="a")):
        out = verify_and_correct_admin_reply(
            bad, tool_results, db=MagicMock(), regenerate_fn=None
        )
    assert out.intervention == "templated"
    assert "3" in (out.final_text or "")


def test_roi_percentages_in_internal_notes_pass_untouched():
    """Soft / allowed: ROI % that appear only inside internal_notes strings."""
    tool_results = [
        {
            "result": {
                "name": "Pirak Villa",
                "status": "available",
                "location": "Phuket",
                "bedrooms": 4,
                "completion_date": "2024",
                "amenities": ["pool"],
                "price_display": "25M THB",
                "internal_notes": "Projected ROI 9.7% year-1, stabilized 6.6% net.",
                "developer_contact": "secret@dev.example",
            }
        }
    ]
    good = (
        "Pirak Villa in Phuket: 4 bedrooms, completion 2024. "
        "Internal projections mention about 9.7% and 6.6% ROI."
    )
    v = verify_admin_claims(good, tool_results)
    assert v.ok is True
    # Should not hard-fail on percentages sourced from notes
    assert not any(h.startswith("wrong_count") for h in v.hard_mismatches)

    audits = []
    with patch(
        "app.services.audit_service.record_audit_log",
        side_effect=lambda **kw: audits.append(kw) or SimpleNamespace(id="a"),
    ):
        out = verify_and_correct_admin_reply(
            good,
            tool_results,
            db=MagicMock(),
            regenerate_fn=lambda i: "SHOULD_NOT_REPLACE",
        )
    assert out.final_text == good
    assert out.intervention in ("none", "soft_logged")
    # Must not template-replace with JSON dump
    assert "developer_contact" not in (out.final_text or "")
    assert "internal_notes" not in (out.final_text or "").lower() or out.intervention != "templated"


def test_wrong_count_fixed_by_regeneration():
    tool_results = [{"result": {"count": 3, "items": [{}, {}, {}]}}]
    bad = "There are 99 listings available."

    def regen(_instruction: str) -> str:
        return "There are 3 listings available."

    audits = []
    with patch(
        "app.services.audit_service.record_audit_log",
        side_effect=lambda **kw: audits.append(kw) or SimpleNamespace(id="a"),
    ):
        out = verify_and_correct_admin_reply(
            bad,
            tool_results,
            db=MagicMock(),
            staff_user_id="a@b.com",
            conversation_id="c1",
            regenerate_fn=regen,
        )
    assert out.ok is True
    assert out.intervention == "regenerated"
    assert out.final_text == "There are 3 listings available."
    assert audits


def test_success_claimed_on_mock_result_rewritten():
    tool_results = [
        {
            "result": {
                "success": False,
                "is_mock": True,
                "delivered": False,
                "provider": "disconnected",
                "error": "Google Workspace is not connected",
            }
        }
    ]
    bad = "The email has been sent successfully to the client."
    assert reply_claims_success(bad) is True
    assert tool_results_allow_success_claim(tool_results) is False
    first = verify_admin_claims(bad, tool_results)
    assert first.ok is False
    assert first.success_violations

    with patch("app.services.audit_service.record_audit_log", return_value=SimpleNamespace(id="a")):
        out = verify_and_correct_admin_reply(
            bad,
            tool_results,
            db=MagicMock(),
            regenerate_fn=None,
        )
    assert out.intervention == "templated"
    low = (out.final_text or "").lower()
    assert "successfully sent" not in low
    assert "did not complete" in low or "not complete" in low or "is_mock" in low


def test_success_claimed_on_failed_result_rewritten():
    tool_results = [
        {"result": {"success": False, "is_mock": False, "error": "SMTP error"}}
    ]
    bad = "✅ **Action completed successfully!** The listing was updated."
    v = verify_admin_claims(bad, tool_results)
    assert v.ok is False
    assert v.success_violations


def test_real_success_allows_success_claim():
    tool_results = [
        {"result": {"success": True, "is_mock": False, "delivered": True, "message_id": "m1"}}
    ]
    good = "The email has been sent successfully."
    v = verify_admin_claims(good, tool_results)
    assert v.ok is True
    assert v.success_violations == []


def test_clean_reply_untouched():
    tool_results = [
        {
            "result": {
                "count": 2,
                "properties": [
                    {"property_id": "villa-a", "name": "Villa A", "price_display": "10M THB"},
                    {"property_id": "villa-b", "name": "Villa B"},
                ],
            }
        }
    ]
    clean = "Found **2 properties**: `villa-a` and `villa-b`."
    v = verify_admin_claims(clean, tool_results)
    assert v.ok is True

    audits = []
    with patch(
        "app.services.audit_service.record_audit_log",
        side_effect=lambda **kw: audits.append(kw) or SimpleNamespace(id="a"),
    ):
        out = verify_and_correct_admin_reply(
            clean,
            tool_results,
            db=MagicMock(),
            regenerate_fn=lambda i: "SHOULD_NOT_RUN",
        )
    assert out.intervention == "none"
    assert out.final_text == clean
    assert audits == []


def test_template_lists_tool_counts():
    md = build_deterministic_reply_from_tools(
        [{"result": {"count": 4, "items": [{"property_id": "p1", "name": "One"}]}}],
        language="en",
    )
    assert "4" in md
    assert "p1" in md or "One" in md


def test_template_five_leads_filled_lines():
    leads = []
    for i in range(5):
        leads.append({
            "lead_id": f"L{i}",
            "contact_name": f"Person {i}",
            "contact_email": f"p{i}@ex.com",
            "contact_phone": f"+66{i}",
            "lead_score": 70 + i,
            "preferred_location": "Phuket",
            "budget": "Not specified" if i % 2 else "10,000,000 THB",
            "created_at": "2026-04-01",
        })
    md = build_deterministic_reply_from_tools(
        [{"result": {"total": 5, "leads": leads, "window_label": "last 7 days (2026-04-01 to 2026-04-08)"}}],
        language="en",
    )
    assert "last 7 days" in md
    for i in range(5):
        assert f"Person {i}" in md
    assert "``" not in md
    assert "-  \n" not in md
    assert "internal_notes" not in md.lower()


def test_template_unknown_payload_no_json_no_blanks():
    md = build_deterministic_reply_from_tools(
        [{"result": {"foo_metric": 42, "bar_label": "alpha", "empty": "", "nested": {"x": 1}}}],
        language="en",
    )
    assert "{" not in md or "Tool data" in md
    assert "```" not in md
    assert "foo_metric: 42" in md or "42" in md
    assert "internal_notes" not in md.lower()
    # no empty bullets
    for line in md.splitlines():
        assert line.strip() not in ("-", "- ``", "- ** **")


def test_template_never_shows_internal_notes_or_developer_contact():
    md = build_deterministic_reply_from_tools(
        [{
            "result": {
                "name": "Pirak Villa",
                "status": "available",
                "location": "Phuket",
                "bedrooms": 3,
                "completion_date": "2025",
                "amenities": ["pool", "gym"],
                "price_display": "18M THB",
                "internal_notes": "SECRET ROI NOTES 9.7%",
                "developer_contact": "dev@secret.com",
            }
        }],
        language="en",
    )
    low = md.lower()
    assert "secret roi" not in low
    assert "dev@secret.com" not in low
    assert "developer_contact" not in low
    assert "Pirak Villa" in md
    assert "Phuket" in md
