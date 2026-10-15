import os
from datetime import datetime
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Conversation, Property
from app.core.config import settings
from app.core.auth import (
    AuthenticatedStaff,
    require_admin_or_employee,
    require_admin_or_employee_with_sites,
    require_admin_or_super_admin_with_sites,
    require_super_admin_with_sites
)
from app.core.tenant import normalize_website_list, normalize_website_domain
from app.services.property_search import (
    PropertyFilterParams,
    search_properties_safe,
    property_belongs_to_websites,
    get_property_by_id
)
from app.services.admin_agent import process_admin_chat
from app.services.action_service import (
    get_pending_action,
    confirm_and_execute_action,
    reject_pending_action
)

router = APIRouter(prefix="/admin", tags=["Admin Agent"])


# Schemas
class AdminChatRequest(BaseModel):
    session_id: str = Field(..., example="admin-session-001", description="Admin session identifier")
    message: Optional[str] = Field(None, example="What is the roof material of PROP-PHK-001?", description="Admin message")
    site_origin: str = Field("tajlandia.com", example="tajlandia.com", description="Admin origin client")
    selected_websites: Optional[List[str]] = Field(None, description="Active selected websites from UI checkboxes, e.g. ['tajlandia.com', 'varsoviaestate.com']")
    language: Optional[str] = Field("en", description="Language code for conversation ('en' or 'pl'). Default is 'en'", example="en")
    action: Optional[str] = Field(None, description="Direct action trigger ('confirm' or 'reject')", example="confirm")
    pending_action_id: Optional[str] = Field(None, description="Target action ID to confirm or reject", example="act_550e8400-...")


class AdminChatResponse(BaseModel):
    type: str = Field(..., example="text", description="'text' or 'action_proposal'")
    text: str = Field(..., description="Grounded response or action proposal text")
    session_id: str = Field(..., example="admin-session-001")
    conversation_id: str = Field(..., description="Database conversation UUID")
    requires_confirmation: bool = Field(False, description="Whether action requires explicit employee confirmation")
    pending_action_id: Optional[str] = Field(None, description="UUID of staged pending action if confirmation required")
    action_status: Optional[str] = Field(None, description="Lifecycle status: 'proposed', 'executed', 'rejected', or null")
    execution_result: Optional[Dict[str, Any]] = Field(None, description="Result payload if action was executed")
    tools_used: Optional[List[Dict[str, Any]]] = Field(None, description="Claude tools invoked during turn")
    download_link: Optional[str] = Field(None, description="Direct download URL for generated report")
    language: str = Field("en", description="Active response language ('en' or 'pl')", example="en")


class ActionConfirmRequest(BaseModel):
    confirmation_token: Optional[str] = Field(None, description="Cryptographic confirmation token if required by policy")


class ActionRejectRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for rejecting the proposed action")


