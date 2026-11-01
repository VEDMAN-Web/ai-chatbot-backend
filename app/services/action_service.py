import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple
from fastapi import HTTPException, status
import logging

from app.core.config import settings
from app.core.auth import AuthenticatedStaff
from app.services.audit_service import record_audit_log, sanitize_payload, scrub_text
from app.services.google_services import google_manager
from app.repositories import pending_actions_repo

# Re-exported so tests can patch `app.services.action_service.record_crud_operation_snapshot`
from app.services.admin_batch_service import record_crud_operation_snapshot  # noqa: E402

logger = logging.getLogger(__name__)

# Lifecycle States (kept in sync with pending_actions_repo)
ACTION_STATUS_PENDING = pending_actions_repo.ACTION_STATUS_PENDING
ACTION_STATUS_CONFIRMED = pending_actions_repo.ACTION_STATUS_CONFIRMED
ACTION_STATUS_EXECUTED = pending_actions_repo.ACTION_STATUS_EXECUTED
ACTION_STATUS_REJECTED = pending_actions_repo.ACTION_STATUS_REJECTED
ACTION_STATUS_EXPIRED = pending_actions_repo.ACTION_STATUS_EXPIRED
ACTION_STATUS_FAILED = pending_actions_repo.ACTION_STATUS_FAILED
ACTION_STATUS_SUPERSEDED = pending_actions_repo.ACTION_STATUS_SUPERSEDED

# Back-compat alias for type hints / isinstance checks in tests
PendingAction = pending_actions_repo.PendingActionRecord


# Canonical listing values. Input is matched case-insensitively and stored in this form.
ALLOWED_PROPERTY_TYPES: Dict[str, str] = {
    "villa": "Villa",
    "condominium": "Condominium",
    "condo": "Condominium",
    "townhouse": "Townhouse",
    "penthouse": "Penthouse",
    "apartment": "Apartment",
    "house": "House",
    "land": "Land",
    "commercial": "Commercial",
}
ALLOWED_STATUSES: Dict[str, str] = {
    "ready to move in": "Ready to move in",
    "under construction": "Under construction",
    "off-plan": "Off-plan",
    "off plan": "Off-plan",
    "pre-sale": "Pre-sale",
    "presale": "Pre-sale",
    "available": "Available",
    "planned": "Planned",
    "sold out": "Sold Out",
    "archived": "Archived",
    "reserved": "Reserved",
    "for sale": "For sale",
}
_PROPERTY_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BLANK_STRINGS = {"", "none", "null"}


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in _BLANK_STRINGS:
        return True
    return False


def property_id_slug_error(property_id: Optional[str]) -> Optional[str]:
    """Slug rule: lowercase letters, digits, hyphens, max 64. None when valid."""
    pid = "" if property_id is None else str(property_id).strip()
    if not pid or len(pid) > 64 or not _PROPERTY_ID_RE.match(pid):
        return (
            "property_id must be lowercase letters, digits, and hyphens, "
            "with no leading or trailing hyphen, and at most 64 characters"
        )
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text.lower() in _BLANK_STRINGS:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def validate_listing_values(
    fields: Optional[Dict[str, Any]],
    *,
    property_id: Optional[str] = None,
    is_create: bool = False,
    check_property_id: bool = False,
) -> Dict[str, str]:
    """
    Field-level listing validation shared by REST, chat (create_or_update_listing),
    and the batch file path.

    Returns {field: message}. An empty dict means the values are acceptable.
    When status or property_type is valid, `fields` is updated to the canonical form.
    """
    src = fields if isinstance(fields, dict) else {}
    errors: Dict[str, str] = {}

    if check_property_id or (is_create and property_id is not None and str(property_id).strip() != ""):
        if _is_blank(property_id):
            errors["property_id"] = "property_id is required"
        else:
            slug_err = property_id_slug_error(str(property_id))
            if slug_err:
                errors["property_id"] = slug_err

    name = src.get("property_name")
    if name is None:
        name = src.get("title")
    if is_create and _is_blank(name):
        errors["property_name"] = "property_name is required and must be non-blank"
    elif "property_name" in src and _is_blank(src.get("property_name")):
        errors["property_name"] = "property_name must be non-blank"

    prop_type = src.get("property_type")
    if is_create and _is_blank(prop_type):
        errors["property_type"] = (
            "property_type is required (" + ", ".join(sorted(set(ALLOWED_PROPERTY_TYPES.values()))) + ")"
        )
    elif not _is_blank(prop_type):
        canonical_type = ALLOWED_PROPERTY_TYPES.get(str(prop_type).strip().lower())
        if not canonical_type:
            errors["property_type"] = (
                "property_type must be one of: "
                + ", ".join(sorted(set(ALLOWED_PROPERTY_TYPES.values())))
            )
        elif isinstance(fields, dict):
            fields["property_type"] = canonical_type

    loc = src.get("province") if not _is_blank(src.get("province")) else src.get("location")
    if is_create and _is_blank(loc):
        errors["location"] = "province or location is required"

    price_min_raw = src.get("price_numeric_min", src.get("price_from"))
    price_max_raw = src.get("price_numeric_max", src.get("price_to"))
    price_min = _as_number(price_min_raw) if not _is_blank(price_min_raw) else None
    price_max = _as_number(price_max_raw) if not _is_blank(price_max_raw) else None
    if not _is_blank(price_min_raw) and price_min is None:
        errors["price_numeric_min"] = "price_numeric_min must be a number greater than 0"
    elif price_min is not None and price_min <= 0:
        errors["price_numeric_min"] = "price_numeric_min must be greater than 0"
    if not _is_blank(price_max_raw) and price_max is None:
        errors["price_numeric_max"] = "price_numeric_max must be a number greater than 0"
    elif price_max is not None and price_max <= 0:
        errors["price_numeric_max"] = "price_numeric_max must be greater than 0"
    if (
        price_min is not None
        and price_max is not None
        and price_min > 0
        and price_max > 0
        and price_max < price_min
    ):
        errors["price_numeric_max"] = "price_numeric_max must be greater than or equal to price_numeric_min"

    price_display = src.get("price_display") if not _is_blank(src.get("price_display")) else src.get("price")
    has_positive_min = price_min is not None and price_min > 0 and "price_numeric_min" not in errors
    if is_create and not has_positive_min and _is_blank(price_display):
        errors["price"] = "price_numeric_min (> 0) or price_display is required"

    status_val = src.get("status")
    if is_create and _is_blank(status_val):
        errors["status"] = (
            "status is required (" + ", ".join(sorted(set(ALLOWED_STATUSES.values()))) + ")"
        )
    elif not _is_blank(status_val):
        canonical_status = ALLOWED_STATUSES.get(str(status_val).strip().lower())
        if not canonical_status:
            errors["status"] = (
                "status must be one of: " + ", ".join(sorted(set(ALLOWED_STATUSES.values())))
            )
        elif isinstance(fields, dict):
            fields["status"] = canonical_status

    def _check_count(key: str, label: str) -> Optional[float]:
        if key not in src or _is_blank(src.get(key)):
            return None
        num = _as_number(src.get(key))
        if num is None or int(num) != num or num < 0 or num > 50:
            errors[key] = f"{label} must be a whole number from 0 to 50"
            return None
        return num

    beds = _check_count("bedrooms", "bedrooms")
    _check_count("bathrooms", "bathrooms")
    beds_min = _check_count("bedrooms_min", "bedrooms_min")
    beds_max = _check_count("bedrooms_max", "bedrooms_max")
    if beds is not None and "bedrooms_min" not in src and isinstance(fields, dict):
        fields["bedrooms_min"] = int(beds)
        fields["bedrooms_max"] = int(beds)
    if (
        beds_min is not None
        and beds_max is not None
        and beds_max < beds_min
        and "bedrooms_min" not in errors
        and "bedrooms_max" not in errors
    ):
        errors["bedrooms_max"] = "bedrooms_max must be greater than or equal to bedrooms_min"

    def _check_area(key: str) -> Optional[float]:
        if key not in src or _is_blank(src.get(key)):
            return None
        num = _as_number(src.get(key))
        if num is None or num <= 0:
            errors[key] = f"{key} must be greater than 0"
            return None
        return num

    area_min = _check_area("area_sqm_min")
    area_max = _check_area("area_sqm_max")
    if (
        area_min is not None
        and area_max is not None
        and area_max < area_min
        and "area_sqm_min" not in errors
        and "area_sqm_max" not in errors
    ):
        errors["area_sqm_max"] = "area_sqm_max must be greater than or equal to area_sqm_min"

    return errors


def _legacy_missing_keys(errors: Dict[str, str]) -> List[str]:
    """Keep the historical missing-field names chat callers already assert on."""
    missing: List[str] = []
    for key in errors:
        if key not in missing:
            missing.append(key)
    return missing


