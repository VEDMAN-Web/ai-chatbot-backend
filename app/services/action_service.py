import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple
from sqlalchemy.orm import Session
from fastapi import HTTPException, status
import logging

from app.db.models import PendingAction, Property
from app.core.config import settings
from app.core.auth import AuthenticatedStaff
from app.services.audit_service import record_audit_log
from app.services.google_services import google_manager

# Re-exported so tests can patch `app.services.action_service.record_crud_operation_snapshot`
from app.services.admin_batch_service import record_crud_operation_snapshot  # noqa: E402

logger = logging.getLogger(__name__)

# Lifecycle States
ACTION_STATUS_PENDING = "PENDING"
ACTION_STATUS_CONFIRMED = "CONFIRMED"
ACTION_STATUS_EXECUTED = "EXECUTED"
ACTION_STATUS_REJECTED = "REJECTED"
ACTION_STATUS_EXPIRED = "EXPIRED"
ACTION_STATUS_FAILED = "FAILED"
ACTION_STATUS_SUPERSEDED = "SUPERSEDED"


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
    Validates data completeness before adding or updating any property in PostgreSQL or Cloud Firestore.
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
        and str(payload.get("resolve_source") or "").lower() not in ("postgres", "firestore")
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


def create_pending_action(
    db: Session,
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
    """
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
    minutes = (
        expires_in_minutes
        if expires_in_minutes is not None
        else getattr(settings, "ADMIN_PENDING_ACTION_TTL_MIN", None)
        or settings.ACTION_EXPIRATION_MINUTES
    )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=minutes)
    confirmation_token = f"act_{secrets.token_hex(16)}"

    # Supersede older PENDING actions for the same property (same conversation preferred)
    prop_id = (
        (action_payload or {}).get("property_id")
        or (action_payload or {}).get("target_property_id")
        or ((action_payload or {}).get("change_diff") or {}).get("property_id")
    )
    if prop_id:
        try:
            q = db.query(PendingAction).filter(PendingAction.status == ACTION_STATUS_PENDING)
            if conversation_id:
                q = q.filter(PendingAction.conversation_id == str(conversation_id))
            for old in q.all():
                old_payload = old.action_payload or {}
                old_pid = (
                    old_payload.get("property_id")
                    or old_payload.get("target_property_id")
                    or ((old_payload.get("change_diff") or {}).get("property_id") if isinstance(old_payload.get("change_diff"), dict) else None)
                )
                if old_pid and str(old_pid).strip().lower() == str(prop_id).strip().lower():
                    old.status = ACTION_STATUS_SUPERSEDED
                    old.error_message = f"Superseded by newer proposal for property {prop_id}"
        except Exception as e:
            logger.warning(f"Could not supersede older pending actions: {e}")

    action = PendingAction(
        confirmation_token=confirmation_token,
        staff_user_id=staff_user_id,
        tool_name=tool_name,
        action_payload=action_payload,
        status=ACTION_STATUS_PENDING,
        conversation_id=conversation_id,
        expires_at=expires_at,
        created_at=now
    )

    db.add(action)
    db.commit()
    db.refresh(action)

    # Record initial audit entry
    record_audit_log(
        db=db,
        staff_user_id=staff_user_id,
        tool_name=tool_name,
        proposed_payload=action_payload,
        confirmed_by_staff=False,
        status=ACTION_STATUS_PENDING,
        conversation_id=conversation_id,
        pending_action_id=str(action.id)
    )

    return action


def get_pending_action(db: Session, pending_action_id: str) -> PendingAction:
    """Retrieves a pending action and lazily marks it as EXPIRED if the timeout elapsed."""
    import uuid
    action = None

    # 1. If ID starts with token prefix "act_", query by confirmation_token
    if isinstance(pending_action_id, str) and pending_action_id.startswith("act_"):
        try:
            action = db.query(PendingAction).filter(PendingAction.confirmation_token == pending_action_id).first()
        except Exception:
            action = None

    # 2. Try querying by primary key UUID (safely cast string to UUID object)
    if not action:
        target_uuid = None
        if isinstance(pending_action_id, uuid.UUID):
            target_uuid = pending_action_id
        elif isinstance(pending_action_id, str):
            clean_str = pending_action_id.replace("act_", "").strip()
            try:
                target_uuid = uuid.UUID(clean_str)
            except Exception:
                target_uuid = None

        if target_uuid:
            try:
                action = db.query(PendingAction).filter(PendingAction.id == target_uuid).first()
            except Exception:
                action = None

    # 3. Fallback: query by confirmation_token
    if not action and isinstance(pending_action_id, str):
        try:
            action = db.query(PendingAction).filter(PendingAction.confirmation_token == pending_action_id).first()
        except Exception:
            action = None

    if not action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pending action '{pending_action_id}' not found."
        )

    # Server-side expiration check
    if action.expires_at.tzinfo is None:
        is_expired = action.expires_at < datetime.now()
    else:
        is_expired = action.expires_at < datetime.now(timezone.utc)

    if action.status == ACTION_STATUS_PENDING and is_expired:
        action.status = ACTION_STATUS_EXPIRED
        db.commit()
        db.refresh(action)

    return action


def _append_action_lifecycle_note(
    db: Session,
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
    Append a system/assistant note to conversation history so the chat model
    sees UI button confirms/rejects (not only free-text Confirm).
    """
    if not conversation_id or db is None:
        return
    try:
        from app.db.models import Conversation
        from app.services.admin_property_fields import format_price_thb

        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if conv is None:
            # conversation_id may be stored as string UUID
            try:
                conv = db.query(Conversation).filter(Conversation.id == str(conversation_id)).first()
            except Exception:
                conv = None
        if conv is None:
            return

        result_data = result_data or {}
        detail = ""
        updated = result_data.get("updated_fields") or {}
        if "price_numeric_min" in updated or "price_display" in updated:
            new_p = updated.get("price_numeric_min") or updated.get("price_display")
            # Prefer change_diff snapshot if present on result
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

        history = list(getattr(conv, "message_history", None) or [])
        history.append({
            "role": "assistant",
            "content": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "system_note": True,
            "pending_action_id": pending_action_id,
            "action_lifecycle": status.upper(),
        })
        # Cap history growth
        if len(history) > 400:
            history = history[-400:]
        conv.message_history = history
        prefs = dict(getattr(conv, "preferences", None) or {})
        if status.upper() in ("CONFIRMED", "EXECUTED", "REJECTED"):
            # Clear stale pending id from session prefs
            if prefs.get("last_pending_action_id") == str(pending_action_id):
                prefs.pop("last_pending_action_id", None)
            prefs["last_finished_action_id"] = str(pending_action_id)
            prefs["last_finished_action_status"] = status.upper()
        conv.preferences = prefs
        db.commit()
    except Exception as e:
        logger.warning(f"_append_action_lifecycle_note failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass


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
    """Token is mandatory. Only the proposer or a super admin may confirm, and only on allowed websites."""
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

    role = (getattr(staff, "role", None) or "").strip().lower()
    actor = (getattr(staff, "email", None) or "").strip().lower()
    proposer = (getattr(action, "staff_user_id", None) or "").strip().lower()
    if role != "super_admin" and proposer and actor != proposer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the staff member who proposed this action, or a super admin, can confirm it.",
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
    db: Session,
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
            action.status = ACTION_STATUS_SUPERSEDED
            action.error_message = (
                "Stale values detected; refused apply. Superseded with one fresh proposal. "
                + "; ".join(stale_list[:8])
            )
            db.commit()
            record_audit_log(
                db=db,
                staff_user_id=authenticated_staff.email,
                tool_name=action.tool_name,
                proposed_payload=payload,
                confirmed_by_staff=True,
                status="SUPERSEDED",
                error_message=action.error_message,
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

    # 4. Mark confirmed
    action.status = ACTION_STATUS_CONFIRMED
    action.confirmed_at = datetime.now(timezone.utc)
    db.commit()

    # 5. Execute action payload based on tool_name (only previewed fields/items)
    result_data: Dict[str, Any] = {}
    try:
        if action.tool_name == "ingest_property_from_url":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reply 'confirm' in the chat to save this import. There is no Confirm button for a URL draft.",
            )
        if action.tool_name in ["create_or_update_listing", "ingest_property_from_drive"]:
            result_data = _execute_update_listing(
                db, payload, authenticated_staff=authenticated_staff
            )
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

        # 6. Mark executed
        action.status = ACTION_STATUS_EXECUTED
        action.executed_at = datetime.now(timezone.utc)
        action.execution_result = result_data
        db.commit()

        # 7. Record immutable audit log (include recipient/subject or event details for non-undoable)
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

        audit = record_audit_log(
            db=db,
            staff_user_id=authenticated_staff.email,
            tool_name=action.tool_name,
            proposed_payload=audit_payload,
            tool_arguments={"operation_id": operation_id} if operation_id else None,
            confirmed_by_staff=True,
            execution_result=str(result_data),
            status="SUCCESS",
            conversation_id=action.conversation_id,
            pending_action_id=str(action.id)
        )

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
        action.status = ACTION_STATUS_FAILED
        action.error_message = str(exc)
        db.commit()

        record_audit_log(
            db=db,
            staff_user_id=authenticated_staff.email,
            tool_name=action.tool_name,
            proposed_payload=action.action_payload,
            confirmed_by_staff=True,
            status="FAILED",
            error_message=str(exc),
            conversation_id=action.conversation_id,
            pending_action_id=str(action.id)
        )
        logger.error(f"Failed executing pending action {action.id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Action execution failed: {str(exc)}"
        )


def reject_pending_action(
    db: Session,
    pending_action_id: str,
    authenticated_staff: AuthenticatedStaff,
    reason: Optional[str] = None
) -> Dict[str, Any]:
    """Rejects a pending action and records the cancellation in audit logs."""
    action = get_pending_action(db, pending_action_id)

    if action.status != ACTION_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action cannot be rejected. Current status is '{action.status}'."
        )

    action.status = ACTION_STATUS_REJECTED
    action.error_message = reason or "Rejected by staff."
    db.commit()

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


def _execute_batch_property_action(
    db: Session,
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
        if op_type != "delete":
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

    return execute_batch_property_operation(
        action_type=op_type,
        records=records,
        staff_user=authenticated_staff,
        source_filename=payload.get("source_filename") or "pending_action_confirm",
        db_session=db,
        confirmed_by_staff=True,
    )


def _serialize_property_snapshot(prop: Any) -> Dict[str, Any]:
    """Capture a plain dict of listing fields for the 6-day undo snapshot."""
    if prop is None:
        return {}
    keys = (
        "property_id", "property_name", "project_name", "property_type", "status",
        "country", "province", "district", "subdistrict", "area", "address",
        "price_display", "price_numeric_min", "price_numeric_max", "currency", "price_note",
        "bedrooms_display", "bedrooms_min", "bedrooms_max", "bathrooms",
        "area_display", "area_sqm_min", "area_sqm_max", "usable_area_sqm", "land_area_sqm",
        "description", "has_pool", "beach_access", "sea_view",
        "features", "facilities", "amenities", "highlights", "developer_name", "ownership_type",
        "websites", "is_public",
    )
    out: Dict[str, Any] = {}
    for k in keys:
        if hasattr(prop, k):
            out[k] = getattr(prop, k, None)
    # Also keep common aliases used by Firestore restore
    if out.get("property_name"):
        out.setdefault("name", out["property_name"])
        out.setdefault("title", out["property_name"])
    return out


def _execute_update_listing(
    db: Session,
    payload: Dict[str, Any],
    authenticated_staff: Optional[AuthenticatedStaff] = None,
) -> Dict[str, Any]:
    """
    Safely creates or updates property listings in PostgreSQL via parameterized ORM.
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
    prop = resolved if resolve_source == "postgres" else (
        db.query(Property).filter(Property.property_id == property_id).first()
    )
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
        validation_payload, existing_property=resolved if resolve_source else prop
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=err_msg
        )

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

    updated_fields = {}
    is_created = False
    operation_id: Optional[str] = None

    # Snapshot BEFORE mutation (6-day undo window) — chat confirm path only
    previous_state = _serialize_property_snapshot(prop) if prop else None
    snapshot_action = "create" if not prop else ("delete" if mode == "delete" else "update")
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

    firestore_only = (resolve_source == "firestore" and prop is None)

    if firestore_only:
        # Firestore-only listing: write Firestore; do NOT invent an incomplete PG row
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
            "success": True,
        }
    elif not prop:
        # Create a new property (truly new id)
        is_created = True
        prop_name = fields.get("property_name") or payload.get("title") or property_id
        prop = Property(
            property_id=property_id,
            property_name=prop_name,
            is_public=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        if norm_websites:
            prop.websites = norm_websites
            updated_fields["websites"] = norm_websites
        else:
            default_site = payload.get("site_origin") or get_current_env_domain()
            prop.websites = normalize_website_list([default_site])
            updated_fields["websites"] = prop.websites

        for k, v in fields.items():
            if k in allowed_fields and hasattr(prop, k) and v is not None and k != "websites":
                setattr(prop, k, v)
                updated_fields[k] = v
        db.add(prop)
        db.commit()
        db.refresh(prop)
    else:
        # Update existing Postgres property — never inject websites
        if explicit_websites and norm_websites is not None:
            prop.websites = norm_websites
            updated_fields["websites"] = norm_websites

        for k, v in fields.items():
            if k in allowed_fields and hasattr(prop, k) and v is not None and k != "websites":
                setattr(prop, k, v)
                updated_fields[k] = v
        prop.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(prop)

    # 1. Process property images in PostgreSQL
    input_images = payload.get("images") or fields.get("images") or fields.get("image_urls") or []
    cover_image = payload.get("cover_image") or fields.get("cover_image") or fields.get("coverImage")
    if isinstance(input_images, str):
        input_images = [img.strip() for img in input_images.split(",") if img.strip()]

    if input_images:
        try:
            from app.db.models import PropertyImage
            for img_url in input_images:
                if img_url and str(img_url).strip():
                    clean_url = str(img_url).strip()
                    # Check if already attached to avoid duplicates
                    existing_img = db.query(PropertyImage).filter(
                        PropertyImage.property_id == prop.property_id,
                        PropertyImage.image_url == clean_url
                    ).first()
                    if not existing_img:
                        db.add(PropertyImage(
                            property_id=prop.property_id,
                            image_url=clean_url,
                            image_type="cover" if clean_url == cover_image else "exterior"
                        ))
            db.commit()
        except Exception as e:
            logger.warning(f"Error persisting property images in SQL: {e}")

    # 2. Sync property info and images directly to Cloud Firestore
    if getattr(settings, "ENABLE_FIRESTORE_SYNC", True):
        try:
            from app.services.firestore_service import save_or_update_property_firestore
            mirror = {**fields, **updated_fields}
            if prop is not None:
                mirror.setdefault("property_name", prop.property_name)
                mirror.setdefault("province", prop.province)
                mirror.setdefault("status", prop.status)
                mirror.setdefault("property_type", prop.property_type)
            save_or_update_property_firestore(
                property_id=prop.property_id,
                fields=mirror,
                images=input_images,
                cover_image=cover_image
            )
        except Exception as e:
            logger.warning(f"Error syncing updated property to Firestore: {e}")

    # 3. Invalidate search cache so updated property data is immediately reflected
    from app.services.cache_service import search_cache
    search_cache.invalidate()

    return {
        "property_id": prop.property_id,
        "title": prop.property_name,
        "mode": "create" if is_created else "update",
        "updated_fields": updated_fields,
        "images_count": len(input_images),
        "images": input_images,
        "current_status": prop.status,
        "operation_id": operation_id,
        "undoable": bool(operation_id),
        "undo_window_days": 6 if operation_id else None,
    }


def _execute_send_email(
    payload: Dict[str, Any],
    staff_user_email: Optional[str] = None,
    db: Optional[Session] = None
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
    db: Optional[Session] = None
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


def _is_real_db_session(db: Any) -> bool:
    if db is None:
        return False
    return not type(db).__module__.startswith("unittest.mock")


def _load_sql_property(db: Any, property_id: str) -> Optional[Property]:
    if not _is_real_db_session(db):
        return None
    return db.query(Property).filter(Property.property_id == property_id).first()


def _read_raw_firestore(property_id: str) -> Optional[Dict[str, Any]]:
    from app.services.firestore_service import get_raw_property_document
    return get_raw_property_document(property_id, target="admin")


def _fs_sync_enabled() -> bool:
    try:
        return bool(settings.get_admin_enable_firestore_sync())
    except Exception:
        return bool(getattr(settings, "ENABLE_FIRESTORE_SYNC", True))


def _invalidate_search_cache() -> None:
    try:
        from app.services.cache_service import search_cache
        search_cache.invalidate()
    except Exception as exc:
        logger.warning("Search cache invalidation failed: %s", exc)


def _summary_sql(prop: Any) -> Optional[Dict[str, Any]]:
    if prop is None:
        return None
    price = getattr(prop, "price_numeric_min", None)
    try:
        price_out = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_out = None
    return {
        "property_id": getattr(prop, "property_id", None),
        "name": getattr(prop, "property_name", None),
        "price": price_out,
        "status": getattr(prop, "status", None),
        "isActive": bool(prop.is_public) if getattr(prop, "is_public", None) is not None else None,
        "websites": list(getattr(prop, "websites", None) or []),
        "property_type": getattr(prop, "property_type", None),
    }


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


_SQL_MUTABLE = {
    "property_name", "project_name", "property_type", "status",
    "country", "province", "district", "subdistrict", "area", "address",
    "price_display", "price_numeric_min", "price_numeric_max", "currency", "price_note",
    "bedrooms_display", "bedrooms_min", "bedrooms_max", "bathrooms",
    "area_display", "area_sqm_min", "area_sqm_max", "usable_area_sqm", "land_area_sqm",
    "description", "has_pool", "beach_access", "sea_view",
    "features", "facilities", "amenities", "highlights", "developer_name", "ownership_type",
    "websites", "raw_metadata",
}


def _apply_sql_fields(prop: Property, fields: Dict[str, Any], websites: Optional[List[str]] = None) -> None:
    data = dict(fields or {})
    if data.get("bedrooms") is not None and data.get("bedrooms_min") is None:
        beds = int(data["bedrooms"])
        data["bedrooms_min"] = beds
        data["bedrooms_max"] = beds
        data["bedrooms_display"] = str(beds)
    if _is_blank(data.get("province")) and not _is_blank(data.get("location")):
        data["province"] = data["location"]
    for key, value in data.items():
        if key in _SQL_MUTABLE and value is not None and hasattr(prop, key):
            setattr(prop, key, value)
    if websites is not None:
        prop.websites = list(websites)
    if data.get("is_public") is not None:
        prop.is_public = bool(data["is_public"])


def _duplicate_property_message(db: Any, property_id: str, name: str) -> Optional[str]:
    slug = name_slug(name)
    seen_ids = []
    for candidate in (property_id, slug):
        if not candidate or candidate in seen_ids:
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
                if doc.id == property_id or any(name_slug(label) == slug for label in labels):
                    return (
                        f"A property named '{name}' already exists in Firestore as '{doc.id}'. "
                        "Use update instead of create."
                    )
    except Exception as exc:
        logger.warning("Firestore duplicate scan failed: %s", exc)

    if _is_real_db_session(db):
        for row in db.query(Property).all():
            if row.property_id in (property_id, slug) or (slug and name_slug(row.property_name or "") == slug):
                return (
                    f"Property '{name or property_id}' already exists in Postgres as '{row.property_id}'. "
                    "Use update instead of create."
                )
    return None


def firestore_document_from_sql(prop: Property, extra_fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Complete Firestore project document built from a Postgres row. Never nameless."""
    name = (getattr(prop, "property_name", None) or "").strip()
    price = getattr(prop, "price_numeric_min", None)
    try:
        price_from = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_from = None
    websites = list(getattr(prop, "websites", None) or [])
    doc: Dict[str, Any] = {
        "name": name,
        "title": name,
        "propertyType": getattr(prop, "property_type", None),
        "province": getattr(prop, "province", None),
        "priceFrom": price_from,
        "isActive": bool(prop.is_public) if getattr(prop, "is_public", None) is not None else True,
        "websites": websites,
        "domains": list(websites),
        "status": getattr(prop, "status", None),
        "property_id": getattr(prop, "property_id", None),
    }
    if getattr(prop, "price_numeric_max", None) is not None:
        doc["priceTo"] = float(prop.price_numeric_max)
    if getattr(prop, "price_display", None):
        doc["priceDisplay"] = prop.price_display
    if getattr(prop, "description", None):
        doc["description"] = prop.description
    if extra_fields:
        if extra_fields.get("property_name"):
            doc["name"] = extra_fields["property_name"]
            doc["title"] = extra_fields["property_name"]
        if extra_fields.get("property_type"):
            doc["propertyType"] = extra_fields["property_type"]
        if extra_fields.get("province"):
            doc["province"] = extra_fields["province"]
        elif extra_fields.get("location") and not doc.get("province"):
            doc["province"] = extra_fields["location"]
        if extra_fields.get("status"):
            doc["status"] = extra_fields["status"]
        if extra_fields.get("price_numeric_min") is not None:
            doc["priceFrom"] = float(extra_fields["price_numeric_min"])
        if extra_fields.get("price_numeric_max") is not None:
            doc["priceTo"] = float(extra_fields["price_numeric_max"])
        if extra_fields.get("price_display"):
            doc["priceDisplay"] = extra_fields["price_display"]
        if extra_fields.get("description"):
            doc["description"] = extra_fields["description"]
        if extra_fields.get("websites") is not None:
            doc["websites"] = list(extra_fields["websites"])
            doc["domains"] = list(extra_fields["websites"])
        if extra_fields.get("is_public") is not None:
            doc["isActive"] = bool(extra_fields["is_public"])
    if not doc.get("name"):
        doc["name"] = name
        doc["title"] = name
    return doc


def _raise_store_failure(
    *,
    message: str,
    sql_ok: bool,
    fs_ok: bool,
    failed: str,
    property_id: str,
    operation_id: Optional[str],
) -> None:
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "message": message,
            "sql": sql_ok,
            "firestore": fs_ok,
            "failed": failed,
            "property_id": property_id,
            "operation_id": operation_id,
        },
    )