# 1. Admin Chat (Unified Action & Chatbot Endpoint)
@router.post("/chat", response_model=AdminChatResponse)
def admin_chat_endpoint(
    req: AdminChatRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Authenticated Admin / Internal Team AI Agent unified endpoint.
    Handles general inquiries, technical dossiers, reports, write proposals,
    and in-chat action confirmations/rejections in one single API.
    Strictly scoped to the staff member's active selected websites.
    """
    user_msg = (req.message or "").strip()
    if not user_msg and not req.action:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide a chat message or specify an action ('confirm'/'reject')."
        )

    # If request body explicitly supplies selected_websites, validate and apply
    if req.selected_websites:
        norm_selected = normalize_website_list(req.selected_websites)
        unauthorized = [s for s in norm_selected if s not in staff.allowed_websites]
        if unauthorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: You do not have permissions for website(s): {unauthorized}. Authorized: {staff.allowed_websites}."
            )
        staff.selected_websites = norm_selected

    active_origin = staff.selected_websites[0] if staff.selected_websites else req.site_origin

    res = process_admin_chat(
        db=db,
        session_id=req.session_id,
        message=user_msg,
        staff_user=staff,
        site_origin=active_origin,
        requested_language=req.language,
        action=req.action,
        pending_action_id=req.pending_action_id
    )
    return res


# 2. Admin Conversation History
@router.get("/chat/history/{session_id}")
def get_admin_history_endpoint(
    session_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """
    Retrieves full admin multi-turn conversation history.
    Includes tool invocations, results, and action proposals.
    Strictly isolated from public chatbot conversations.
    """
    conv = db.query(Conversation).filter(
        Conversation.session_id == session_id,
        Conversation.agent_type == "admin"
    ).first()

    if not conv:
        return {
            "session_id": session_id,
            "turns_count": 0,
            "messages": []
        }

    return {
        "session_id": session_id,
        "conversation_id": str(conv.id),
        "agent_type": "admin",
        "turns_count": len(conv.message_history or []),
        "messages": conv.message_history or [],
        "created_at": conv.created_at.isoformat() if conv.created_at else None
    }


# 3. Pending Action Inspection
@router.get("/actions/{pending_action_id}")
def inspect_pending_action_endpoint(
    pending_action_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """Retrieves current status and payload details of a staged pending action."""
    action = get_pending_action(db, pending_action_id)
    return {
        "pending_action_id": str(action.id),
        "tool_name": action.tool_name,
        "status": action.status,
        "action_payload": action.action_payload,
        "staff_user_id": action.staff_user_id,
        "confirmation_token": action.confirmation_token,
        "expires_at": action.expires_at.isoformat(),
        "created_at": action.created_at.isoformat(),
        "execution_result": action.execution_result
    }


# 4. Pending Action Confirmation & Execution
@router.post("/actions/{pending_action_id}/confirm")
def confirm_pending_action_endpoint(
    pending_action_id: str,
    body: Optional[ActionConfirmRequest] = None,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """
    Explicit Two-Step Confirmation endpoint for write actions.
    Validates token, server-side expiration, and permissions, then executes the action.
    """
    token = body.confirmation_token if body else None
    return confirm_and_execute_action(
        db=db,
        pending_action_id=pending_action_id,
        authenticated_staff=staff,
        confirmation_token=token
    )


# 5. Pending Action Rejection
@router.post("/actions/{pending_action_id}/reject")
def reject_pending_action_endpoint(
    pending_action_id: str,
    body: Optional[ActionRejectRequest] = None,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """Rejects a staged pending action and logs the rejection in audit logs."""
    reason = body.reason if body else None
    return reject_pending_action(
        db=db,
        pending_action_id=pending_action_id,
        authenticated_staff=staff,
        reason=reason
    )


# 6. Report Download Endpoint
@router.get("/reports/{filename}")
def download_report_endpoint(
    filename: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee)
):
    """Downloads a generated PDF/XLSX/DOCX/CSV property intelligence report."""
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(settings.REPORT_STORAGE_DIR, safe_filename)
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Requested report file not found on server."
        )
    media_map = {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".csv": "text/csv"
    }
    ext = os.path.splitext(safe_filename)[1].lower()
    media_type = media_map.get(ext, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type, filename=safe_filename)


# ==============================================================================
# 7. Admin Bulk Property Operations (Excel/PDF/DOCX/CSV) with 6-Day Undo
# ==============================================================================

@router.post("/properties/batch-file")
async def execute_batch_file_endpoint(
    file: UploadFile = File(..., description="Uploaded Excel (.xlsx/.xls), CSV, PDF, or Word (.docx) file containing property list"),
    action: str = Form(..., description="Action to perform on target properties: 'delete', 'update', or 'create'"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """
    Instantly applies bulk property mutations (delete, update, or create) from an uploaded file.
    Automatically captures a full pre-modification snapshot stored for exactly 6 days,
    enabling instant rollback via the Undo API.
    """
    from app.services.admin_batch_service import (
        parse_property_batch_file,
        execute_batch_property_operation,
        BatchOperationError
    )

    try:
        content = await file.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty."
            )

        records = parse_property_batch_file(content, file.filename or "uploaded_file")
        result = execute_batch_property_operation(
            action_type=action,
            records=records,
            staff_user=staff,
            source_filename=file.filename or "uploaded_file",
            db_session=db
        )
        return result

    except BatchOperationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error executing batch operation: {str(e)}"
        )


@router.post("/operations/{operation_id}/undo")
def undo_batch_operation_endpoint(
    operation_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """
    Instantly rolls back a previous batch operation within its 6-day retention window.
    Restores the exact document states from the saved snapshot.
    """
    from app.services.admin_batch_service import (
        undo_batch_property_operation,
        BatchOperationError
    )

    try:
        result = undo_batch_property_operation(
            operation_id=operation_id,
            staff_user=staff,
            db_session=db
        )
        return result
    except BatchOperationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error rolling back operation: {str(e)}"
        )


@router.get("/operations/history")
def get_batch_operations_history_endpoint(
    limit: int = 50,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee)
):
    """
    Returns full history of all past batch operations, showing affected property counts,
    timestamps, and remaining time left in the 6-day undo window.
    """
    from app.services.admin_batch_service import get_batch_operations_history
    return {
        "operations": get_batch_operations_history(limit=limit)
    }


# ==============================================================================
# 8. Direct Property CRUD Endpoints (REST API for Frontend Developers)
# ==============================================================================

class PropertyCreateRequest(BaseModel):
    property_id: Optional[str] = Field(None, example="PROP-PHK-101", description="Unique property ID. If omitted, will be auto-generated.")
    property_name: str = Field(..., example="Villa Horizon Luxury Residences", description="Property title/name")
    project_name: Optional[str] = Field(None, example="Villa Horizon")
    property_type: Optional[str] = Field("Villa", example="Villa", description="'Villa', 'Condominium', 'Townhouse'")
    status: Optional[str] = Field("Ready to move in", example="Ready to move in", description="'Ready to move in', 'Under construction', 'Off-plan'")
    location: Optional[str] = Field("Phuket, Thailand", example="Bang Tao, Phuket")
    province: Optional[str] = Field("Phuket", example="Phuket")
    district: Optional[str] = Field(None, example="Thalang")
    address: Optional[str] = Field(None, example="123 Lagoon Road, Choeng Thale")
    price_display: Optional[str] = Field("from 18.50M THB", example="from 18.50M THB")
    price_numeric_min: Optional[float] = Field(None, example=18500000.0)
    price_numeric_max: Optional[float] = Field(None, example=24000000.0)
    currency: Optional[str] = Field("THB", example="THB")
    bedrooms: Optional[int] = Field(None, example=3)
    bedrooms_min: Optional[int] = Field(None, example=3)
    bedrooms_max: Optional[int] = Field(None, example=4)
    bathrooms: Optional[int] = Field(None, example=4)
    area_sqm_min: Optional[float] = Field(None, example=320.0)
    area_sqm_max: Optional[float] = Field(None, example=450.0)
    has_pool: Optional[bool] = Field(False, example=True)
    beach_access: Optional[bool] = Field(False, example=True)
    sea_view: Optional[bool] = Field(False, example=True)
    description: Optional[str] = Field(None, example="Stunning luxury pool villa with panoramic sea views.")
    images: Optional[List[str]] = Field(default_factory=list, example=["https://example.com/pool.jpg", "https://example.com/bedroom.jpg"])
    cover_image: Optional[str] = Field(None, example="https://example.com/pool.jpg")
    websites: Optional[List[str]] = Field(None, example=["tajlandia.com", "varsoviaestate.com"], description="Specific website domains for this property: 'varsoviaestate.com', 'tajlandia.com', 'thai.estate'")
    is_public: Optional[bool] = Field(True, example=True)


class PropertyUpdateRequest(BaseModel):
    property_name: Optional[str] = Field(None, example="Villa Horizon Luxury Residences")
    project_name: Optional[str] = Field(None, example="Villa Horizon")
    property_type: Optional[str] = Field(None, example="Villa")
    status: Optional[str] = Field(None, example="Ready to move in")
    location: Optional[str] = Field(None, example="Bang Tao, Phuket")
    province: Optional[str] = Field(None, example="Phuket")
    district: Optional[str] = Field(None, example="Thalang")
    address: Optional[str] = Field(None, example="123 Lagoon Road, Choeng Thale")
    price_display: Optional[str] = Field(None, example="from 19.00M THB")
    price_numeric_min: Optional[float] = Field(None, example=19000000.0)
    price_numeric_max: Optional[float] = Field(None, example=25000000.0)
    currency: Optional[str] = Field(None, example="THB")
    bedrooms: Optional[int] = Field(None, example=4)
    bedrooms_min: Optional[int] = Field(None, example=3)
    bedrooms_max: Optional[int] = Field(None, example=5)
    bathrooms: Optional[int] = Field(None, example=4)
    area_sqm_min: Optional[float] = Field(None, example=350.0)
    area_sqm_max: Optional[float] = Field(None, example=480.0)
    has_pool: Optional[bool] = Field(None, example=True)
    beach_access: Optional[bool] = Field(None, example=True)
    sea_view: Optional[bool] = Field(None, example=True)
    description: Optional[str] = Field(None, example="Updated description with new villa details.")
    images: Optional[List[str]] = Field(None, example=["https://example.com/new-image.jpg"])
    cover_image: Optional[str] = Field(None, example="https://example.com/new-cover.jpg")
    websites: Optional[List[str]] = Field(None, example=["tajlandia.com"], description="Update website domains for this property")
    is_public: Optional[bool] = Field(None, example=True)


@router.get("/properties")
def list_admin_properties_endpoint(
    location: Optional[str] = Query(None, description="Location filter"),
    property_type: Optional[str] = Query(None, description="Property type"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    bedrooms: Optional[int] = Query(None, ge=0),
    has_pool: Optional[bool] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Fetches property listings strictly scoped to the staff member's currently selected websites.
    If user selected Tajlandia & Varsovia, properties belonging exclusively to thai.estate are NOT returned.
    """
    params = PropertyFilterParams(
        location=location,
        property_type=property_type,
        min_price=min_price,
        max_price=max_price,
        bedrooms=bedrooms,
        has_pool=has_pool,
        status=status,
        websites=staff.selected_websites,
        limit=limit,
        offset=offset,
        is_public=None
    )
    results = search_properties_safe(db, params)
    output = []
    for p in results:
        output.append({
            "property_id": p.property_id,
            "property_name": p.property_name,
            "project_name": p.project_name,
            "property_type": p.property_type,
            "status": p.status,
            "province": p.province,
            "district": p.district,
            "area": p.area,
            "price_display": p.price_display,
            "bedrooms": p.bedrooms_display,
            "has_pool": p.has_pool,
            "websites": p.websites or [],
            "images": [img.image_url for img in p.images] if p.images else [],
            "is_public": p.is_public
        })
    return {
        "count": len(output),
        "selected_websites": staff.selected_websites,
        "properties": output
    }


@router.get("/properties/{property_id}")
def get_admin_property_endpoint(
    property_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Retrieves a single property listing.
    Strictly verifies that the property belongs to the staff member's currently selected websites.
    """
    prop = db.query(Property).filter(Property.property_id == property_id).first()
    if not prop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found."
        )
    if not property_belongs_to_websites(prop, staff.selected_websites):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied: Property '{property_id}' is not part of your currently selected website(s) "
                f"{staff.selected_websites}."
            )
        )
    return {
        "found": True,
        "property_id": prop.property_id,
        "property_name": prop.property_name,
        "project_name": prop.project_name,
        "property_type": prop.property_type,
        "status": prop.status,
        "location": {
            "country": prop.country,
            "province": prop.province,
            "district": prop.district,
            "area": prop.area,
            "address": prop.address
        },
        "pricing": {
            "display": prop.price_display,
            "numeric_min": float(prop.price_numeric_min) if prop.price_numeric_min else None,
            "currency": prop.currency
        },
        "details": {
            "bedrooms": prop.bedrooms_display,
            "bathrooms": prop.bathrooms,
            "area": prop.area_display
        },
        "amenities": prop.amenities,
        "websites": prop.websites or [],
        "images": [img.image_url for img in prop.images] if prop.images else [],
        "is_public": prop.is_public
    }


@router.post("/properties", status_code=status.HTTP_201_CREATED)
def create_property_endpoint(
    req: PropertyCreateRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Direct REST API to create a new property listing.
    Scoped strictly to the staff member's active selected websites.
    Instantly saves to PostgreSQL and synchronizes with Cloud Firestore `projects` collection.
    """
    import uuid
    from app.services.action_service import _execute_update_listing
    pid = req.property_id or f"PROP-{uuid.uuid4().hex[:8].upper()}"

    # Website scope validation
    if req.websites:
        norm_targets = normalize_website_list(req.websites)
        unauthorized = [s for s in norm_targets if s not in staff.allowed_websites]
        if unauthorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: You cannot create properties for website(s) outside your permissions: {unauthorized}."
            )
        not_selected = [s for s in norm_targets if s not in staff.selected_websites]
        if not_selected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Website(s) {not_selected} are not in your currently active checkbox selection ({staff.selected_websites})."
            )
        assigned_websites = norm_targets
    else:
        assigned_websites = list(staff.selected_websites)

    payload = {
        "property_id": pid,
        "mode": "create",
        "fields": req.model_dump(exclude_unset=True, exclude={"images", "cover_image"}),
        "images": req.images or [],
        "cover_image": req.cover_image,
        "websites": assigned_websites,
        "selected_websites": assigned_websites
    }
    result = _execute_update_listing(db, payload)
    return {
        "status": "CREATED",
        "message": f"Property '{pid}' successfully created in database and Firestore.",
        "property_id": pid,
        "websites": assigned_websites,
        "details": result
    }


