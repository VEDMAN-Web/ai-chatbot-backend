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

logger = logging.getLogger(__name__)

# Lifecycle States
ACTION_STATUS_PENDING = "PENDING"
ACTION_STATUS_CONFIRMED = "CONFIRMED"
ACTION_STATUS_EXECUTED = "EXECUTED"
ACTION_STATUS_REJECTED = "REJECTED"
ACTION_STATUS_EXPIRED = "EXPIRED"
ACTION_STATUS_FAILED = "FAILED"


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
        # Update mode: must provide non-empty fields to update and no null/empty values among updated fields
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
    minutes = expires_in_minutes or settings.ACTION_EXPIRATION_MINUTES
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=minutes)
    confirmation_token = f"act_{secrets.token_hex(16)}"

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

    # 4. Mark confirmed
    action.status = ACTION_STATUS_CONFIRMED
    action.confirmed_at = datetime.now(timezone.utc)
    db.commit()

    # 5. Execute action payload based on tool_name
    result_data: Dict[str, Any] = {}
    try:
        if action.tool_name in ["create_or_update_listing", "ingest_property_from_drive"]:
            result_data = _execute_update_listing(db, action.action_payload)
        elif action.tool_name == "send_email":
            result_data = _execute_send_email(
                action.action_payload,
                staff_user_email=authenticated_staff.email if authenticated_staff else None,
                db=db
            )
        elif action.tool_name == "create_calendar_event":
            result_data = _execute_create_calendar_event(
                action.action_payload,
                staff_user_email=authenticated_staff.email if authenticated_staff else None,
                db=db
            )
        else:
            raise ValueError(f"Unsupported tool action: {action.tool_name}")

        # 6. Mark executed
        action.status = ACTION_STATUS_EXECUTED
        action.executed_at = datetime.now(timezone.utc)
        action.execution_result = result_data
        db.commit()

        # 7. Record immutable audit log
        audit = record_audit_log(
            db=db,
            staff_user_id=authenticated_staff.email,
            tool_name=action.tool_name,
            proposed_payload=action.action_payload,
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


def _execute_update_listing(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Safely creates or updates property listings in PostgreSQL via parameterized ORM.
    Handles mode == 'create' (or missing property) and mode == 'update'.
    """
    property_id = payload.get("property_id")
    fields = payload.get("fields_to_update", {}) or payload.get("fields", {})
    mode = payload.get("mode", "update")
    if not property_id:
        raise ValueError("Missing required property_id in payload.")

    prop = db.query(Property).filter(Property.property_id == property_id).first()
    is_valid, missing, err_msg = validate_property_data(payload, existing_property=prop)
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

    from app.core.tenant import normalize_website_list, get_current_env_domain

    target_websites = payload.get("websites") or fields.get("websites") or payload.get("selected_websites")
    norm_websites = normalize_website_list(target_websites) if target_websites else None

    if not prop or mode == "create":
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
        # Update existing property
        if norm_websites is not None:
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
        "current_status": prop.status
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
    return {
        "delivered": res.get("success", True),
        "message_id": res.get("message_id"),
        "to": to_email,
        "subject": subject,
        "dispatched_at": res.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "provider": res.get("provider", "simulation"),
        "is_mock": res.get("is_mock", True)
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
    return {
        "created": res.get("success", True),
        "event_id": res.get("event_id"),
        "title": title,
        "start_time": start_time,
        "end_time": end_time or start_time,
        "attendees": attendees,
        "status": "confirmed",
        "google_calendar_url": res.get("google_calendar_url"),
        "is_mock": res.get("is_mock", True)
    }