def rest_create_property(
    db: Session,
    staff: AuthenticatedStaff,
    fields: Dict[str, Any],
    *,
    property_id: str,
    websites: List[str],
    images: Optional[List[str]] = None,
    cover_image: Optional[str] = None,
    allow_without_undo: bool = False,
) -> Dict[str, Any]:
    """Validate, snapshot, then write Postgres and Firestore together."""
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
    )

    fs_required = _fs_sync_enabled()
    prop = Property(
        property_id=property_id,
        property_name=str(fields.get("property_name")).strip(),
        is_public=True if fields.get("is_public") is None else bool(fields.get("is_public")),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        websites=list(websites or []),
    )
    _apply_sql_fields(prop, fields, websites=websites)
    try:
        db.add(prop)
        db.commit()
        db.refresh(prop)
    except Exception as exc:
        db.rollback()
        _raise_store_failure(
            message=f"Postgres write failed: {exc}",
            sql_ok=False,
            fs_ok=False,
            failed="sql",
            property_id=property_id,
            operation_id=operation_id,
        )

    fs_ok = False
    if fs_required:
        try:
            from app.services.firestore_service import save_or_update_property_firestore
            wrote = save_or_update_property_firestore(
                property_id=property_id,
                fields={**fields, "websites": list(websites or []), "is_public": prop.is_public},
                images=images or [],
                cover_image=cover_image,
                target="admin",
            )
            if not wrote:
                raise RuntimeError("Firestore write returned failure")
            fs_ok = True
        except Exception as exc:
            try:
                db.query(Property).filter(Property.property_id == property_id).delete(
                    synchronize_session="fetch"
                )
                db.commit()
                db.expire_all()
            except Exception:
                db.rollback()
            _raise_store_failure(
                message=f"Firestore write failed; Postgres row was rolled back. {exc}",
                sql_ok=False,
                fs_ok=False,
                failed="firestore",
                property_id=property_id,
                operation_id=operation_id,
            )

    _invalidate_search_cache()
    stores = {"sql": True, "firestore": fs_ok}
    after = {
        "sql": _summary_sql(prop),
        "firestore": _summary_fs(_read_raw_firestore(property_id)) if fs_ok else None,
    }
    audit = _audit_rest(db, staff, "create", property_id, operation_id, stores, None, after)
    written = ["Postgres"] + (["Firestore"] if fs_ok else [])
    return {
        "status": "CREATED",
        "message": f"Property '{property_id}' written to {' and '.join(written)}.",
        "property_id": property_id,
        "operation_id": operation_id,
        "undo_window_days": 6 if operation_id else None,
        "sql": True,
        "firestore": fs_ok,
        "stores": stores,
        "websites": list(websites or []),
        "audit": audit,
    }