@router.put("/properties/{property_id}")
def update_property_endpoint(
    property_id: str,
    req: PropertyUpdateRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Direct REST API to update an existing property listing.
    Strictly verifies that the property belongs to the staff member's currently selected websites.
    Updates fields, pricing, bedrooms, and images in PostgreSQL and Cloud Firestore.
    """
    from app.services.action_service import _execute_update_listing
    
    # 1. Verify property existence and website scope
    prop = db.query(Property).filter(Property.property_id == property_id).first()
    if not prop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found."
        )
    if not property_belongs_to_websites(prop, staff.selected_websites):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied: Property '{property_id}' does not belong to your currently selected website(s) "
                f"{staff.selected_websites}."
            )
        )

    # 2. If updating websites, validate user has permission
    assigned_websites = None
    if req.websites is not None:
        norm_targets = normalize_website_list(req.websites)
        unauthorized = [s for s in norm_targets if s not in staff.allowed_websites]
        if unauthorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Cannot assign property to unauthorized website(s): {unauthorized}."
            )
        not_selected = [s for s in norm_targets if s not in staff.selected_websites]
        if not_selected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Website(s) {not_selected} are not in your currently active checkbox selection ({staff.selected_websites})."
            )
        assigned_websites = norm_targets

    update_data = req.model_dump(exclude_unset=True, exclude={"images", "cover_image"})
    payload = {
        "property_id": property_id,
        "mode": "update",
        "fields": update_data,
        "images": req.images,
        "cover_image": req.cover_image
    }
    if assigned_websites is not None:
        payload["websites"] = assigned_websites

    result = _execute_update_listing(db, payload)
    return {
        "status": "UPDATED",
        "message": f"Property '{property_id}' successfully updated in database and Firestore.",
        "property_id": property_id,
        "websites": assigned_websites or prop.websites,
        "details": result
    }


@router.delete("/properties/{property_id}")
def delete_property_endpoint(
    property_id: str,
    hard_delete: bool = False,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Direct REST API to delete/deactivate a property listing.
    Strictly verifies that the property belongs to the staff member's currently selected websites.
    """
    from app.services.firestore_service import delete_property_firestore
    from app.services.cache_service import search_cache

    # 1. Update/Delete in PostgreSQL with website verification
    prop = db.query(Property).filter(Property.property_id == property_id).first()
    if not prop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found."
        )
    if not property_belongs_to_websites(prop, staff.selected_websites):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied: Property '{property_id}' does not belong to your currently selected website(s) "
                f"{staff.selected_websites}."
            )
        )

    if hard_delete:
        db.delete(prop)
    else:
        prop.is_public = False
        prop.status = "Archived"
    db.commit()

    # 2. Update/Delete in Cloud Firestore
    fs_ok = delete_property_firestore(
        property_id=property_id,
        hard_delete=hard_delete,
        staff_email=staff.email
    )

    # 3. Invalidate search cache
    search_cache.invalidate()

    return {
        "status": "DELETED" if hard_delete else "ARCHIVED",
        "message": f"Property '{property_id}' successfully {'permanently deleted' if hard_delete else 'archived/deactivated'}.",
        "property_id": property_id,
        "hard_delete": hard_delete,
        "firestore_synced": fs_ok
    }


