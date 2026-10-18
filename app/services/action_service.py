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


def validate_property_data(
    payload: Dict[str, Any],
    existing_property: Optional[Any] = None
) -> Tuple[bool, List[str], str]:
    """
    Validates data completeness before adding or updating any property in PostgreSQL or Cloud Firestore.
    If ANY required value is missing, empty, or null, blocks insertion and returns:
    (False, missing_fields, error_message).
    """
    fields = payload.get("fields_to_update") or payload.get("fields") or {}
    mode = payload.get("mode", "update")
    property_id = payload.get("property_id")

    missing = []
    if not property_id or not str(property_id).strip() or str(property_id).strip().lower() in ["none", "null"]:
        missing.append("property_id")

    is_create = (mode == "create") or (existing_property is None and not payload.get("is_update_only"))

    if is_create:
        # Check required fields for new property: property_name, property_type, location, price, bedrooms, status
        prop_name = fields.get("property_name") or payload.get("title") or fields.get("title")
        if not prop_name or not str(prop_name).strip() or str(prop_name).strip().lower() in ["none", "null"]:
            missing.append("property_name")

        prop_type = fields.get("property_type") or payload.get("property_type")
        if not prop_type or not str(prop_type).strip() or str(prop_type).strip().lower() in ["none", "null"]:
            missing.append("property_type")

        loc = fields.get("location") or fields.get("province") or fields.get("district") or fields.get("area") or payload.get("location")
        if not loc or not str(loc).strip() or str(loc).strip().lower() in ["none", "null"]:
            missing.append("location")

        price = fields.get("price_display") or fields.get("price_numeric_min") or fields.get("price") or payload.get("price")
        if price is None or (isinstance(price, str) and (not price.strip() or price.strip().lower() in ["none", "null"])):
            missing.append("price")

        bedrooms = fields.get("bedrooms") or fields.get("bedrooms_display") or fields.get("bedrooms_min") or payload.get("bedrooms")
        if bedrooms is None or (isinstance(bedrooms, str) and (not str(bedrooms).strip() or str(bedrooms).strip().lower() in ["none", "null"])):
            missing.append("bedrooms")

        status_val = fields.get("status") or payload.get("status")
        if not status_val or not str(status_val).strip() or str(status_val).strip().lower() in ["none", "null"]:
            missing.append("status")

    else:
        # Update mode for an EXISTING property: only the fields being changed are required.
        # Never demand property_name/type/location/bedrooms/status for a partial update.
        if existing_property is not None:
            if not fields or not isinstance(fields, dict):
                missing.append("fields_to_update")
            else:
                for k, v in fields.items():
                    if k in ("websites", "selected_websites", "mode", "property_id"):
                        continue
                    if v is None or (isinstance(v, str) and (not v.strip() or v.strip().lower() in ["none", "null"])):
                        missing.append(f"{k}")
        else:
            # Update claimed but no existing record → treat like create completeness
            if not fields or not isinstance(fields, dict):
                missing.append("fields_to_update")
            else:
                for k, v in fields.items():
                    if v is None or (isinstance(v, str) and (not v.strip() or v.strip().lower() in ["none", "null"])):
                        missing.append(f"{k}")

    if missing:
        msg = (
            f"Without proper information I am not able to complete this task. "
            f"The following required value(s) are missing or null: {', '.join(missing)}. "
            f"Please provide these details so I can proceed."
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
        prop = db.query(Property).filter(Property.property_id == action_payload.get("property_id")).first() if action_payload.get("property_id") else None
        is_valid, missing, err_msg = validate_property_data(action_payload, existing_property=prop)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
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

    # 3. Check token if provided
    if confirmation_token and confirmation_token != action.confirmation_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid confirmation token."
        )

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

        return {
            "status": ACTION_STATUS_EXECUTED,
            "pending_action_id": str(action.id),
            "tool_name": action.tool_name,
            "execution_result": result_data,
            "operation_id": operation_id,
            "audit_log_id": str(audit.id),
            "confirmed_by": authenticated_staff.email
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

    prop = db.query(Property).filter(Property.property_id == property_id).first()
    # Validation uses original payload shape; inject trimmed fields
    validation_payload = dict(payload)
    if "fields_to_update" in validation_payload or "fields_to_update" in payload:
        validation_payload["fields_to_update"] = fields
    else:
        validation_payload["fields"] = fields
    is_valid, missing, err_msg = validate_property_data(validation_payload, existing_property=prop)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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

    if not prop:
        # Create a new property
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
        # Update existing property — never inject websites
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
            save_or_update_property_firestore(
                property_id=prop.property_id,
                fields={**fields, **updated_fields},
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
