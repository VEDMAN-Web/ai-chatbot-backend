"""
TASK 6 — Admin claim verifier against this turn's tool results.
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
    assert "12" in first.unsupported_numbers

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
    assert "12" not in (out.final_text or "") or "tool" in (out.final_text or "").lower()
    assert "5" in (out.final_text or "")
    assert audits and audits[0].get("tool_name") == "admin_claim_verifier"
    assert "templated" in (audits[0].get("error_message") or audits[0].get("execution_result") or "")


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
