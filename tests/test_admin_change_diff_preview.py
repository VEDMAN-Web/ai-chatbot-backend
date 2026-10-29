"""
TASK 4 — Before/after Field|Current|New diffs from live store;
stale-value protection; risky warnings; confirm applies only previewed fields.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.auth import AuthenticatedStaff
from app.services import action_service
from app.services.action_service import (
    ACTION_STATUS_PENDING,
    ACTION_STATUS_REJECTED,
    ACTION_STATUS_SUPERSEDED,
)
from app.services.admin_change_diff import (
    assess_risky_changes,
    build_batch_change_diff,
    build_single_listing_change_diff,
    format_batch_diff_markdown,
    format_single_diff_markdown,
    message_acks_delete_count,
    verify_diff_still_current,
)
from app.services.admin_property_fields import UNREADABLE, expand_proposed_fields, parse_price_amount
from app.services.admin_agent import is_strict_confirm_message, process_admin_chat
from app.services.action_service import create_pending_action
from tests.fake_firestore import patch_firestore, set_store_modes


@pytest.fixture
def staff():
    return AuthenticatedStaff(
        user_id="admin-001",
        email="admin@tajlandia.com",
        role="super_admin",
    )


def _pending(**kwargs):
    return SimpleNamespace(
        id=kwargs.get("id", uuid4()),
        confirmation_token="act_tok",
        staff_user_id="admin@tajlandia.com",
        tool_name=kwargs.get("tool_name", "create_or_update_listing"),
        action_payload=kwargs.get("action_payload", {}),
        status=ACTION_STATUS_PENDING,
        conversation_id="c1",
        expires_at=MagicMock(),
        created_at=MagicMock(),
        confirmed_at=None,
        executed_at=None,
        execution_result=None,
        error_message=None,
    )


def test_single_diff_uses_stored_current_values():
    stored = {
        "property_id": "prop-1",
        "property_name": "Villa Kata",
        "price_display": "10M THB",
        "price_numeric_min": 10_000_000,
        "status": "Available",
    }

    with patch(
        "app.services.admin_change_diff.read_stored_property_fields",
        return_value=stored,
    ):
        diff = build_single_listing_change_diff(
            db=MagicMock(),
            property_id="prop-1",
            proposed_fields={
                "price_display": "15M THB",
                "price_numeric_min": 15_000_000,
                "status": "Available",  # unchanged — omitted from rows
            },
            mode="update",
        )

    fields = {r["field"] for r in diff["rows"]}
    assert "price_display" in fields
    assert "price_numeric_min" in fields
    assert "status" not in fields  # unchanged
    price_row = next(r for r in diff["rows"] if r["field"] == "price_display")
    # Display string kept; numeric companion may also appear normalized
    assert "10" in str(price_row["current"])
    assert "15" in str(price_row["new"])
    md = diff["diff_markdown"]
    assert "Field" in md and "Current" in md and "New" in md
    assert "10M THB" in md and "15M THB" in md


def test_batch_diff_summary_and_ellipsis():
    items = [
        {"property_id": f"p{i}", "name": f"Prop {i}", "change": {"fields": {"status": "Sold"}}}
        for i in range(12)
    ]

    def fake_read(db, pid, field_keys=None):
        return {"property_id": pid, "status": "Available", "property_name": pid}

    with patch(
        "app.services.admin_change_diff.read_stored_property_fields",
        side_effect=fake_read,
    ):
        diff = build_batch_change_diff(
            db=MagicMock(),
            operation_type="update",
            items=items,
            update_fields={"status": "Sold"},
        )

    assert diff["item_count"] == 12
    md = diff["diff_markdown"]
    assert "12 properties will change" in md
    assert "...and 7 more" in md
    assert "Available" in md and "Sold" in md


def test_risky_price_over_30_percent_and_delete_warning():
    rows = [
        {"field": "price_numeric_min", "current": 10_000_000, "new": 16_000_000},
    ]
    warns = assess_risky_changes(operation_type="update", rows=rows)
    assert any("30%" in w for w in warns)

    del_warns = assess_risky_changes(operation_type="delete", delete_count=5, item_diffs=[{}] * 5)
    assert any("DELETE" in w for w in del_warns)

    inactive = assess_risky_changes(
        operation_type="update",
        item_diffs=[
            {"rows": [{"field": "status", "current": "Available", "new": "Sold"}]},
            {"rows": [{"field": "status", "current": "Available", "new": "Inactive"}]},
        ],
    )
    assert any("sold/inactive" in w.lower() for w in inactive)


def test_message_acks_delete_count_and_strict_confirm():
    assert message_acks_delete_count("yes 12", 12)
    assert message_acks_delete_count("tak 3", 3)
    assert not message_acks_delete_count("yes", 12)
    assert is_strict_confirm_message("yes 12")
    assert is_strict_confirm_message("tak 3")


def test_verify_diff_detects_stale_current():
    change_diff = {
        "property_id": "prop-1",
        "operation_type": "update",
        "rows": [
            {"field": "price_display", "current": "10M THB", "new": "15M THB"},
        ],
    }
    with patch(
        "app.services.admin_change_diff.read_stored_property_fields",
        return_value={"price_display": "12M THB"},
    ):
        ok, stale = verify_diff_still_current(MagicMock(), change_diff)
    assert ok is False
    assert stale and "prop-1.price_display" in stale[0]


def test_confirm_refuses_stale_and_reproposes(staff):
    change_diff = {
        "property_id": "prop-1",
        "operation_type": "update",
        "rows": [
            {"field": "price_display", "current": "10M THB", "new": "15M THB"},
        ],
        "diff_markdown": "| Field | Current | New |",
        "risk_warnings": [],
    }
    action = _pending(
        action_payload={
            "mode": "update",
            "property_id": "prop-1",
            "fields_to_update": {"price_display": "15M THB"},
            "change_diff": change_diff,
        }
    )
    created = []

    def fake_create(**kwargs):
        pa = _pending(action_payload=kwargs.get("action_payload"))
        created.append(pa)
        return pa

    db = MagicMock()
    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a1")
    ), patch(
        "app.services.action_service.create_pending_action", side_effect=fake_create
    ), patch(
        "app.services.admin_change_diff.verify_diff_still_current",
        return_value=(False, ["prop-1.price_display: expected `10M THB`, now `12M THB`"]),
    ), patch(
        "app.services.admin_change_diff.build_single_listing_change_diff",
        return_value={
            "property_id": "prop-1",
            "operation_type": "update",
            "rows": [{"field": "price_display", "current": "12M THB", "new": "15M THB"}],
            "diff_markdown": "| `price_display` | 12M THB | 15M THB |",
            "risk_warnings": [],
        },
    ), patch(
        "app.services.action_service._execute_update_listing"
    ) as exe:
        out = action_service.confirm_and_execute_action(
            db=db, pending_action_id=str(action.id), authenticated_staff=staff, confirmation_token=action.confirmation_token
        )

    assert out["status"] == "STALE_REFUSED"
    assert action.status == ACTION_STATUS_SUPERSEDED
    assert not exe.called
    assert created, "must re-propose a fresh PendingAction"
    assert out.get("pending_action_id") == str(created[0].id)
    assert str(created[0].id) in (out.get("message") or "")


def test_confirm_applies_only_previewed_fields(staff, monkeypatch):
    """Extra fields in payload that were not in the diff table must not be written."""
    change_diff = {
        "property_id": "prop-1",
        "operation_type": "update",
        "rows": [
            {"field": "price_display", "current": "10M THB", "new": "12M THB"},
        ],
        "risk_warnings": [],
    }
    action = _pending(
        action_payload={
            "mode": "update",
            "property_id": "prop-1",
            "fields_to_update": {
                "price_display": "12M THB",
                "property_name": "SHOULD_NOT_APPLY",  # not in preview rows
                "status": "Sold",  # not in preview rows
            },
            "change_diff": change_diff,
        }
    )
    fake = patch_firestore(monkeypatch)
    fake.docs[("projects", "prop-1")] = {
        "name": "Keep Name", "status": "Available", "priceDisplay": "10M THB",
        "websites": ["tajlandia.com"], "isActive": True,
    }
    db = MagicMock()

    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a1")
    ), patch(
        "app.services.admin_change_diff.verify_diff_still_current", return_value=(True, [])
    ), patch(
        "app.services.action_service.validate_property_data", return_value=(True, [], "")
    ), patch(
        "app.services.action_service.record_crud_operation_snapshot", return_value="op_preview1"
    ), patch(
        "app.services.cache_service.search_cache.invalidate"
    ), patch(
        "app.core.config.settings.ENABLE_FIRESTORE_SYNC", False
    ):
        out = action_service.confirm_and_execute_action(
            db=db, pending_action_id=str(action.id), authenticated_staff=staff, confirmation_token=action.confirmation_token
        )

    assert out["status"] == action_service.ACTION_STATUS_EXECUTED
    # Only price_display from the preview should have been applied
    stored = fake.docs[("projects", "prop-1")]
    assert stored["priceDisplay"] == "12M THB"
    assert stored["name"] == "Keep Name"
    assert stored["status"] == "Available"
    assert out["execution_result"].get("operation_id") == "op_preview1"


def test_chat_delete_requires_count_ack(staff, monkeypatch):
    patch_firestore(monkeypatch)
    set_store_modes(monkeypatch)
    conv_id = "c1"
    action = create_pending_action(
        db=MagicMock(),
        staff_user_id=staff.email,
        tool_name="execute_batch_property_action",
        action_payload={
            "operation_type": "delete",
            "items": [{"property_id": "a"}, {"property_id": "b"}, {"property_id": "c"}],
            "requires_explicit_delete_ack": True,
            "delete_count": 3,
            "change_diff": {
                "operation_type": "delete",
                "requires_explicit_delete_ack": True,
                "delete_count": 3,
                "items": [{"property_id": "a"}, {"property_id": "b"}, {"property_id": "c"}],
            },
        },
        conversation_id=conv_id,
    )

    monkeypatch.setattr(
        "app.services.admin_agent.get_or_create_conversation",
        lambda **kw: SimpleNamespace(
            id=conv_id,
            session_id="s1",
            preferences={"language": "en"},
            message_history=[],
            site_origin="admin",
        ),
    )
    monkeypatch.setattr("app.services.admin_agent.append_conversation_turn", lambda *a, **k: None)
    monkeypatch.setattr("app.services.admin_agent.resolve_language", lambda **k: ("en", False))

    with patch("app.services.admin_batch_service.execute_batch_property_operation") as batch_exec:
        res = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message="yes",
            staff_user=staff,
            site_origin="admin",
        )
    assert not batch_exec.called
    assert "3" in (res.get("text") or "")
    assert res.get("action_status") == "pending"

    with patch(
        "app.services.admin_batch_service.execute_batch_property_operation",
        return_value={
            "success": True,
            "operation_id": "batch_del1",
            "affected_ids": ["a", "b", "c"],
            "affected_count": 3,
        },
    ) as batch_exec2:
        res2 = process_admin_chat(
            db=MagicMock(),
            session_id="s1",
            message="yes 3",
            staff_user=staff,
            site_origin="admin",
        )
    assert batch_exec2.called
    assert res2.get("action_status") == "executed"
    assert "undo" in (res2.get("text") or "").lower()
    assert res2.get("operation_id") == "batch_del1"
    assert "batch_del1" not in (res2.get("text") or "")


def test_format_helpers_smoke():
    md = format_single_diff_markdown(
        "p1",
        [{"field": "status", "current": "Available", "new": "Sold"}],
        property_name="Villa",
    )
    assert "| Field | Current | New |" in md
    batch = format_batch_diff_markdown(
        "update",
        [
            {
                "property_id": "p1",
                "name": "A",
                "rows": [{"field": "status", "current": "A", "new": "B"}],
            }
        ]
        + [{"property_id": f"p{i}", "name": f"N{i}", "rows": []} for i in range(2, 8)],
    )
    assert "7 properties will change" in batch
    assert "...and 2 more" in batch


def test_proposal_shows_real_current_price_not_dash():
    """Alias 'price' maps to price_numeric_min; Current shows 39,990,000 not '—'."""
    stored = {
        "property_id": "pirak-villa",
        "property_name": "Pirak Villa",
        "price_numeric_min": 39_990_000,
        "price_display": "39,990,000 THB",
        "_readable": {"price_numeric_min": True, "price_display": True, "property_name": True},
    }
    with patch(
        "app.services.admin_change_diff.read_stored_property_fields",
        return_value=stored,
    ):
        diff = build_single_listing_change_diff(
            db=MagicMock(),
            property_id="pirak-villa",
            proposed_fields={"price": "5 million"},
            mode="update",
        )
    assert diff["rows"], "expected at least one price row"
    # Only price-related rows — no websites
    fields = {r["field"] for r in diff["rows"]}
    assert "websites" not in fields
    price_rows = [r for r in diff["rows"] if "price" in r["field"]]
    assert price_rows
    for r in price_rows:
        assert r["current"] != "—"
        assert r["current"] != UNREADABLE or r.get("readable") is False
        assert "39,990,000" in str(r["current"]) or "39990000" in str(r["current"]).replace(",", "")
    assert "5,000,000" in str(price_rows[0]["new"]) or "5000000" in str(price_rows[0]["new"]).replace(",", "")


def test_confirm_right_after_proposal_succeeds_no_stale(staff, monkeypatch):
    """Same current at confirm → apply; no endless stale loop."""
    change_diff = {
        "property_id": "pirak-villa",
        "operation_type": "update",
        "rows": [
            {
                "field": "price_numeric_min",
                "current": "39,990,000",
                "new": "5,000,000",
                "readable": True,
            }
        ],
        "canonical_fields": {"price_numeric_min": 5_000_000, "price_display": "5,000,000 THB"},
        "risk_warnings": [],
    }
    action = _pending(
        action_payload={
            "mode": "update",
            "property_id": "pirak-villa",
            "fields_to_update": {"price_numeric_min": 5_000_000, "price_display": "5,000,000 THB"},
            "change_diff": change_diff,
        }
    )
    live = {
        "property_id": "pirak-villa",
        "price_numeric_min": 39_990_000,
        "price_display": "39,990,000 THB",
        "_readable": {"price_numeric_min": True, "price_display": True},
    }
    fake = patch_firestore(monkeypatch)
    fake.docs[("projects", "pirak-villa")] = {
        "name": "Pirak Villa", "status": "Available", "priceDisplay": "39,990,000 THB",
        "priceFrom": 39_990_000, "websites": ["tajlandia.com"], "isActive": True,
    }
    db = MagicMock()

    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="aud1")
    ), patch(
        "app.services.admin_change_diff.read_stored_property_fields",
        return_value=live,
    ), patch(
        "app.services.action_service.validate_property_data", return_value=(True, [], "")
    ), patch(
        "app.services.action_service.record_crud_operation_snapshot", return_value="op1"
    ), patch(
        "app.services.cache_service.search_cache.invalidate"
    ), patch(
        "app.core.config.settings.ENABLE_FIRESTORE_SYNC", False
    ):
        out = action_service.confirm_and_execute_action(
            db=db, pending_action_id=str(action.id), authenticated_staff=staff, confirmation_token=action.confirmation_token
        )

    assert out["status"] == action_service.ACTION_STATUS_EXECUTED
    assert float(fake.docs[("projects", "pirak-villa")]["priceFrom"]) == 5_000_000


def test_real_stale_refused_once_then_fresh_confirmable(staff):
    """Real drift → one SUPERSEDED + one new pending; second confirm with matching current applies."""
    change_diff = {
        "property_id": "pirak-villa",
        "operation_type": "update",
        "rows": [
            {"field": "price_numeric_min", "current": "39,990,000", "new": "5,000,000", "readable": True}
        ],
        "canonical_fields": {"price_numeric_min": 5_000_000},
    }
    action = _pending(
        action_payload={
            "mode": "update",
            "property_id": "pirak-villa",
            "fields_to_update": {"price_numeric_min": 5_000_000},
            "change_diff": change_diff,
        }
    )
    created = []

    def fake_create(**kwargs):
        pa = _pending(id=uuid4(), action_payload=kwargs.get("action_payload"))
        created.append(pa)
        return pa

    with patch("app.services.action_service.get_pending_action", return_value=action), patch(
        "app.services.action_service.record_audit_log", return_value=SimpleNamespace(id="a1")
    ), patch(
        "app.services.action_service.create_pending_action", side_effect=fake_create
    ), patch(
        "app.services.admin_change_diff.verify_diff_still_current",
        return_value=(False, ["pirak-villa.price_numeric_min: expected `39990000`, now `42000000`"]),
    ), patch(
        "app.services.admin_change_diff.build_single_listing_change_diff",
        return_value={
            "property_id": "pirak-villa",
            "operation_type": "update",
            "rows": [
                {"field": "price_numeric_min", "current": "42,000,000", "new": "5,000,000", "readable": True}
            ],
            "canonical_fields": {"price_numeric_min": 5_000_000},
            "diff_markdown": "| `price_numeric_min` | 42,000,000 | 5,000,000 |",
            "risk_warnings": [],
        },
    ), patch(
        "app.services.action_service._execute_update_listing"
    ) as exe:
        out = action_service.confirm_and_execute_action(
            db=MagicMock(), pending_action_id=str(action.id), authenticated_staff=staff, confirmation_token=action.confirmation_token
        )

    assert out["status"] == "STALE_REFUSED"
    assert action.status == ACTION_STATUS_SUPERSEDED
    assert not exe.called
    assert len(created) == 1
    assert out["pending_action_id"] == str(created[0].id)


def test_unreadable_fields_skipped_in_stale_check():
    change_diff = {
        "property_id": "p1",
        "operation_type": "update",
        "rows": [
            {"field": "price_numeric_min", "current": UNREADABLE, "new": "5,000,000", "readable": False},
            {"field": "status", "current": "Available", "new": "Sold", "readable": True},
        ],
    }
    with patch(
        "app.services.admin_change_diff.read_stored_property_fields",
        return_value={
            "price_numeric_min": 39_990_000,
            "status": "Available",
            "_readable": {"price_numeric_min": True, "status": True},
        },
    ):
        ok, stale = verify_diff_still_current(MagicMock(), change_diff)
    # price was unreadable at proposal — not stale; status still matches
    assert ok is True
    assert stale == []


def test_price_only_update_no_websites_and_percent_change():
    stored = {
        "property_id": "p1",
        "price_numeric_min": 10_000_000,
        "price_display": "10,000,000 THB",
        "_readable": {"price_numeric_min": True, "price_display": True},
    }
    expanded, errs = expand_proposed_fields(
        {"price": "increase by 80%"},
        current_stored=stored,
        user_message="increase price by 80%",
        allow_websites=False,
    )
    assert "websites" not in expanded
    assert abs(float(expanded["price_numeric_min"]) - 18_000_000) < 1
    assert not any("Unknown" in e for e in errs)

    with patch(
        "app.services.admin_change_diff.read_stored_property_fields",
        return_value=stored,
    ):
        diff = build_single_listing_change_diff(
            db=MagicMock(),
            property_id="p1",
            proposed_fields={"price": "increase by 80%"},
            mode="update",
            user_message="increase price of villa by 80%",
        )
    assert len(diff["rows"]) >= 1
    assert all(r["field"] != "websites" for r in diff["rows"])
    # price-only (display may accompany numeric; currency must not be injected)
    assert all(
        r["field"] in ("price_numeric_min", "price_numeric_max", "price_display")
        for r in diff["rows"]
    )


def test_parse_price_variants():
    assert parse_price_amount("5 million") == 5_000_000
    assert parse_price_amount("5M") == 5_000_000
    assert parse_price_amount("5 mln") == 5_000_000
    assert parse_price_amount("5,000,000") == 5_000_000
    assert parse_price_amount(5_000_000) == 5_000_000


def test_update_existing_never_requires_all_fields():
    from app.services.action_service import validate_property_data

    existing = SimpleNamespace(property_id="p1", property_name="X")
    ok, missing, msg = validate_property_data(
        {
            "mode": "update",
            "property_id": "p1",
            "fields_to_update": {"price_numeric_min": 5_000_000},
            "is_update_only": True,
        },
        existing_property=existing,
    )
    assert ok is True
    assert missing == []
    assert "property_name" not in msg
    assert "bedrooms" not in msg