def rest_update_property(
    db: Session,
    staff: AuthenticatedStaff,
    property_id: str,
    fields: Dict[str, Any],
    *,
    websites: Optional[List[str]] = None,
    images: Optional[List[str]] = None,
    cover_image: Optional[str] = None,
    allow_without_undo: bool = False,
) -> Dict[str, Any]:
    """Update the stores that already hold the property. Never invent a nameless Firestore doc."""
    errors = validate_listing_values(fields, is_create=False)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Validation failed", "fields": errors},
        )
    raw = _read_raw_firestore(property_id)
    prop = _load_sql_property(db, property_id)
    if raw is None and prop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found.",
        )
    before = {"sql": _summary_sql(prop), "firestore": _summary_fs(raw)}
    operation_id = _require_undo_snapshot(
        db=db,
        staff=staff,
        action="update",
        property_id=property_id,
        source="admin_api_update",
        allow_without_undo=allow_without_undo,
    )

    sql_ok = False
    fs_ok = False
    if prop is not None:
        try:
            _apply_sql_fields(prop, fields, websites=websites)
            prop.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(prop)
            sql_ok = True
        except Exception as exc:
            db.rollback()
            _raise_store_failure(
                message=f"Postgres update failed. Snapshot kept for undo. {exc}",
                sql_ok=False,
                fs_ok=False,
                failed="sql",
                property_id=property_id,
                operation_id=operation_id,
            )

    if raw is not None:
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
            fs_ok = True
        except Exception as exc:
            _raise_store_failure(
                message=f"Firestore update failed. Snapshot kept for undo. {exc}",
                sql_ok=sql_ok,
                fs_ok=False,
                failed="firestore",
                property_id=property_id,
                operation_id=operation_id,
            )
    elif prop is not None and _fs_sync_enabled():
        document = firestore_document_from_sql(prop, fields)
        if not (document.get("name") or document.get("title")):
            _raise_store_failure(
                message="Refusing to create a nameless Firestore document. Snapshot kept for undo.",
                sql_ok=sql_ok,
                fs_ok=False,
                failed="firestore",
                property_id=property_id,
                operation_id=operation_id,
            )
        from app.services.firestore_service import write_admin_property_document
        if not write_admin_property_document(property_id, document, merge=False, target="admin"):
            _raise_store_failure(
                message="Firestore sync from Postgres failed. Snapshot kept for undo.",
                sql_ok=sql_ok,
                fs_ok=False,
                failed="firestore",
                property_id=property_id,
                operation_id=operation_id,
            )
        fs_ok = True

    _invalidate_search_cache()
    stores = {"sql": sql_ok, "firestore": fs_ok}
    after = {
        "sql": _summary_sql(prop) if sql_ok else None,
        "firestore": _summary_fs(_read_raw_firestore(property_id)) if fs_ok else None,
    }
    audit = _audit_rest(db, staff, "update", property_id, operation_id, stores, before, after)
    parts = []
    if sql_ok:
        parts.append("Postgres")
    if fs_ok:
        parts.append("Firestore")
    where = " and ".join(parts) if parts else "no store"
    return {
        "status": "UPDATED",
        "message": f"Property '{property_id}' written to {where}.",
        "property_id": property_id,
        "operation_id": operation_id,
        "undo_window_days": 6 if operation_id else None,
        "sql": sql_ok,
        "firestore": fs_ok,
        "stores": stores,
        "websites": websites if websites is not None else (
            list(prop.websites or []) if prop is not None else (raw.get("websites") if raw else [])
        ),
        "audit": audit,
    }


