"""Proposed emails and calendar events are validated by the backend before they can be staged for confirmation."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.db.models import AuditLog, PendingAction
from app.db.session import SessionLocal
from app.services.action_service import (
    create_pending_action,
    validate_calendar_action,
    validate_email_action,
)

STAFF = "val_test_staff@tajlandia.com"


def _future(days=3, offset="+07:00"):
    when = datetime.now(timezone.utc) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S") + offset


GOOD_EMAIL = {"to": "client@example.com", "subject": "Your viewing", "body": "See you Monday."}
GOOD_EVENT = {"title": "Viewing: Layan Palm Villa", "start_time": _future(), "end_time": _future(3, "+07:00"), "attendees": ["client@example.com"]}


def test_valid_email_has_no_problems():
    assert validate_email_action(GOOD_EMAIL) == []
    assert validate_email_action({**GOOD_EMAIL, "to": "a@example.com, b@example.org"}) == []


@pytest.mark.parametrize("change", [
    {"to": ""}, {"to": "not-an-email"}, {"to": "a@example.com, nope"}, {"to": "victim@example.com\nBcc: x@evil.com"},
    {"to": ",".join(f"u{i}@example.com" for i in range(11))},
    {"subject": ""}, {"subject": "Hi\nBcc: x@evil.com"}, {"subject": "x" * 201},
    {"body": ""}, {"body": "x" * 20001},
])
def test_invalid_emails_are_reported(change):
    assert validate_email_action({**GOOD_EMAIL, **change})


def test_valid_event_has_no_problems():
    assert validate_calendar_action(GOOD_EVENT) == []
    assert validate_calendar_action({**GOOD_EVENT, "end_time": None, "attendees": None}) == []


@pytest.mark.parametrize("change,fragment", [
    ({"title": ""}, "title"),
    ({"start_time": "next Monday"}, "ISO 8601"),
    ({"start_time": "2030-01-01T10:00:00"}, "UTC offset"),
    ({"start_time": "2020-01-01T10:00:00+00:00"}, "past"),
    ({"end_time": "2030-01-01T10:00:00"}, "UTC offset"),
    ({"end_time": "2020-01-01T10:00:00+00:00"}, "before start_time"),
    ({"attendees": ["not-an-email"]}, "attendee"),
    ({"attendees": [f"u{i}@example.com" for i in range(51)]}, "attendees"),
])
def test_invalid_events_are_reported(change, fragment):
    problems = validate_calendar_action({**GOOD_EVENT, **change})
    assert any(fragment in p for p in problems), problems


def test_zulu_time_is_accepted():
    assert validate_calendar_action({**GOOD_EVENT, "start_time": _future(offset="") + "Z", "end_time": None}) == []


@pytest.mark.parametrize("tool,payload", [
    ("send_email", {**GOOD_EMAIL, "to": "bad"}),
    ("create_calendar_event", {**GOOD_EVENT, "start_time": "2030-01-01T10:00:00"}),
])
def test_an_invalid_action_is_never_staged(tool, payload):
    db = MagicMock()
    with pytest.raises(HTTPException) as err:
        create_pending_action(db=db, staff_user_id=STAFF, tool_name=tool, action_payload=payload)
    assert err.value.status_code == 422 and "Cannot stage this action" in err.value.detail
    db.add.assert_not_called()


@pytest.mark.parametrize("tool,payload", [("send_email", GOOD_EMAIL), ("create_calendar_event", GOOD_EVENT)])
def test_a_valid_action_is_staged_as_pending(tool, payload):
    db = SessionLocal()
    try:
        action = create_pending_action(db=db, staff_user_id=STAFF, tool_name=tool, action_payload=payload)
        assert action.status == "PENDING" and action.staff_user_id == STAFF
    finally:
        db.query(PendingAction).filter(PendingAction.staff_user_id == STAFF).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.staff_user_id == STAFF).delete(synchronize_session=False)
        db.commit()
        db.close()