def validate_property_data(
    payload: Dict[str, Any],
    existing_property: Optional[Any] = None
) -> Tuple[bool, List[str], str]:
    """
    Validates data completeness before adding or updating any property in Cloud Firestore.
    If ANY required value is missing, empty, or null, blocks insertion and returns:
    (False, missing_fields, error_message).

    Create requires only property_name, property_type, province or location,
    price_numeric_min (> 0) or price_display, and status. Bedrooms are optional.
    The same numeric, range, status, type, and property_id rules apply on update.
    """
    fields = payload.get("fields_to_update") or payload.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    mode = payload.get("mode", "update")
    property_id = payload.get("property_id")

    is_create = (
        str(mode).lower() == "create"
        and existing_property is None
        and not payload.get("is_update_only")
        and str(payload.get("resolve_source") or "").lower() not in ("firestore",)
    )

    errors: Dict[str, str] = {}
    if _is_blank(property_id):
        errors["property_id"] = "property_id is required"
    elif is_create:
        slug_err = property_id_slug_error(str(property_id))
        if slug_err:
            errors["property_id"] = slug_err

    if is_create:
        errors.update(validate_listing_values(fields, property_id=None, is_create=True))
    else:
        # Update: only provided fields are checked. Never demand a full create payload.
        if existing_property is not None:
            if not fields:
                errors["fields_to_update"] = "at least one field to update is required"
            else:
                for k, v in fields.items():
                    if k in ("websites", "selected_websites", "mode", "property_id"):
                        continue
                    if _is_blank(v):
                        errors[k] = f"{k} must not be null or blank"
        elif not fields:
            errors["fields_to_update"] = "at least one field to update is required"
        else:
            for k, v in fields.items():
                if _is_blank(v):
                    errors[k] = f"{k} must not be null or blank"
        if fields and "fields_to_update" not in errors:
            value_errors = validate_listing_values(fields, is_create=False)
            for key, message in value_errors.items():
                errors.setdefault(key, message)

    if errors:
        missing = _legacy_missing_keys(errors)
        detail = "; ".join(f"{key}: {message}" for key, message in errors.items())
        msg = (
            "Without proper information I am not able to complete this task. "
            f"The following required value(s) are missing or null: {', '.join(missing)}. "
            f"{detail}"
        )
        return False, missing, msg

    return True, [], ""


_EMAIL_ADDRESS_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]{2,}$")
MAX_EMAIL_RECIPIENTS = 10
MAX_CALENDAR_ATTENDEES = 50


def _single_line(value: Any, label: str, max_len: int, problems: List[str]) -> None:
    text = value if isinstance(value, str) else ""
    if not text.strip():
        problems.append(f"{label} is required")
    elif len(text) > max_len:
        problems.append(f"{label} must be at most {max_len} characters")
    elif "\r" in text or "\n" in text:
        problems.append(f"{label} must be a single line")


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def validate_email_action(payload: Dict[str, Any]) -> List[str]:
    """Problems with a proposed email; an empty list means it can be staged for confirmation."""
    problems: List[str] = []
    recipients = [part.strip() for part in re.split(r"[;,]", str(payload.get("to") or "")) if part.strip()]
    if not recipients:
        problems.append("a recipient email address is required")
    elif len(recipients) > MAX_EMAIL_RECIPIENTS:
        problems.append(f"at most {MAX_EMAIL_RECIPIENTS} recipients are allowed")
    elif not all(_EMAIL_ADDRESS_RE.match(r) for r in recipients):
        problems.append("the recipient is not a valid email address")
    _single_line(payload.get("subject"), "subject", 200, problems)
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        problems.append("the message body is required")
    elif len(body) > 20000:
        problems.append("the message body must be at most 20000 characters")
    return problems


def validate_calendar_action(payload: Dict[str, Any]) -> List[str]:
    """Problems with a proposed calendar event. Times need an explicit UTC offset, e.g. 2026-11-03T10:00:00+07:00."""
    problems: List[str] = []
    _single_line(payload.get("title"), "title", 200, problems)
    start = _parse_iso_datetime(payload.get("start_time"))
    if start is None:
        problems.append("start_time must be an ISO 8601 date and time")
    elif start.tzinfo is None:
        problems.append("start_time must include a UTC offset (for example 2026-11-03T10:00:00+07:00)")
    elif start < datetime.now(timezone.utc) - timedelta(minutes=5):
        problems.append("start_time is in the past")
    if payload.get("end_time"):
        end = _parse_iso_datetime(payload.get("end_time"))
        if end is None:
            problems.append("end_time must be an ISO 8601 date and time")
        elif end.tzinfo is None:
            problems.append("end_time must include a UTC offset")
        elif start is not None and start.tzinfo is not None and end < start:
            problems.append("end_time must not be before start_time")
    attendees = payload.get("attendees") or []
    if not isinstance(attendees, list) or len(attendees) > MAX_CALENDAR_ATTENDEES:
        problems.append(f"attendees must be a list of at most {MAX_CALENDAR_ATTENDEES} email addresses")
    elif not all(isinstance(a, str) and _EMAIL_ADDRESS_RE.match(a.strip()) for a in attendees):
        problems.append("an attendee is not a valid email address")
    return problems


def create_pending_action(
    db: Any,
    staff_user_id: str,
    tool_name: str,
    action_payload: Dict[str, Any],
    conversation_id: Optional[str] = None,
    expires_in_minutes: Optional[int] = None
) -> PendingAction:
    """
    Creates a new pending action staged for explicit employee/admin confirmation.
    Claude NEVER executes write actions directly; it only proposes them here.
    Validates data completeness for listing creations and updates.
    Persistence goes through pending_actions_repo (STORE_PENDING_ACTIONS).
    """
    if tool_name in ("send_email", "create_calendar_event"):
        problems = (
            validate_email_action(action_payload or {})
            if tool_name == "send_email"
            else validate_calendar_action(action_payload or {})
        )
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot stage this action: " + "; ".join(problems) + ".",
            )
    if tool_name in ["create_or_update_listing", "propose_listing_update"]:
        from app.services.admin_property_fields import resolve_existing_property
        src, rec = resolve_existing_property(db, action_payload.get("property_id"))
        if src:
            action_payload = dict(action_payload)
            action_payload["mode"] = "update"
            action_payload["is_update_only"] = True
            action_payload["resolve_source"] = src
        is_valid, missing, err_msg = validate_property_data(
            action_payload, existing_property=rec if src else None
        )
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=err_msg
            )

    # Supersede older PENDING actions for the same property (same conversation preferred)
    prop_id = (
        (action_payload or {}).get("property_id")
        or (action_payload or {}).get("target_property_id")
        or ((action_payload or {}).get("change_diff") or {}).get("property_id")
    )
    if prop_id:
        try:
            for old in pending_actions_repo.list_pending_for_property(
                db, property_id=str(prop_id), conversation_id=conversation_id
            ):
                pending_actions_repo.update_fields(
                    db,
                    old.id,
                    status=ACTION_STATUS_SUPERSEDED,
                    error=f"Superseded by newer proposal for property {prop_id}",
                )
        except Exception as e:
            logger.warning(f"Could not supersede older pending actions: {e}")

    action = pending_actions_repo.create(
        db,
        staff_email=staff_user_id,
        tool_name=tool_name,
        action_payload=action_payload,
        conversation_id=conversation_id,
        expires_in_minutes=expires_in_minutes,
    )

    # Record initial audit entry — if this fails, roll back the pending row
    from app.services.audit_service import AuditPersistenceError, AUDIT_WRITE_FAILED_MSG
    try:
        record_audit_log(
            db=db,
            staff_user_id=staff_user_id,
            tool_name=tool_name,
            proposed_payload=action_payload,
            confirmed_by_staff=False,
            status=ACTION_STATUS_PENDING,
            conversation_id=conversation_id,
            pending_action_id=str(action.id),
            raise_on_failure=True,
        )
    except AuditPersistenceError:
        try:
            pending_actions_repo.delete(db, str(action.id))
        except Exception as cleanup_exc:
            logger.warning(f"Could not remove pending action after audit failure: {cleanup_exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=AUDIT_WRITE_FAILED_MSG,
        )

    return action


def staff_owns_action(action: Any, staff: Any) -> bool:
    """True when the proposer of the pending action is this staff member (matched by email or user id)."""
    owner = str(getattr(action, "staff_user_id", "") or "").strip().lower()
    if not owner:
        return False
    mine = {
        str(getattr(staff, "email", "") or "").strip().lower(),
        str(getattr(staff, "user_id", "") or "").strip().lower(),
    }
    mine.discard("")
    return owner in mine


def get_pending_action(db: Any, pending_action_id: str) -> PendingAction:
    """Retrieves a pending action and lazily marks it as EXPIRED if the timeout elapsed."""
    return pending_actions_repo.get(db, pending_action_id)