def rest_delete_property(
    db: Session,
    staff: AuthenticatedStaff,
    property_id: str,
    *,
    hard_delete: bool = False,
    confirm_property_id: Optional[str] = None,
    allow_without_undo: bool = False,
) -> Dict[str, Any]:
    """Soft-delete by default. Hard delete requires a typed property id confirmation."""
    if hard_delete and confirm_property_id != property_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="hard_delete requires confirm_property_id equal to the property id",
        )
    raw = _read_raw_firestore(property_id)
    prop = _load_sql_property(db, property_id)
    if raw is None and prop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found.",
        )
    before = {"sql": _summary_sql(prop), "firestore": _summary_fs(raw)}
    operation_id = _require_undo_snapshot(
        db=db,
        staff=staff,
        action="delete",
        property_id=property_id,
        source="admin_api_delete",
        allow_without_undo=allow_without_undo,
    )

    sql_ok = False
    fs_ok = False
    if raw is not None:
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
                sql_ok=False,
                fs_ok=False,
                failed="firestore",
                property_id=property_id,
                operation_id=operation_id,
            )
    if prop is not None:
        try:
            if hard_delete:
                db.query(Property).filter(Property.property_id == property_id).delete(
                    synchronize_session="fetch"
                )
            else:
                prop.is_public = False
                prop.status = "Archived"
            db.commit()
            sql_ok = True
        except Exception as exc:
            db.rollback()
            _raise_store_failure(
                message=f"Postgres delete failed. Snapshot kept for undo. {exc}",
                sql_ok=False,
                fs_ok=fs_ok,
                failed="sql",
                property_id=property_id,
                operation_id=operation_id,
            )

    _invalidate_search_cache()
    stores = {"sql": sql_ok, "firestore": fs_ok}
    after = {
        "sql": _summary_sql(_load_sql_property(db, property_id)) if not hard_delete else None,
        "firestore": _summary_fs(_read_raw_firestore(property_id)),
    }
    audit = _audit_rest(db, staff, "delete", property_id, operation_id, stores, before, after)
    verb = "permanently deleted" if hard_delete else "archived"
    return {
        "status": "DELETED" if hard_delete else "ARCHIVED",
        "message": f"Property '{property_id}' {verb}.",
        "property_id": property_id,
        "operation_id": operation_id,
        "undo_window_days": 6 if operation_id else None,
        "hard_delete": hard_delete,
        "sql": sql_ok,
        "firestore": fs_ok,
        "stores": stores,
        "audit": audit,
    }