# ==============================================================================
# 9. Admin Activity Logs & Data History Endpoints
# ==============================================================================

@router.get("/activity-logs", tags=["Admin Logs & Audit Trail"])
def get_admin_activity_logs_endpoint(
    admin_email: Optional[str] = Query(None, description="Filter by admin email or ID"),
    action: Optional[str] = Query(None, description="Filter by action/tool name (e.g. update_listing, auth:login)"),
    status: Optional[str] = Query(None, description="Filter by status (e.g. SUCCESS, FAILED, PENDING, REJECTED)"),
    start_date: Optional[datetime] = Query(None, description="Filter logs starting from date (ISO format)"),
    end_date: Optional[datetime] = Query(None, description="Filter logs up to date (ISO format)"),
    limit: int = Query(50, ge=1, le=500, description="Page limit"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    source: str = Query("all", description="'postgres', 'firestore', or 'all'"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """
    Returns full administrative activity logs.
    Includes tool executions, staff chat sessions, database mutations, and auth actions.
    Aggregates from PostgreSQL and Cloud Firestore.
    """
    from app.services.audit_service import query_admin_activity_logs

    return query_admin_activity_logs(
        db=db,
        admin_email=admin_email,
        action=action,
        status=status,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
        source=source
    )


@router.get("/data-history", tags=["Admin Logs & Audit Trail"])
def get_data_change_history_endpoint(
    target_type: Optional[str] = Query(None, description="Filter by entity type ('property', 'user', 'batch_operation')"),
    target_id: Optional[str] = Query(None, description="Filter by entity ID (e.g. 'PROP-PHK-101' or user email)"),
    updated_by: Optional[str] = Query(None, description="Filter by admin email/ID who performed the update"),
    limit: int = Query(50, ge=1, le=500, description="Page limit"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """
    Returns chronological history of who updated which data across the platform.
    Details the admin actor, target entity, specific fields changed, confirmation status, and results.
    """
    from app.services.audit_service import query_data_change_history

    return query_data_change_history(
        db=db,
        target_type=target_type,
        target_id=target_id,
        updated_by=updated_by,
        limit=limit,
        offset=offset
    )


# RBAC Admin & Worker Directory Management Endpoints (/api/v1/admin/users)
from app.api.v1.auth import (
    list_admin_users_with_google_status,
    create_admin_user,
    get_admin_user_details,
    update_admin_user,
    delete_admin_user,
    AdminUserDirectoryItem,
    CreateUserRequest,
    UpdateUserRequest
)

router.get("/users", response_model=List[AdminUserDirectoryItem], tags=["Admin Directory Management"])(list_admin_users_with_google_status)
router.post("/users", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Admin Directory Management"])(create_admin_user)
router.get("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(get_admin_user_details)
router.patch("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(update_admin_user)
router.put("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(update_admin_user)
router.delete("/users/{user_id}", tags=["Admin Directory Management"])(delete_admin_user)