def _append_action_lifecycle_note(
    db: Any,
    *,
    conversation_id: Optional[str],
    pending_action_id: str,
    status: str,
    tool_name: str,
    result_data: Optional[Dict[str, Any]] = None,
    verification_line: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """
    Append a system/assistant note to Firestore conversation history so the chat
    model sees UI button confirms/rejects (not only free-text Confirm).
    """
    if not conversation_id:
        return
    try:
        from app.services.admin_property_fields import format_price_thb
        from app.services.firestore_service import get_firestore_client

        result_data = result_data or {}
        detail = ""
        updated = result_data.get("updated_fields") or {}
        if "price_numeric_min" in updated or "price_display" in updated:
            new_p = updated.get("price_numeric_min") or updated.get("price_display")
            detail = f" price → {format_price_thb(new_p)}"
        elif updated:
            detail = " fields=" + ",".join(list(updated.keys())[:5])

        if status.upper() in ("CONFIRMED", "EXECUTED"):
            text = (
                f"[SYSTEM] Action `{pending_action_id}` was CONFIRMED and executed "
                f"({tool_name}){detail}."
            )
            if verification_line:
                text += f" {verification_line}"
        else:
            text = (
                f"[SYSTEM] Action `{pending_action_id}` was REJECTED ({tool_name}). "
                f"{reason or 'No changes applied.'}"
            )

        client = get_firestore_client(target="admin")
        if client is None:
            return
        ref = client.collection("conversations").document(str(conversation_id))
        try:
            snap = ref.get()
            data = snap.to_dict() if getattr(snap, "exists", False) else {}
        except Exception:
            data = {}
        data = dict(data or {})
        history = list(data.get("messageHistory") or data.get("message_history") or [])
        history.append({
            "role": "assistant",
            "content": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "system_note": True,
            "pending_action_id": pending_action_id,
            "action_lifecycle": status.upper(),
        })
        if len(history) > 400:
            history = history[-400:]
        prefs = dict(data.get("preferences") or {})
        if status.upper() in ("CONFIRMED", "EXECUTED", "REJECTED"):
            if prefs.get("last_pending_action_id") == str(pending_action_id):
                prefs.pop("last_pending_action_id", None)
            prefs["last_finished_action_id"] = str(pending_action_id)
            prefs["last_finished_action_status"] = status.upper()
        ref.set(
            {
                "messageHistory": history,
                "preferences": prefs,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            merge=True,
        )
    except Exception as e:
        logger.warning(f"_append_action_lifecycle_note failed: {e}")


def _action_target_websites(payload: Optional[Dict[str, Any]]) -> list:
    from app.core.tenant import normalize_website_list

    raw = []
    if not isinstance(payload, dict):
        return []
    for key in ("websites", "selected_websites", "website", "site_origin"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            raw.append(value)
        elif isinstance(value, list):
            raw.extend(value)
    fields = payload.get("fields_to_update") or payload.get("fields") or {}
    if isinstance(fields, dict):
        sites = fields.get("websites")
        if isinstance(sites, str) and sites.strip():
            raw.append(sites)
        elif isinstance(sites, list):
            raw.extend(sites)
    return normalize_website_list(raw)


def _assert_confirm_authorized(action, staff: AuthenticatedStaff, confirmation_token: Optional[str]) -> None:
    """
    Ownership first, then token, then website access.

    Anyone but the proposer gets 403 before any token check. A missing or wrong
    token returns 400 for the proposer.
    """
    role = (getattr(staff, "role", None) or "").strip().lower()
    # Only the staff member who proposed the action decides it (a super admin included).
    if not staff_owns_action(action, staff):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the staff member who proposed this action can confirm or reject it.",
        )

    token = (confirmation_token or "").strip()
    expected = (getattr(action, "confirmation_token", None) or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Confirmation token is required. Nothing was executed.",
        )
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid confirmation token. Nothing was executed.",
        )

    if role == "super_admin":
        return

    sites = _action_target_websites(getattr(action, "action_payload", None) or {})
    if not sites:
        return
    from app.core.tenant import normalize_website_list

    allowed = set(normalize_website_list(getattr(staff, "allowed_websites", None) or []))
    denied = [site for site in sites if site not in allowed]
    if denied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot confirm an action for website(s) outside your access: {denied}.",
        )