def _norm_compare(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    if isinstance(value, float):
        return round(value, 2)
    return value


def rest_property_consistency(db: Session, property_id: str) -> Dict[str, Any]:
    """Compare Firestore and Postgres for one property. Admin callers only."""
    raw = _read_raw_firestore(property_id)
    prop = _load_sql_property(db, property_id)
    if raw is None and prop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found.",
        )
    fs_view = _summary_fs(raw)
    sql_view = _summary_sql(prop)
    differences = []
    for field in ("name", "price", "status", "websites", "isActive"):
        fs_val = None if fs_view is None else fs_view.get(field)
        sql_val = None if sql_view is None else sql_view.get(field)
        if _norm_compare(fs_val) != _norm_compare(sql_val):
            differences.append({"field": field, "firestore": fs_val, "sql": sql_val})
    return {
        "property_id": property_id,
        "consistent": not differences and fs_view is not None and sql_view is not None,
        "in_firestore": raw is not None,
        "in_sql": prop is not None,
        "differences": differences,
        "firestore": fs_view,
        "sql": sql_view,
    }


def save_ingested_listing(
    db: Session,
    staff: AuthenticatedStaff,
    draft: Dict[str, Any],
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
    existing_meta: Dict[str, Any] = {}
    if mode == "update":
        current = _load_sql_property(db, property_id)
        if current is not None and isinstance(getattr(current, "raw_metadata", None), dict):
            existing_meta = dict(current.raw_metadata)
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

    stored = _load_sql_property(db, property_id)
    stored_name = getattr(stored, "property_name", None) if stored is not None else None
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
            tool_name="ingest_property_from_url",
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
        "sql": result.get("sql"),
        "firestore": result.get("firestore"),
    }