def confirm_and_execute_action(
    db: Any,
    pending_action_id: str,
    authenticated_staff: AuthenticatedStaff,
    confirmation_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Validates confirmation token, authenticated role, and expiration, then executes the action.
    Updates lifecycle states: PENDING -> CONFIRMED -> EXECUTED (or FAILED).
    """
    action = get_pending_action(db, pending_action_id)
    _assert_confirm_authorized(action, authenticated_staff, confirmation_token)

    from app.core.permissions import tool_denial
    denial = tool_denial(getattr(authenticated_staff, "role", None), action.tool_name)
    if denial:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denial)

    # Already executed: return the first result and do not run the tool again.
    if action.status == ACTION_STATUS_EXECUTED:
        stored = action.execution_result if isinstance(action.execution_result, dict) else {}
        return {
            "status": ACTION_STATUS_EXECUTED,
            "pending_action_id": str(action.id),
            "tool_name": action.tool_name,
            "execution_result": stored,
            "operation_id": stored.get("operation_id"),
            "idempotent": True,
            "message": "This action was already confirmed. Returning the original result.",
        }

    # 1. Check expiration
    if action.status == ACTION_STATUS_EXPIRED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pending action has expired. A new action must be proposed."
        )

    # 2. Check status
    if action.status != ACTION_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action cannot be executed. Current status is '{action.status}'."
        )

    # Token and proposer checks already ran in _assert_confirm_authorized.

    # 3b. Stale-value guard: re-check stored "Current" vs live record before any write
    payload = dict(action.action_payload or {})
    change_diff = payload.get("change_diff")
    if isinstance(change_diff, dict) and (
        change_diff.get("rows") or change_diff.get("items")
    ):
        from app.services.admin_change_diff import (
            verify_diff_still_current,
            build_single_listing_change_diff,
            build_batch_change_diff,
        )

        ok, stale_list = verify_diff_still_current(db, change_diff)
        if not ok:
            # Supersede old pending (not a silent loop of REJECTED twins)
            stale_msg = (
                "Stale values detected; refused apply. Superseded with one fresh proposal. "
                + "; ".join(stale_list[:8])
            )
            pending_actions_repo.update_fields(
                db,
                str(action.id),
                status=ACTION_STATUS_SUPERSEDED,
                error=stale_msg,
            )
            action.status = ACTION_STATUS_SUPERSEDED
            action.error_message = stale_msg
            record_audit_log(
                db=db,
                staff_user_id=authenticated_staff.email,
                tool_name=action.tool_name,
                proposed_payload=payload,
                confirmed_by_staff=True,
                status="SUPERSEDED",
                error_message=stale_msg,
                conversation_id=action.conversation_id,
                pending_action_id=str(action.id),
            )
            # Re-propose exactly ONE fresh pending action from live store
            fresh_diff: Dict[str, Any] = {}
            new_payload = dict(payload)
            try:
                if action.tool_name in ("create_or_update_listing", "ingest_property_from_drive"):
                    fields = (
                        (change_diff.get("canonical_fields") if isinstance(change_diff, dict) else None)
                        or payload.get("fields_to_update")
                        or payload.get("fields")
                        or {}
                    )
                    # Drop injected websites on re-propose unless originally explicit
                    if "websites" in fields and "websites" not in (
                        (payload.get("explicit_fields") or [])
                    ):
                        # Keep websites only if admin put it in original fields_to_update keys intentionally
                        orig = payload.get("fields_to_update") or payload.get("fields") or {}
                        if "websites" not in orig and "selected_websites" not in orig:
                            fields = {k: v for k, v in fields.items() if k != "websites"}
                    mode = payload.get("mode") or "update"
                    fresh_diff = build_single_listing_change_diff(
                        db=db,
                        property_id=str(payload.get("property_id") or ""),
                        proposed_fields=fields,
                        mode=mode,
                    )
                    if fresh_diff.get("canonical_fields"):
                        new_payload["fields_to_update"] = fresh_diff["canonical_fields"]
                elif action.tool_name == "execute_batch_property_action":
                    fresh_diff = build_batch_change_diff(
                        db=db,
                        operation_type=payload.get("operation_type") or "update",
                        items=payload.get("items") or [],
                        update_fields=payload.get("update_fields"),
                    )
            except Exception as rebuild_exc:
                logger.warning(f"Failed to rebuild change_diff after stale refuse: {rebuild_exc}")
                fresh_diff = {}

            new_action = None
            if fresh_diff:
                new_payload["change_diff"] = fresh_diff
                new_payload["diff_markdown"] = fresh_diff.get("diff_markdown")
                new_payload["risk_warnings"] = fresh_diff.get("risk_warnings") or []
                if fresh_diff.get("requires_explicit_delete_ack") is not None:
                    new_payload["requires_explicit_delete_ack"] = fresh_diff[
                        "requires_explicit_delete_ack"
                    ]
                    new_payload["delete_count"] = fresh_diff.get("delete_count") or 0
                new_action = create_pending_action(
                    db=db,
                    staff_user_id=authenticated_staff.email,
                    tool_name=action.tool_name,
                    action_payload=new_payload,
                    conversation_id=action.conversation_id,
                )

            real_new_id = str(new_action.id) if new_action else None
            msg = (
                "Values changed since this proposal. Nothing was applied. "
                "A fresh proposal was staged — review the new Current values and confirm again."
            )
            if real_new_id:
                msg += f" New pending action ID: `{real_new_id}`."

            return {
                "status": "STALE_REFUSED",
                "stale_fields": stale_list,
                "pending_action_id": real_new_id,
                "confirmation_token": new_action.confirmation_token if new_action else None,
                "change_diff": fresh_diff or None,
                "diff_table": (fresh_diff or {}).get("diff_markdown"),
                "message": msg,
                "previous_pending_action_id": str(action.id),
                "action_status": "awaiting_confirmation" if new_action else "superseded",
            }

    # 4. Persist confirm audit BEFORE any data mutation. If audit cannot be written,
    # stop here so nothing changes.
    from app.services.audit_service import AuditPersistenceError, AUDIT_WRITE_FAILED_MSG

    audit_payload_pre = dict(action.action_payload or {})
    try:
        audit = record_audit_log(
            db=db,
            staff_user_id=authenticated_staff.email,
            tool_name=action.tool_name,
            proposed_payload=audit_payload_pre,
            confirmed_by_staff=True,
            execution_result=None,
            status="CONFIRMED",
            conversation_id=action.conversation_id,
            pending_action_id=str(action.id),
            raise_on_failure=True,
        )
    except AuditPersistenceError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=AUDIT_WRITE_FAILED_MSG,
        )

    # 5. Atomic claim PENDING → CONFIRMED (double-confirm races execute once)
    # Keep a handle to the originally-fetched object so unit tests that reuse the
    # same in-memory action see EXECUTED on a second confirm.
    pre_claim_action = action
    claimed = pending_actions_repo.claim_confirm(
        db,
        str(action.id),
        staff_email=authenticated_staff.email,
        known_action=action,
    )
    if claimed.status == ACTION_STATUS_EXECUTED:
        stored = claimed.execution_result if isinstance(claimed.execution_result, dict) else {}
        return {
            "status": ACTION_STATUS_EXECUTED,
            "pending_action_id": str(claimed.id),
            "tool_name": claimed.tool_name,
            "execution_result": stored,
            "operation_id": stored.get("operation_id") if isinstance(stored, dict) else None,
            "idempotent": True,
            "message": "This action was already confirmed. Returning the original result.",
        }
    if not getattr(claimed, "claim_won", False):
        # Another tab/request already claimed — re-read; return idempotent if done
        refreshed = get_pending_action(db, str(action.id))
        if refreshed.status == ACTION_STATUS_EXECUTED:
            stored = refreshed.execution_result if isinstance(refreshed.execution_result, dict) else {}
            return {
                "status": ACTION_STATUS_EXECUTED,
                "pending_action_id": str(refreshed.id),
                "tool_name": refreshed.tool_name,
                "execution_result": stored,
                "operation_id": stored.get("operation_id") if isinstance(stored, dict) else None,
                "idempotent": True,
                "message": "This action was already confirmed. Returning the original result.",
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Action cannot be executed. Current status is '{refreshed.status}'.",
        )
    action = claimed

    # 6. Execute action payload based on tool_name (only previewed fields/items)
    result_data: Dict[str, Any] = {}
    try:
        if action.tool_name == "ingest_property_from_url":
            result_data = save_ingested_listing(db, authenticated_staff, payload)
        elif action.tool_name in ["create_or_update_listing", "ingest_property_from_drive"]:
            result_data = _execute_update_listing(
                db,
                payload,
                authenticated_staff=authenticated_staff,
                conversation_id=getattr(action, "conversation_id", None),
                pending_action_id=str(action.id),
            )
        elif action.tool_name == "archive_listing":
            result_data = _execute_archive_listing(db, payload, authenticated_staff)
        elif action.tool_name == "undo_operation":
            result_data = _execute_undo_operation(db, payload, authenticated_staff)
        elif action.tool_name == "bulk_create_from_document":
            from app.services.admin_offer_import import execute_document_import

            result_data = execute_document_import(db, payload, authenticated_staff)
        elif action.tool_name == "send_email":
            result_data = _execute_send_email(
                payload,
                staff_user_email=authenticated_staff.email if authenticated_staff else None,
                db=db
            )
            result_data["undoable"] = False
        elif action.tool_name == "create_calendar_event":
            result_data = _execute_create_calendar_event(
                payload,
                staff_user_email=authenticated_staff.email if authenticated_staff else None,
                db=db
            )
            result_data["undoable"] = False
        elif action.tool_name == "execute_batch_property_action":
            result_data = _execute_batch_property_action(
                db, payload, authenticated_staff
            )
        elif action.tool_name == "manage_property_images":
            from app.services.admin_image_commands import execute_property_image_change

            result_data = execute_property_image_change(db, payload, authenticated_staff)
        elif action.tool_name == "approve_drive_ingestion":
            from app.services.drive_folder_ingest import approve_drafts
            result_data = approve_drafts(
                db, authenticated_staff, str(payload.get("job_id") or ""),
                list(payload.get("draft_ids") or []), confirm=True,
            )
        else:
            raise ValueError(f"Unsupported tool action: {action.tool_name}")

        # Persist operation_id on the pending action so Data history / Activity logs can link undo
        operation_id = None
        if isinstance(result_data, dict):
            operation_id = result_data.get("operation_id")
            if operation_id:
                merged_payload = dict(action.action_payload or {})
                merged_payload["operation_id"] = operation_id
                action.action_payload = merged_payload

        # 7. Mark executed via repo
        now_ex = datetime.now(timezone.utc)
        action = pending_actions_repo.update_fields(
            db,
            str(action.id),
            status=ACTION_STATUS_EXECUTED,
            result=result_data,
            executed_at=now_ex,
            action_payload=action.action_payload,
        ) or action
        action.status = ACTION_STATUS_EXECUTED
        action.execution_result = result_data
        action.executed_at = now_ex
        # Sync the pre-claim object (shared SimpleNamespace/ORM in unit tests)
        if pre_claim_action is not None and pre_claim_action is not action:
            try:
                pre_claim_action.status = ACTION_STATUS_EXECUTED
                pre_claim_action.execution_result = result_data
                pre_claim_action.executed_at = now_ex
            except Exception:
                pass

        # Enrich the pre-write audit row with execution outcome (best-effort)
        if (
            audit is not None
            and not getattr(audit, "is_ephemeral", False)
            and not getattr(audit, "audit_write_failed", False)
            and getattr(audit, "id", None)
            and not str(audit.id).startswith("audit_")
        ):
            try:
                audit_payload = dict(action.action_payload or {})
                if action.tool_name == "send_email":
                    audit_payload = {
                        **audit_payload,
                        "to": (action.action_payload or {}).get("to"),
                        "subject": (action.action_payload or {}).get("subject"),
                        "undoable": False,
                        "operation_id": operation_id,
                    }
                elif action.tool_name == "create_calendar_event":
                    audit_payload = {
                        **audit_payload,
                        "title": (action.action_payload or {}).get("title"),
                        "start_time": (action.action_payload or {}).get("start_time"),
                        "attendees": (action.action_payload or {}).get("attendees"),
                        "undoable": False,
                        "operation_id": operation_id,
                    }
                elif operation_id:
                    audit_payload["operation_id"] = operation_id
                from app.repositories import audit_repo
                audit_repo.update_fields(
                    db,
                    str(audit.id),
                    proposed_payload=sanitize_payload(audit_payload),
                    tool_arguments={"operation_id": operation_id} if operation_id else None,
                    execution_result=str(sanitize_payload(result_data)),
                    status="SUCCESS",
                )
            except Exception as enrich_exc:
                logger.warning(f"Could not enrich confirm audit row: {enrich_exc}")

        from app.core.config import settings as _settings
        audit_disabled = bool(
            getattr(audit, "audit_logging_disabled", False)
            or getattr(audit, "is_ephemeral", False)
            or not getattr(_settings, "ENABLE_AUDIT_LOGGING", False)
        )
        audit_id = None if audit_disabled else str(getattr(audit, "id", "") or "") or None

        # Read-back verification line for listing updates
        verification_line = None
        if isinstance(result_data, dict):
            verified = result_data.get("verified_fields") or {}
            if not verified and result_data.get("updated_fields"):
                try:
                    from app.services.admin_change_diff import read_stored_property_fields
                    from app.services.admin_property_fields import format_price_thb
                    live = read_stored_property_fields(
                        db, str(result_data.get("property_id") or ""), list(result_data["updated_fields"].keys())
                    )
                    bits = []
                    for k, expected in (result_data.get("updated_fields") or {}).items():
                        actual = live.get(k)
                        if k.startswith("price"):
                            bits.append(f"{k}={format_price_thb(actual)}")
                        else:
                            bits.append(f"{k}={actual}")
                    if bits:
                        verification_line = "Verified in database: " + ", ".join(bits[:4])
                except Exception:
                    verification_line = None
            elif verified:
                from app.services.admin_property_fields import format_price_thb
                bits = []
                for k, actual in verified.items():
                    if k.startswith("price"):
                        bits.append(f"{k}={format_price_thb(actual)}")
                    else:
                        bits.append(f"{k}={actual}")
                if bits:
                    verification_line = "Verified in database: " + ", ".join(bits[:4])

        # Append system note to conversation so the chat model sees button confirms
        try:
            _append_action_lifecycle_note(
                db,
                conversation_id=action.conversation_id,
                pending_action_id=str(action.id),
                status="CONFIRMED",
                tool_name=action.tool_name,
                result_data=result_data if isinstance(result_data, dict) else {},
                verification_line=verification_line,
            )
        except Exception as note_exc:
            logger.warning(f"Failed to append confirm history note: {note_exc}")

        return {
            "status": ACTION_STATUS_EXECUTED,
            "pending_action_id": str(action.id),
            "tool_name": action.tool_name,
            "execution_result": result_data,
            "operation_id": operation_id,
            "audit_log_id": audit_id,
            "audit_logging_disabled": audit_disabled,
            "audit_logging_note": "Audit logging is disabled" if audit_disabled else None,
            "verification_line": verification_line,
            "confirmed_by": authenticated_staff.email,
        }

    except Exception as exc:
        safe_error = scrub_text(str(exc))
        pending_actions_repo.update_fields(
            db,
            str(action.id),
            status=ACTION_STATUS_FAILED,
            error=safe_error,
        )
        action.status = ACTION_STATUS_FAILED
        action.error_message = safe_error

        try:
            record_audit_log(
                db=db,
                staff_user_id=authenticated_staff.email,
                tool_name=action.tool_name,
                proposed_payload=action.action_payload,
                confirmed_by_staff=True,
                status="FAILED",
                error_message=safe_error,
                conversation_id=action.conversation_id,
                pending_action_id=str(action.id),
                raise_on_failure=False,
            )
        except Exception as aud_exc:
            logger.warning(f"Could not record FAILED audit for {action.id}: {aud_exc}")
        logger.error(f"Failed executing pending action {action.id}: {safe_error}")
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if exc.__class__.__name__ in ("BatchOperationError", "OperationNotFound", "IngestionError")
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=f"Action execution failed: {safe_error}"
        )


def reject_pending_action(
    db: Any,
    pending_action_id: str,
    authenticated_staff: AuthenticatedStaff,
    reason: Optional[str] = None
) -> Dict[str, Any]:
    """Rejects a pending action and records the cancellation in audit logs."""
    action = get_pending_action(db, pending_action_id)
    if not staff_owns_action(action, authenticated_staff):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the staff member who proposed this action can confirm or reject it.",
        )
    action = pending_actions_repo.claim_reject(
        db,
        pending_action_id,
        staff_email=authenticated_staff.email,
        reason=reason,
    )
    if action.status != ACTION_STATUS_REJECTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action cannot be rejected. Current status is '{action.status}'.",
        )

    record_audit_log(
        db=db,
        staff_user_id=authenticated_staff.email,
        tool_name=action.tool_name,
        proposed_payload=action.action_payload,
        confirmed_by_staff=False,
        status="REJECTED",
        error_message=reason or "Rejected by staff.",
        conversation_id=action.conversation_id,
        pending_action_id=str(action.id)
    )
    try:
        _append_action_lifecycle_note(
            db,
            conversation_id=action.conversation_id,
            pending_action_id=str(action.id),
            status="REJECTED",
            tool_name=action.tool_name,
            reason=reason or "Rejected by staff.",
        )
    except Exception as note_exc:
        logger.warning(f"Failed to append reject history note: {note_exc}")

    return {
        "status": ACTION_STATUS_REJECTED,
        "pending_action_id": str(action.id),
        "reason": reason or "Rejected by staff."
    }


def _execute_archive_listing(db: Any, payload: Dict[str, Any], staff: AuthenticatedStaff) -> Dict[str, Any]:
    """Archive (soft delete) one listing through the same path as DELETE /admin/properties/{id}: both stores, undo snapshot, audit."""
    from app.services.admin_property_fields import resolve_existing_property
    from app.services.property_search import property_belongs_to_websites

    property_id = str(payload.get("property_id") or "").strip()
    if not property_id:
        raise ValueError("Missing property_id in the archive action.")
    source, record = resolve_existing_property(db, property_id)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Property with ID '{property_id}' not found.")
    allowed = list(getattr(staff, "allowed_websites", None) or [])
    if isinstance(staff, AuthenticatedStaff) and not property_belongs_to_websites(record, allowed):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: this listing belongs to a website your account cannot reach.")
    result = rest_delete_property(db, staff, property_id, hard_delete=False)
    result["title"] = payload.get("property_name") or property_id
    return result


def _execute_undo_operation(db: Any, payload: Dict[str, Any], staff: AuthenticatedStaff) -> Dict[str, Any]:
    """Undo one operation from its stored snapshot (once, inside the 6-day window, for an account that may see it)."""
    from app.services.admin_batch_service import undo_batch_property_operation

    operation_id = str(payload.get("operation_id") or "").strip()
    if not operation_id:
        raise ValueError("Missing operation_id in the undo action.")
    result = undo_batch_property_operation(operation_id, staff, db_session=db)
    if result.get("restored_ok") is False:
        raise RuntimeError(f"The undo did not restore the previous state exactly: {result}")
    return result


def _execute_batch_property_action(
    db: Any,
    payload: Dict[str, Any],
    authenticated_staff: AuthenticatedStaff,
) -> Dict[str, Any]:
    """
    Applies a previously proposed batch property action. Only invoked after explicit
    staff confirmation (pending action id + confirming user).
    """
    from app.services.admin_batch_service import execute_batch_property_operation

    op_type = (payload.get("operation_type") or payload.get("action_type") or "update").lower()
    records = payload.get("records") or []
    if not records and payload.get("items"):
        records = []
        for it in payload["items"]:
            row = {
                "property_id": it.get("property_id"),
                "name": it.get("name") or it.get("property_id"),
            }
            fields = (it.get("change") or {}).get("fields") or payload.get("update_fields") or {}
            row.update(fields)
            records.append(row)
    # Confirm applies only what was previewed in change_diff
    change_diff = payload.get("change_diff") or {}
    preview_items = change_diff.get("items") or []
    if preview_items:
        allowed_ids = {str(it.get("property_id")) for it in preview_items if it.get("property_id")}
        records = [r for r in records if str(r.get("property_id")) in allowed_ids]
        # For non-delete, restrict fields to those shown in each item's rows
        if op_type not in ("delete", "create", "write"):
            field_map = {
                str(it.get("property_id")): {r["field"] for r in (it.get("rows") or [])}
                for it in preview_items
            }
            trimmed = []
            for r in records:
                pid = str(r.get("property_id"))
                allowed_fields = field_map.get(pid)
                if not allowed_fields:
                    trimmed.append(r)
                    continue
                keep = {
                    k: v
                    for k, v in r.items()
                    if k in ("property_id", "name") or k in allowed_fields
                }
                trimmed.append(keep)
            records = trimmed
    if not records:
        raise ValueError("Batch pending action has no listed records/items to apply.")

    staff_for_batch = authenticated_staff
    if isinstance(authenticated_staff, AuthenticatedStaff):
        # The websites the admin had selected when uploading, limited to what the account still has
        allowed = list(authenticated_staff.allowed_websites or [])
        scope = [s for s in (payload.get("site_scope") or []) if s in allowed] if payload.get("site_scope") else allowed
        if not scope:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: your account has no website for this operation.")
        staff_for_batch = authenticated_staff.model_copy(update={"selected_websites": scope})

    return execute_batch_property_operation(
        action_type=op_type,
        records=records,
        staff_user=staff_for_batch,
        source_filename=payload.get("source_filename") or "pending_action_confirm",
        db_session=db,
        confirmed_by_staff=True,
    )


def _enforce_listing_scope(db, payload, fields, orig_fields, prop, resolved, resolve_source, staff) -> None:
    """Confirm-time guard for chat-driven listing changes (the account may have changed since the proposal).

    Runs for a real signed-in staff member only. Refuses a website outside the account, a listing that belongs to
    websites the account cannot reach, and a new or renamed listing that would duplicate another one.
    """
    if not isinstance(staff, AuthenticatedStaff):
        return
    from app.core.tenant import normalize_website_list
    from app.services.property_search import property_belongs_to_websites

    allowed = list(staff.allowed_websites or [])
    if "websites" in orig_fields or "selected_websites" in orig_fields or "websites" in payload:
        requested = orig_fields.get("websites") or orig_fields.get("selected_websites") or payload.get("websites") or fields.get("websites")
    else:
        requested = None
    requested = normalize_website_list(requested) if requested else []
    requested += [s for s in normalize_website_list(payload.get("site_scope") or []) if s not in requested]
    outside = [s for s in requested if s not in allowed]
    if outside:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: your account has no access to website(s) {outside}.",
        )
    existing = prop if prop is not None else (resolved if isinstance(resolved, dict) else None)
    if existing is not None and not property_belongs_to_websites(existing, allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: this listing belongs to a website your account cannot reach.",
        )
    new_name = fields.get("property_name")
    duplicate = None
    if existing is None and not resolve_source:
        duplicate = _duplicate_property_message(
            db, str(payload.get("property_id") or ""), str(new_name or payload.get("title") or "")
        )
    elif new_name:
        current_name = existing.get("property_name") if isinstance(existing, dict) else getattr(existing, "property_name", "")
        if str(new_name).strip() != str(current_name or "").strip():
            duplicate = _duplicate_property_message(db, "", str(new_name), exclude_id=str(payload.get("property_id") or ""))
    if duplicate:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=duplicate)
def _execute_update_listing(
    db: Any,
    payload: Dict[str, Any],
    authenticated_staff: Optional[AuthenticatedStaff] = None,
    conversation_id: Optional[str] = None,
    pending_action_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Safely creates or updates property listings in Cloud Firestore.
    Handles mode == 'create' (or missing property) and mode == 'update'.
    Saves a 6-day undo snapshot BEFORE writing when staff_user is provided (chat confirm path).
    """
    property_id = payload.get("property_id")
    fields = dict(payload.get("fields_to_update", {}) or payload.get("fields", {}) or {})
    mode = payload.get("mode", "update")
    if not property_id:
        raise ValueError("Missing required property_id in payload.")

    # Prefer canonical_fields from staged diff when present
    change_diff = payload.get("change_diff") or {}
    if isinstance(change_diff, dict) and change_diff.get("canonical_fields"):
        fields = dict(change_diff["canonical_fields"])

    # Confirm applies only fields that appeared in the staged Field|Current|New preview
    preview_rows = change_diff.get("rows") or []
    if preview_rows:
        allowed = {r.get("field") for r in preview_rows if r.get("field")}
        fields = {k: v for k, v in fields.items() if k in allowed}

    # Never apply websites on update unless explicitly present in original payload keys
    orig_fields = payload.get("fields_to_update") or payload.get("fields") or {}
    if "websites" not in orig_fields and "selected_websites" not in orig_fields and "websites" not in payload:
        fields.pop("websites", None)

    from app.services.admin_property_fields import (
        resolve_existing_property,
        coerce_price_storage,
        format_price_thb,
    )

    resolve_source, resolved = resolve_existing_property(db, property_id)
    if resolve_source == "firestore" and isinstance(resolved, dict):
        property_id = str(resolved.get("property_id") or resolved.get("_firestore_doc_id") or property_id)
        payload = dict(payload)
        payload["property_id"] = property_id
        payload["resolve_source"] = "firestore"
        payload["is_update_only"] = True
        payload["mode"] = "update"
        mode = "update"

    # Coerce price fields to int before write
    for pk in ("price_numeric_min", "price_numeric_max"):
        if pk in fields and fields[pk] is not None:
            c = coerce_price_storage(fields[pk])
            if c is not None:
                fields[pk] = c
                if pk == "price_numeric_min":
                    fields.setdefault("price_display", format_price_thb(c))

    validation_payload = dict(payload)
    if "fields_to_update" in validation_payload or "fields_to_update" in payload:
        validation_payload["fields_to_update"] = fields
    else:
        validation_payload["fields"] = fields
    if resolve_source:
        validation_payload["is_update_only"] = True
        validation_payload["mode"] = "update"
        validation_payload["resolve_source"] = resolve_source
    is_valid, missing, err_msg = validate_property_data(
        validation_payload, existing_property=resolved if resolve_source else None
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=err_msg
        )

    _enforce_listing_scope(db, payload, fields, orig_fields, None, resolved, resolve_source, authenticated_staff)

    allowed_fields = {
        "property_name", "project_name", "property_type", "status",
        "country", "province", "district", "subdistrict", "area", "address",
        "price_display", "price_numeric_min", "price_numeric_max", "currency", "price_note",
        "bedrooms_display", "bedrooms_min", "bedrooms_max", "bathrooms",
        "area_display", "area_sqm_min", "area_sqm_max", "usable_area_sqm", "land_area_sqm",
        "description", "has_pool", "beach_access", "sea_view",
        "features", "facilities", "amenities", "highlights", "developer_name", "ownership_type",
        "websites"
    }

    operation_id: Optional[str] = None

    # Snapshot BEFORE mutation (6-day undo window) — chat confirm path only
    previous_state = None  # record_crud_operation_snapshot reads the raw Firestore document
    # undo of a "create" deletes the document, so an existing listing must be snapshotted as update.
    existing_listing = resolve_source is not None
    snapshot_action = "create" if not existing_listing else ("delete" if mode == "delete" else "update")
    if authenticated_staff is not None:
        try:
            operation_id = record_crud_operation_snapshot(
                action_type=snapshot_action,
                property_id=property_id,
                previous_state=previous_state,
                staff_user=authenticated_staff,
                source="chat_confirm",
                created_id=property_id if snapshot_action == "create" else None,
                db_session=db,
                confirmed_by_staff=True,
                conversation_id=conversation_id or payload.get("conversation_id"),
                pending_action_id=pending_action_id or payload.get("pending_action_id"),
                new_values=fields,
            )
        except Exception as snap_exc:
            logger.warning(f"Failed to save CRUD undo snapshot before listing write: {snap_exc}")

    from app.core.tenant import normalize_website_list, get_current_env_domain

    # Websites only when explicitly requested
    explicit_websites = (
        "websites" in orig_fields
        or "selected_websites" in orig_fields
        or "websites" in payload
    )
    target_websites = fields.get("websites") if explicit_websites else None
    if explicit_websites and target_websites is None:
        target_websites = payload.get("websites") or payload.get("selected_websites")
    norm_websites = normalize_website_list(target_websites) if target_websites else None

    if not existing_listing:
        # Brand-new listing goes straight to Firestore.
        prop_name = str(fields.get("property_name") or payload.get("title") or property_id).strip()
        # The websites the admin had selected when asking; the environment default only when none were recorded
        ws = norm_websites or normalize_website_list(
            payload.get("site_scope") or [payload.get("site_origin") or get_current_env_domain()]
        )
        to_write = {k: v for k, v in fields.items() if k in allowed_fields and v is not None and k != "websites"}
        to_write["property_name"] = prop_name
        to_write["websites"] = ws
        to_write["is_public"] = True
        new_images = payload.get("images") or fields.get("images") or fields.get("image_urls") or []
        if isinstance(new_images, str):
            new_images = [i.strip() for i in new_images.split(",") if i.strip()]
        from app.services.firestore_service import save_or_update_property_firestore
        if not save_or_update_property_firestore(
            property_id=property_id,
            fields=to_write,
            images=new_images,
            cover_image=payload.get("cover_image") or fields.get("cover_image") or fields.get("coverImage"),
            target="admin",
        ):
            raise RuntimeError(f"Firestore create failed for '{property_id}'")
        from app.services.cache_service import search_cache
        search_cache.invalidate()
        _bump_properties_version()
        return {
            "property_id": property_id,
            "title": prop_name,
            "mode": "create",
            "updated_fields": {k: v for k, v in to_write.items() if k != "is_public"},
            "images_count": len(new_images),
            "images": new_images,
            "current_status": to_write.get("status"),
            "operation_id": operation_id,
            "undoable": bool(operation_id),
            "undo_window_days": 6 if operation_id else None,
            "resolve_source": "firestore",
            "success": True,
        }

    # Existing Firestore listing: update the document.
    is_created = False
    updated_fields = dict(fields)
    if getattr(settings, "ENABLE_FIRESTORE_SYNC", True) or True:
        try:
            from app.services.firestore_service import save_or_update_property_firestore
            save_or_update_property_firestore(
                property_id=property_id,
                fields=fields,
                images=payload.get("images") or fields.get("images") or [],
                cover_image=payload.get("cover_image") or fields.get("cover_image"),
                target="admin",
            )
        except Exception as e:
            logger.warning(f"Firestore-only listing update failed: {e}")
            raise
    # Verify read-back
    from app.services.admin_change_diff import read_stored_property_fields
    live = read_stored_property_fields(db, property_id, list(fields.keys()))
    verified = {}
    for k, v in fields.items():
        verified[k] = live.get(k)
    from app.services.cache_service import search_cache
    search_cache.invalidate()
    _bump_properties_version()
    title = (
        (resolved or {}).get("property_name")
        if isinstance(resolved, dict)
        else property_id
    )
    return {
        "property_id": property_id,
        "title": title,
        "mode": "update",
        "updated_fields": updated_fields,
        "images_count": 0,
        "images": [],
        "current_status": live.get("status") or (resolved or {}).get("status") if isinstance(resolved, dict) else None,
        "operation_id": operation_id,
        "undoable": bool(operation_id),
        "undo_window_days": 6 if operation_id else None,
        "resolve_source": "firestore",
        "verified_fields": verified,
        "firestore_synced": True,
        "success": True,
    }


def _execute_send_email(
    payload: Dict[str, Any],
    staff_user_email: Optional[str] = None,
    db: Optional[Any] = None
) -> Dict[str, Any]:
    """Executes email dispatch via Google Workspace Gmail API, SMTP, or local test dispatcher."""
    to_email = payload.get("to")
    subject = payload.get("subject")
    body = payload.get("body")
    html_body = payload.get("html_body")

    if not to_email or not subject or not body:
        raise ValueError("Email requires 'to', 'subject', and 'body'.")

    res = google_manager.send_email(
        to=to_email,
        subject=subject,
        body=body,
        html_body=html_body,
        staff_user_email=staff_user_email,
        db=db
    )
    delivered = bool(res.get("success")) and not bool(res.get("is_mock"))
    return {
        "delivered": delivered,
        "success": bool(res.get("success")) and delivered,
        "completed": delivered,
        "message_id": res.get("message_id"),
        "to": to_email,
        "subject": subject,
        "dispatched_at": res.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "provider": res.get("provider", "disconnected"),
        "is_mock": bool(res.get("is_mock", True)),
        "error": res.get("error"),
    }


def _execute_create_calendar_event(
    payload: Dict[str, Any],
    staff_user_email: Optional[str] = None,
    db: Optional[Any] = None
) -> Dict[str, Any]:
    """Executes calendar event creation via Google Calendar API or local test dispatcher."""
    title = payload.get("title")
    start_time = payload.get("start_time")
    end_time = payload.get("end_time")
    attendees = payload.get("attendees", [])
    description = payload.get("description")
    related_property = payload.get("related_property")

    if not title or not start_time:
        raise ValueError("Calendar event requires 'title' and 'start_time'.")

    res = google_manager.create_calendar_event(
        title=title,
        start_time=start_time,
        end_time=end_time,
        attendees=attendees,
        description=description,
        related_property=related_property,
        staff_user_email=staff_user_email,
        db=db
    )
    created = bool(res.get("success")) and not bool(res.get("is_mock"))
    return {
        "created": created,
        "success": created,
        "completed": created,
        "event_id": res.get("event_id"),
        "title": title,
        "start_time": start_time,
        "end_time": end_time or start_time,
        "attendees": attendees,
        "status": "confirmed" if created else "incomplete",
        "google_calendar_url": res.get("google_calendar_url"),
        "is_mock": bool(res.get("is_mock", True)),
        "error": res.get("error"),
    }


def name_slug(value: Optional[str]) -> str:
    """Lowercase hyphen slug of a property name (max 64)."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:64]


def generate_property_id(db: Any, name: Optional[str]) -> str:
    """
    Build a new property_id from the listing name. The admin never has to supply one.
    Appends -2, -3 ... when the slug is already used by a Firestore listing.
    """
    from app.services.firestore_service import get_firestore_client

    def _taken(pid: str) -> bool:
        # Exact document-id check only: the name resolver is fuzzy and would
        # treat "villa-2" as the same listing as "villa".
        for target in ("admin", "user"):
            try:
                client = get_firestore_client(target=target) or (get_firestore_client() if target == "admin" else None)
                if client and client.collection("projects").document(pid).get().exists:
                    return True
            except Exception:
                continue
        return False

    base = name_slug(name)
    if property_id_slug_error(base):
        import hashlib
        base = "prop-" + hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:8]
    candidate = base
    for n in range(2, 100):
        if not _taken(candidate):
            return candidate
        suffix = "-" + str(n)
        candidate = base[: 64 - len(suffix)].rstrip("-") + suffix
    return candidate


def _read_raw_firestore(property_id: str) -> Optional[Dict[str, Any]]:
    from app.services.firestore_service import get_raw_property_document
    return get_raw_property_document(property_id, target="admin")


def _fs_sync_enabled() -> bool:
    try:
        return bool(settings.get_admin_enable_firestore_sync())
    except Exception:
        return bool(getattr(settings, "ENABLE_FIRESTORE_SYNC", True))


def _bump_properties_version() -> None:
    try:
        from app.services.firestore_service import bump_properties_version
        bump_properties_version(target="admin")
    except Exception as e:
        logger.debug("bump_properties_version skipped: %s", e)


def _invalidate_search_cache() -> None:
    """Search results, Firestore catalog and name index (see firestore_service.invalidate_property_caches)."""
    try:
        from app.services.firestore_service import invalidate_property_caches
        invalidate_property_caches()
    except Exception as exc:
        logger.warning("Search cache invalidation failed: %s", exc)
    _bump_properties_version()


def _fs_display_name(raw: Dict[str, Any]) -> Optional[str]:
    name = raw.get("name") or raw.get("title")
    if isinstance(name, dict):
        name = name.get("en") or name.get("pl") or next((v for v in name.values() if v), None)
    return str(name) if name else None


def _summary_fs(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    price = raw.get("priceFrom")
    try:
        price_out = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_out = None
    websites = raw.get("websites") or raw.get("domains") or []
    return {
        "name": _fs_display_name(raw),
        "price": price_out,
        "status": raw.get("status"),
        "isActive": raw.get("isActive"),
        "websites": list(websites) if isinstance(websites, list) else websites,
        "property_type": raw.get("propertyType") or raw.get("property_type"),
    }


def _audit_rest(
    db: Any,
    staff: Any,
    operation: str,
    property_id: str,
    operation_id: Optional[str],
    stores: Dict[str, bool],
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
) -> str:
    """One audit row per REST mutation. Returns 'disabled' when logging is off."""
    if not getattr(settings, "ENABLE_AUDIT_LOGGING", False):
        return "disabled"
    actor = getattr(staff, "email", None) or getattr(staff, "user_id", None)
    log = record_audit_log(
        db=db,
        staff_user_id=actor,
        tool_name=f"admin_rest_{operation}",
        tool_arguments={
            "actor": actor,
            "property_id": property_id,
            "operation_id": operation_id,
            "stores": stores,
        },
        proposed_payload={
            "before": before,
            "after": after,
            "stores": stores,
            "operation_id": operation_id,
        },
        confirmed_by_staff=True,
        status="SUCCESS",
        execution_result=f"{operation} {property_id}",
    )
    if getattr(log, "audit_logging_disabled", False) or getattr(log, "is_ephemeral", False):
        return "disabled"
    return str(getattr(log, "id", "") or "") or "disabled"


def _require_undo_snapshot(
    *,
    db: Any,
    staff: Any,
    action: str,
    property_id: str,
    source: str,
    created_id: Optional[str] = None,
    allow_without_undo: bool = False,
    new_values: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    operation_id = record_crud_operation_snapshot(
        action_type=action,
        property_id=property_id,
        previous_state=None,
        staff_user=staff,
        source=source,
        created_id=created_id,
        db_session=db,
        confirmed_by_staff=True,
        skip_audit=True,
        new_values=new_values,
    )
    if not operation_id and not allow_without_undo:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Undo snapshot could not be saved. Retry, or pass allow_without_undo=true "
                "to write without an undo snapshot."
            ),
        )
    return operation_id


def _duplicate_property_message(db: Any, property_id: str, name: str, exclude_id: Optional[str] = None) -> Optional[str]:
    """Why this id/name would duplicate a listing, or None. `exclude_id` is the listing being renamed (it may match itself)."""
    slug = name_slug(name)
    seen_ids = []
    for candidate in (property_id, slug):
        if not candidate or candidate in seen_ids or candidate == exclude_id:
            continue
        seen_ids.append(candidate)
        raw = _read_raw_firestore(candidate)
        if raw is not None:
            return (
                f"Property '{property_id}' already exists in Firestore (document '{candidate}'). "
                "Use update instead of create."
            )
    try:
        from app.services.firestore_service import get_firestore_client
        client = get_firestore_client(target="admin")
        if client and slug:
            for doc in client.collection("projects").stream():
                data = doc.to_dict() or {}
                labels: List[str] = [str(doc.id)]
                for key in ("name", "title"):
                    val = data.get(key)
                    if isinstance(val, dict):
                        labels.extend(str(v) for v in val.values() if v)
                    elif val:
                        labels.append(str(val))
                if doc.id != exclude_id and (doc.id == property_id or any(name_slug(label) == slug for label in labels)):
                    return (
                        f"A property named '{name}' already exists in Firestore as '{doc.id}'. "
                        "Use update instead of create."
                    )
    except Exception as exc:
        logger.warning("Firestore duplicate scan failed: %s", exc)


    return None


def _raise_store_failure(
    *,
    message: str,
    fs_ok: bool,
    failed: str,
    property_id: str,
    operation_id: Optional[str],
) -> None:
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "message": message,
            "firestore": fs_ok,
            "failed": failed,
            "property_id": property_id,
            "operation_id": operation_id,
        },
    )


def rest_create_property(
    db: Any,
    staff: AuthenticatedStaff,
    fields: Dict[str, Any],
    *,
    property_id: str,
    websites: List[str],
    images: Optional[List[str]] = None,
    cover_image: Optional[str] = None,
    allow_without_undo: bool = False,
) -> Dict[str, Any]:
    """Validate, snapshot, then write the Firestore property document."""
    errors = validate_listing_values(
        fields, property_id=property_id, is_create=True, check_property_id=True
    )
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Validation failed", "fields": errors},
        )
    duplicate = _duplicate_property_message(db, property_id, str(fields.get("property_name") or ""))
    if duplicate:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=duplicate)

    operation_id = _require_undo_snapshot(
        db=db,
        staff=staff,
        action="create",
        property_id=property_id,
        source="admin_api_create",
        created_id=property_id,
        allow_without_undo=allow_without_undo,
        new_values=fields,
    )

    is_public = True if fields.get("is_public") is None else bool(fields.get("is_public"))
    try:
        from app.services.firestore_service import save_or_update_property_firestore
        wrote = save_or_update_property_firestore(
            property_id=property_id,
            fields={**fields, "websites": list(websites or []), "is_public": is_public},
            images=images or [],
            cover_image=cover_image,
            target="admin",
        )
        if not wrote:
            raise RuntimeError("Firestore write returned failure")
    except Exception as exc:
        _raise_store_failure(
            message=f"Firestore write failed. {exc}",
            fs_ok=False,
            failed="firestore",
            property_id=property_id,
            operation_id=operation_id,
        )

    _invalidate_search_cache()
    stores = {"firestore": True}
    after = {"firestore": _summary_fs(_read_raw_firestore(property_id))}
    audit = _audit_rest(db, staff, "create", property_id, operation_id, stores, None, after)
    return {
        "status": "CREATED",
        "message": f"Property '{property_id}' written to Firestore.",
        "property_id": property_id,
        "operation_id": operation_id,
        "undo_window_days": 6 if operation_id else None,
        "firestore": True,
        "stores": stores,
        "websites": list(websites or []),
        "audit": audit,
    }


def rest_update_property(
    db: Any,
    staff: AuthenticatedStaff,
    property_id: str,
    fields: Dict[str, Any],
    *,
    websites: Optional[List[str]] = None,
    images: Optional[List[str]] = None,
    cover_image: Optional[str] = None,
    allow_without_undo: bool = False,
) -> Dict[str, Any]:
    """Update the Firestore property document. Never invents a nameless document."""
    errors = validate_listing_values(fields, is_create=False)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Validation failed", "fields": errors},
        )
    raw = _read_raw_firestore(property_id)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found.",
        )
    if fields.get("property_name"):
        duplicate = _duplicate_property_message(db, "", str(fields["property_name"]), exclude_id=property_id)
        if duplicate:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=duplicate)
    before = {"firestore": _summary_fs(raw)}
    operation_id = _require_undo_snapshot(
        db=db,
        staff=staff,
        action="update",
        property_id=property_id,
        source="admin_api_update",
        allow_without_undo=allow_without_undo,
        new_values={**fields, **({"websites": websites} if websites is not None else {})},
    )

    try:
        from app.services.firestore_service import save_or_update_property_firestore
        fs_fields = dict(fields)
        if websites is not None:
            fs_fields["websites"] = websites
        wrote = save_or_update_property_firestore(
            property_id=property_id,
            fields=fs_fields,
            images=images,
            cover_image=cover_image,
            target="admin",
        )
        if not wrote:
            raise RuntimeError("Firestore update failed")
    except Exception as exc:
        _raise_store_failure(
            message=f"Firestore update failed. Snapshot kept for undo. {exc}",
            fs_ok=False,
            failed="firestore",
            property_id=property_id,
            operation_id=operation_id,
        )

    _invalidate_search_cache()
    stores = {"firestore": True}
    after = {"firestore": _summary_fs(_read_raw_firestore(property_id))}
    audit = _audit_rest(db, staff, "update", property_id, operation_id, stores, before, after)
    return {
        "status": "UPDATED",
        "message": f"Property '{property_id}' written to Firestore.",
        "property_id": property_id,
        "operation_id": operation_id,
        "undo_window_days": 6 if operation_id else None,
        "firestore": True,
        "stores": stores,
        "websites": websites if websites is not None else (raw.get("websites") or []),
        "audit": audit,
    }


def rest_delete_property(
    db: Any,
    staff: AuthenticatedStaff,
    property_id: str,
    *,
    hard_delete: bool = False,
    confirm_property_id: Optional[str] = None,
    allow_without_undo: bool = False,
) -> Dict[str, Any]:
    """Soft-delete by default. Hard delete is off unless ALLOW_HARD_DELETE is set, then needs a typed property id."""
    if hard_delete and not getattr(settings, "ALLOW_HARD_DELETE", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permanent deletes are disabled. DELETE archives the listing and can be undone.",
        )
    if hard_delete and confirm_property_id != property_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="hard_delete requires confirm_property_id equal to the property id",
        )
    raw = _read_raw_firestore(property_id)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found.",
        )
    if not hard_delete:
        from app.services.firestore_service import is_soft_deleted_record
        if is_soft_deleted_record(raw):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Property '{property_id}' is already archived. Use undo to restore it.",
            )
    before = {"firestore": _summary_fs(raw)}
    operation_id = _require_undo_snapshot(
        db=db,
        staff=staff,
        action="delete",
        property_id=property_id,
        source="admin_api_delete",
        allow_without_undo=allow_without_undo,
    )

    from app.services.firestore_service import delete_property_firestore
    try:
        fs_ok = bool(delete_property_firestore(
            property_id=property_id,
            hard_delete=hard_delete,
            staff_email=getattr(staff, "email", None),
            target="admin",
        ))
        if not fs_ok:
            raise RuntimeError("Firestore delete returned failure")
    except Exception as exc:
        _raise_store_failure(
            message=f"Firestore delete failed. Snapshot kept for undo. {exc}",
            fs_ok=False,
            failed="firestore",
            property_id=property_id,
            operation_id=operation_id,
        )

    _invalidate_search_cache()
    stores = {"firestore": fs_ok}
    after = {"firestore": _summary_fs(_read_raw_firestore(property_id))}
    audit = _audit_rest(db, staff, "delete", property_id, operation_id, stores, before, after)
    verb = "permanently deleted" if hard_delete else "archived"
    return {
        "status": "DELETED" if hard_delete else "ARCHIVED",
        "message": f"Property '{property_id}' {verb}.",
        "property_id": property_id,
        "operation_id": operation_id,
        "undo_window_days": 6 if operation_id else None,
        "hard_delete": hard_delete,
        "firestore": fs_ok,
        "stores": stores,
        "audit": audit,
    }


def save_ingested_listing(
    db: Any,
    staff: AuthenticatedStaff,
    draft: Dict[str, Any],
    *,
    audit_tool_name: str = "ingest_property_from_url",
) -> Dict[str, Any]:
    """
    Write one chat-confirmed URL draft through the REST create/update path.
    Provenance is stored on raw_metadata.ingest. The page HTML is not stored.
    """
    from app.core.tenant import normalize_website_list

    mode = "update" if draft.get("mode") == "update" else "create"
    property_id = str(draft.get("property_id") or "").strip()
    fields = dict(draft.get("fields") or {})
    fields.pop("location", None)
    websites = normalize_website_list(draft.get("websites") or [])
    allowed = normalize_website_list(getattr(staff, "allowed_websites", None) or [])
    if (getattr(staff, "role", "") or "").lower() != "super_admin":
        denied = [site for site in websites if allowed and site not in allowed]
        if denied or not websites:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Those websites are outside your allowed sites. Nothing was saved.",
            )
    if not websites:
        websites = normalize_website_list(getattr(staff, "selected_websites", None) or []) or allowed

    guessed = list(draft.get("guessed_fields") or [])
    provided = list(draft.get("admin_fields") or [])
    provenance = draft.get("provenance") if isinstance(draft.get("provenance"), dict) else {}
    ingest_meta = {
        "source_url": draft.get("source_url"),
        "fetched_at": draft.get("fetched_at"),
        "imported_by": getattr(staff, "email", None),
        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "provenance": provenance,
        "guessed_fields": guessed,
        "admin_fields": provided,
        "content_hash": draft.get("content_hash"),
    }
    if isinstance(draft.get("drive_ingestion"), dict):
        ingest_meta["drive_ingestion"] = draft["drive_ingestion"]
    existing_meta: Dict[str, Any] = {}
    if mode == "update":
        current = _read_raw_firestore(property_id)
        if isinstance(current, dict) and isinstance(current.get("raw_metadata"), dict):
            existing_meta = dict(current["raw_metadata"])
    existing_meta["ingest"] = ingest_meta
    fields["raw_metadata"] = existing_meta

    images = list(draft.get("images") or [])
    cover = draft.get("cover_image") or (images[0] if images else None)
    try:
        if mode == "update":
            result = rest_update_property(
                db, staff, property_id, fields,
                websites=websites, images=images, cover_image=cover,
            )
        else:
            result = rest_create_property(
                db, staff, fields,
                property_id=property_id, websites=websites,
                images=images, cover_image=cover,
            )
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        return {
            "saved": False,
            "message": f"{detail} Nothing was saved.",
            "audit": "disabled",
            "property_id": property_id,
        }

    stored = _read_raw_firestore(property_id)
    stored_name = _fs_display_name(stored) if stored else None
    if not stored_name:
        return {
            "saved": False,
            "message": "The database did not return the new listing after the write. Nothing was saved.",
            "audit": result.get("audit") or "disabled",
            "property_id": property_id,
        }

    audit_flag = result.get("audit") or "disabled"
    if getattr(settings, "ENABLE_AUDIT_LOGGING", False):
        actor = getattr(staff, "email", None)
        log = record_audit_log(
            db=db,
            staff_user_id=actor,
            tool_name=audit_tool_name,
            tool_arguments={
                "actor": actor,
                "property_id": property_id,
                "operation_id": result.get("operation_id"),
                "source_url": draft.get("source_url"),
                "fields_count": len([k for k, v in fields.items() if k != "raw_metadata" and v is not None]),
                "guessed_fields": guessed,
            },
            proposed_payload={
                "source_url": draft.get("source_url"),
                "guessed_fields": guessed,
                "admin_fields": provided,
                "operation_id": result.get("operation_id"),
            },
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"ingest {property_id}",
        )
        if getattr(log, "audit_logging_disabled", False) or getattr(log, "is_ephemeral", False):
            audit_flag = "disabled"
        else:
            audit_flag = str(getattr(log, "id", "") or "") or "disabled"
    else:
        audit_flag = "disabled"

    notes = []
    if guessed:
        from app.services.admin_chat_quality import humanise_field_list

        notes.append("guessed: " + humanise_field_list(guessed))
    if provided:
        from app.services.admin_chat_quality import humanise_field_list

        notes.append("provided by admin: " + humanise_field_list(provided))
    note = (". ".join(notes) + ". ") if notes else ""
    message = (
        f"Saved: {stored_name} ({property_id}) - verified in database. "
        f"{note}"
        f"Undo operation id: {result.get('operation_id')}. "
        "Next: open the property, generate a brochure, or paste another URL."
    )
    return {
        "saved": True,
        "message": message,
        "operation_id": result.get("operation_id"),
        "audit": audit_flag,
        "property_id": property_id,
        "firestore": result.get("firestore"),
    }
