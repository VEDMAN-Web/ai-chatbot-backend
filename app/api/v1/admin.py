import json
import os
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List, Generator
from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.db.session import get_db
from app.db.models import Conversation, Property
from app.core.config import settings
from app.core.auth import (
    AuthenticatedStaff,
    require_admin_or_employee,
    require_admin_or_employee_with_sites,
    require_admin_or_super_admin,
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
    reject_pending_action,
    staff_owns_action,
)

router = APIRouter(prefix="/admin", tags=["Admin Agent"])


# Schemas
class AdminChatRequest(BaseModel):
    session_id: str = Field(..., example="admin-session-001", description="Admin session identifier")
    message: Optional[str] = Field(None, example="What is the roof material of 15 Sukhumvit Residences?", description="Admin message")
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
    suggestions: Optional[List[str]] = Field(None, description="Optional follow-up suggestions")
    debug: Optional[Dict[str, Any]] = Field(None, description="Present only when ADMIN_DEBUG_TIMING=true")


class ActionConfirmRequest(BaseModel):
    confirmation_token: Optional[str] = Field(None, description="Cryptographic confirmation token if required by policy")


class ActionRejectRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for rejecting the proposed action")


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_ADMIN_OWNER_KEY = "owner_user_id"


def _claim_admin_conversation(db: Session, session_id: str, staff: AuthenticatedStaff):
    """
    Admin chat session ids are chosen by the client, so a conversation must belong to the staff member using it.
    Without this, any staff member (workers included) could read - or continue - another person's admin chat by
    guessing its session id. Reviewing other staff's chats goes through /chat-logs, which is permission-scoped.
    A conversation with no owner yet (new, or created before this check existed) is claimed by the caller.
    """
    from app.services.conversation_service import get_or_create_conversation, update_conversation_preferences

    conv = get_or_create_conversation(db=db, session_id=session_id, agent_type="admin")
    prefs = dict(getattr(conv, "preferences", None) or {})
    owner = prefs.get(_ADMIN_OWNER_KEY)
    if owner and str(owner) != str(staff.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    if not owner:
        if not update_conversation_preferences(session_id, "admin", {_ADMIN_OWNER_KEY: str(staff.user_id)}):
            prefs[_ADMIN_OWNER_KEY] = str(staff.user_id)
            conv.preferences = prefs
    return conv


# 1. Admin Chat (Unified Action & Chatbot Endpoint)
@router.post("/chat", response_model=None)
def admin_chat_endpoint(
    req: AdminChatRequest,
    stream: bool = Query(False, description="If true, return Server-Sent Events (status then final verified text)"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Authenticated Admin / Internal Team AI Agent unified endpoint.
    Handles general inquiries, technical dossiers, reports, write proposals,
    and in-chat action confirmations/rejections in one single API.
    Strictly scoped to the staff member's active selected websites.

    Default: JSON AdminChatResponse.
    With ?stream=true: SSE — status events while working, then a final
    verified text event (never streams unverified claim text mid-flight).
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
    if req.session_id:
        _claim_admin_conversation(db, req.session_id, staff)

    def _run() -> Dict[str, Any]:
        return process_admin_chat(
            db=db,
            session_id=req.session_id,
            message=user_msg,
            staff_user=staff,
            site_origin=active_origin,
            requested_language=req.language,
            action=req.action,
            pending_action_id=req.pending_action_id
        )

    if not stream:
        return _run()

    def event_gen() -> Generator[str, None, None]:
        yield _sse_pack("status", {"message": "Checking the database..."})
        try:
            res = _run()
        except Exception as e:
            logger.exception("admin chat stream failed: %s", e)
            yield _sse_pack("error", {"message": "Service temporarily unavailable"})
            return
        # Only emit verified final text (claim verifier already ran inside process_admin_chat)
        payload = {
            "type": res.get("type", "text"),
            "text": res.get("text") or "",
            "session_id": res.get("session_id"),
            "conversation_id": res.get("conversation_id"),
            "requires_confirmation": bool(res.get("requires_confirmation")),
            "pending_action_id": res.get("pending_action_id"),
            "action_status": res.get("action_status"),
            "tools_used": res.get("tools_used") or [],
            "download_link": res.get("download_link"),
            "language": res.get("language") or "en",
        }
        if res.get("debug") is not None:
            payload["debug"] = res["debug"]
        yield _sse_pack("final", payload)
        yield _sse_pack("done", {"ok": True})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# 2. Admin Conversation History
@router.get("/chat/history/{session_id}")
def get_admin_history_endpoint(
    session_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db)
):
    """
    Retrieves full admin multi-turn conversation history from Firestore.
    Includes tool invocations, results, and action proposals.
    Strictly isolated from public chatbot conversations.
    """
    conv = _claim_admin_conversation(db, session_id, staff)
    history = list(getattr(conv, "message_history", None) or [])
    created = getattr(conv, "created_at", None)
    return {
        "session_id": session_id,
        "conversation_id": str(getattr(conv, "id", session_id)),
        "agent_type": "admin",
        "turns_count": len(history),
        "messages": history,
        "preferences": getattr(conv, "preferences", None) or {},
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
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
    is_owner = staff_owns_action(action, staff)
    if not is_owner and staff.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view actions you proposed.")
    return {
        "pending_action_id": str(action.id),
        "tool_name": action.tool_name,
        "status": action.status,
        "action_payload": action.action_payload,
        "staff_user_id": action.staff_user_id,
        "confirmation_token": action.confirmation_token if is_owner else None,
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


# 6. Report Download Endpoints (auth + short-lived signed links)
def _serve_report_file(safe_filename: str, *, prefer_redirect: bool = True):
    """Stream or redirect a report from the same provider that stored it."""
    from app.services.report_service import (
        ReportExpiredError,
        ReportNotFoundError,
        load_report_bytes,
        get_report_metadata,
        _provider_for_storage_mode,
        get_storage_provider,
    )

    try:
        meta = get_report_metadata(safe_filename) or {}
        mode = meta.get("storage")
        provider = _provider_for_storage_mode(mode) if mode else get_storage_provider()

        if prefer_redirect and getattr(provider, "mode", None) == "s3":
            url = provider.get_presigned_url(safe_filename, expires_in=600)
            if url:
                return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)

        local_path = provider.local_path(safe_filename) if hasattr(provider, "local_path") else None
        if local_path and os.path.isfile(local_path):
            from app.services.report_service import REPORT_MEDIA_TYPES
            ext = os.path.splitext(safe_filename)[1].lower()
            media_type = REPORT_MEDIA_TYPES.get(ext, "application/octet-stream")
            return FileResponse(local_path, media_type=media_type, filename=safe_filename)

        content, media_type, _meta = load_report_bytes(safe_filename, storage=provider)
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{safe_filename}"',
            },
        )
    except ReportExpiredError as e:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(e)) from e
    except ReportNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e) or "This report is no longer available - regenerate it",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post("/reports/{filename}/link")
def create_report_signed_link_endpoint(
    filename: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
):
    """Return a short-lived HMAC signed download URL (valid ~10 minutes)."""
    from app.services.report_service import (
        create_signed_download_token,
        report_exists,
        validate_report_filename,
        ReportExpiredError,
        get_report_metadata,
    )

    try:
        safe = validate_report_filename(filename)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    meta = get_report_metadata(safe) or {}
    expires_at = meta.get("expires_at")
    if expires_at:
        try:
            from datetime import datetime, timezone
            exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="This report has expired",
                )
        except HTTPException:
            raise
        except Exception:
            pass

    if not report_exists(safe):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This report is no longer available - regenerate it",
        )

    token, exp = create_signed_download_token(safe)
    return {
        "url": f"/api/v1/admin/reports/download/{token}",
        "expires_at": exp,
        "filename": safe,
    }


@router.get("/reports/download/{token}")
def download_report_signed_endpoint(token: str):
    """Public (token-gated) download — no Bearer header required."""
    from app.services.report_service import (
        verify_signed_download_token,
        SignedLinkError,
    )

    try:
        safe = verify_signed_download_token(token)
    except SignedLinkError as e:
        code = status.HTTP_410_GONE if e.expired else status.HTTP_403_FORBIDDEN
        raise HTTPException(status_code=code, detail=str(e)) from e

    return _serve_report_file(safe, prefer_redirect=True)


@router.get("/reports/{filename}")
def download_report_endpoint(
    filename: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee)
):
    """Downloads a generated PDF/XLSX/DOCX/CSV property intelligence report (auth required)."""
    from app.services.report_service import validate_report_filename

    try:
        safe_filename = validate_report_filename(filename)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    return _serve_report_file(safe_filename, prefer_redirect=True)


# ==============================================================================
# 7. Admin Bulk Property Operations (Excel/PDF/DOCX/CSV) with 6-Day Undo
# ==============================================================================

MAX_BATCH_FILE_BYTES = 5 * 1024 * 1024


@router.post("/properties/batch-file", status_code=status.HTTP_202_ACCEPTED)
async def propose_batch_file_endpoint(
    file: UploadFile = File(..., description="Uploaded Excel (.xlsx/.xls), CSV, PDF, or Word (.docx) file containing property list"),
    action: str = Form(..., description="Action to perform on target properties: 'delete', 'update', or 'create'"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db)
):
    """
    Parses the uploaded file and STAGES the bulk change as a pending action with a preview of every row.
    Nothing is written until the staff member who uploaded the file confirms through
    POST /admin/actions/{pending_action_id}/confirm. A pre-change snapshot (6 days) is saved on confirm,
    so the Undo API still applies.
    """
    import tempfile
    from app.services.admin_agent import _execute_admin_tool
    from app.services.admin_batch_service import BatchOperationError

    operation = (action or "").lower().strip()
    if operation not in ("delete", "remove", "update", "create", "write"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="action must be 'delete', 'update' or 'create'.")

    content = await file.read(MAX_BATCH_FILE_BYTES + 1)
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(content) > MAX_BATCH_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"The file is larger than {MAX_BATCH_FILE_BYTES // (1024 * 1024)} MB.",
        )

    filename = os.path.basename(file.filename or "") or "uploaded_file"
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, filename)
            with open(path, "wb") as fh:
                fh.write(content)
            result, _needs_confirmation, pending_id = _execute_admin_tool(
                db,
                "execute_batch_property_action",
                {"operation_type": operation, "file_path": path, "notes": f"Uploaded file {filename}"},
                staff,
                None,
            )
    except BatchOperationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if not pending_id:
        detail = (result or {}).get("message") or (result or {}).get("error") or "The batch could not be proposed."
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    return result


@router.post("/operations/{operation_id}/undo")
def undo_batch_operation_endpoint(
    operation_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db)
):
    """
    Instantly rolls back a previous batch operation within its 6-day retention window.
    Restores the exact document states from the saved snapshot.
    """
    from app.services.admin_batch_service import (
        undo_batch_property_operation,
        BatchOperationError,
        OperationNotFound,
    )

    try:
        result = undo_batch_property_operation(
            operation_id=operation_id,
            staff_user=staff,
            db_session=db
        )
        if result.get("restored_ok") is False:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=result,
            )
        return result
    except HTTPException:
        raise
    except OperationNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
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
    limit: int = Query(50, ge=1, le=200),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin)
):
    """
    Returns full history of all past batch operations, showing affected property counts,
    timestamps, and remaining time left in the 6-day undo window.
    """
    from app.services.admin_batch_service import get_batch_operations_history
    return {
        "operations": get_batch_operations_history(limit=limit, staff_user=staff)
    }


@router.post("/maintenance/purge-expired", tags=["Admin Maintenance"])
def purge_expired_snapshots_endpoint(
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
):
    """
    Admin-only: delete expired undo snapshots from Firestore admin_batch_operations
    (expire_at < now). Safe for Cloud Scheduler. Never raises from the purge itself.

    Infra should also enable a Firestore TTL policy on field `expire_at` as a second safety net.
    """
    from app.services.admin_batch_service import purge_expired_snapshots
    from app.services.report_service import purge_old_reports

    deleted = purge_expired_snapshots()
    reports_deleted = purge_old_reports()
    return {
        "success": True,
        "deleted": deleted,
        "reports_deleted": reports_deleted,
        "message": f"Purged {deleted} expired snapshot(s), {reports_deleted} old report(s).",
    }


@router.get("/system/config-status", tags=["Admin Maintenance"])
def admin_config_status_endpoint(
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
):
    """
    Admin-only: feature-flag / wiring status as labels and booleans only.
    Never returns secrets, passwords, tokens, or credentialed URLs.
    """
    from app.core.config import get_admin_config_status

    return {**get_admin_config_status(), **_brochure_config_labels()}


# ==============================================================================
# 8. Direct Property CRUD Endpoints (REST API for Frontend Developers)
# ==============================================================================

from pydantic import model_validator

class LayoutItem(BaseModel):
    name: Optional[str] = Field(None, description="Layout/Unit title e.g. 2-Bedroom Deluxe")
    bedrooms: Optional[int] = Field(None, description="Bedrooms count")
    bathrooms: Optional[int] = Field(None, description="Bathrooms count")
    area_sqm: Optional[float] = Field(None, description="Total size in sqm")
    price: Optional[str] = Field(None, description="Price display or numerical")
    floor_plan_url: Optional[str] = Field(None, description="Floor plan diagram URL")


class PropertyCreateRequest(BaseModel):
    # --- 1. Basic Info ---
    project_name_pl: str = Field(..., description="[REQUIRED] Project Name in Polish")
    project_name_en: str = Field(..., description="[REQUIRED] Project Name in English")
    property_type: str = Field(..., description="[REQUIRED] Project Type e.g. Villa, Condominium, Apartment, Townhouse")
    slug_pl: Optional[str] = Field(None, description="[Optional] URL Slug in Polish")
    slug_en: Optional[str] = Field(None, description="[Optional] URL Slug in English")
    subtitle_pl: Optional[str] = Field(None, description="[Optional] Subtitle in Polish")
    subtitle_en: Optional[str] = Field(None, description="[Optional] Subtitle in English")
    country: str = Field(..., description="[REQUIRED] Country e.g. Thailand")
    city: str = Field(..., description="[REQUIRED] City e.g. Phuket, Bangkok, Samui")
    websites: Optional[List[str]] = Field(None, description="[Optional] Sites this project belongs to")

    # --- 2. Pricing ---
    currency: Optional[str] = Field("THB", description="[Optional] Currency e.g. THB, EUR, USD")
    price_from: Optional[float] = Field(None, description="[Optional] Price From (minimum numerical price)")
    price_to: Optional[float] = Field(None, description="[Optional] Price To (maximum numerical price)")
    expected_roi: Optional[str] = Field(None, description="[Optional] Expected ROI (%) e.g. 7.5%")
    rental_yield: Optional[str] = Field(None, description="[Optional] Rental Yield (%) e.g. 8.0%")

    # --- 3. Details (all optional) ---
    completion_status: Optional[str] = Field(None, description="[Optional] Completion Status e.g. Under construction, Ready to move in")
    completion_date: Optional[str] = Field(None, description="[Optional] Completion Date e.g. Q4 2026")
    total_units: Optional[int] = Field(None, description="[Optional] Total Units")
    available_units: Optional[int] = Field(None, description="[Optional] Available Units")
    area_from_sqm: Optional[float] = Field(None, description="[Optional] Area From (sqm)")
    area_to_sqm: Optional[float] = Field(None, description="[Optional] Area To (sqm)")
    amenities: Optional[List[str]] = Field(default_factory=list, description="[Optional] Amenities list")
    tags: Optional[List[str]] = Field(default_factory=list, description="[Optional] Project tags")

    # --- 4. Description PL ---
    short_description_pl: str = Field(..., description="[REQUIRED] Short Description in Polish")
    full_description_pl: Optional[str] = Field(None, description="[Optional] Full Description in Polish")

    # --- 5. Description EN ---
    short_description_en: str = Field(..., description="[REQUIRED] Short Description in English")
    full_description_en: Optional[str] = Field(None, description="[Optional] Full Description in English")

    # --- 6. Media ---
    media: List[str] = Field(..., min_length=1, description="[REQUIRED] Media image URLs (minimum 1 image required)")
    video_url: Optional[str] = Field(None, description="[Optional] Video Tour URL")

    # --- 7. Location (all optional) ---
    search_location: Optional[str] = Field(None, description="[Optional] Search Location query")
    latitude: Optional[float] = Field(None, description="[Optional] Latitude")
    longitude: Optional[float] = Field(None, description="[Optional] Longitude")
    address_en: Optional[str] = Field(None, description="[Optional] Address in English")
    address_pl: Optional[str] = Field(None, description="[Optional] Address in Polish")
    neighborhood_lifestyle: Optional[str] = Field(None, description="[Optional] Neighborhood & Lifestyle (AI)")

    # --- 8. Layouts ---
    layouts: Optional[List[Dict[str, Any]]] = Field(default_factory=list, description="[Optional] Layout configurations")

    # --- 9. SEO (all optional) ---
    seo_title_pl: Optional[str] = Field(None, description="[Optional] SEO Title (PL)")
    seo_title_en: Optional[str] = Field(None, description="[Optional] SEO Title (EN)")
    seo_description_pl: Optional[str] = Field(None, description="[Optional] SEO Description (PL)")
    seo_description_en: Optional[str] = Field(None, description="[Optional] SEO Description (EN)")

    # --- 10. Publish (all optional) ---
    active: Optional[bool] = Field(True, description="[Optional] Active / Published status")
    featured: Optional[bool] = Field(False, description="[Optional] Featured property flag")
    top_list: Optional[bool] = Field(False, description="[Optional] Top List property flag")

    # --- Compatibility / Internal Aliases ---
    property_id: Optional[str] = Field(None, description="Internal property ID override")
    cover_image: Optional[str] = Field(None, description="Cover image URL")
    price_display: Optional[str] = Field(None, description="Price display string e.g. from 18.5M THB")
    bedrooms: Optional[int] = Field(None, description="Bedrooms")
    bathrooms: Optional[int] = Field(None, description="Bathrooms")
    has_pool: Optional[bool] = Field(False)
    beach_access: Optional[bool] = Field(False)
    sea_view: Optional[bool] = Field(False)
    # Legacy location fields still sent by older clients and batch tools; without them these values were silently dropped.
    province: Optional[str] = Field(None, description="Legacy alias: province (defaults to city)")
    district: Optional[str] = Field(None, description="Legacy alias: district")
    address: Optional[str] = Field(None, description="Legacy alias: address (defaults to address_en)")
    project_name: Optional[str] = Field(None, description="Legacy alias: project / development name")
    bedrooms_min: Optional[int] = Field(None, description="Legacy alias: minimum bedrooms (validated against bedrooms_max)")
    bedrooms_max: Optional[int] = Field(None, description="Legacy alias: maximum bedrooms")

    @model_validator(mode="before")
    @classmethod
    def _reconcile_and_default_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)

        # 1. Names reconciliation
        en_name = d.get("project_name_en") or d.get("property_name") or d.get("project_name") or d.get("title")
        pl_name = d.get("project_name_pl") or en_name
        if en_name and not d.get("project_name_en"):
            d["project_name_en"] = en_name
        if pl_name and not d.get("project_name_pl"):
            d["project_name_pl"] = pl_name

        # 2. Location & Country/City
        if not d.get("country"):
            d["country"] = "Thailand"
        city_val = d.get("city") or d.get("province") or d.get("district") or d.get("area") or d.get("location")
        if city_val and not d.get("city"):
            d["city"] = str(city_val).split(",")[0].strip()

        # 3. Descriptions
        desc_en = d.get("full_description_en") or d.get("description") or d.get("short_description_en") or ""
        short_en = d.get("short_description_en") or (desc_en[:200] if desc_en else en_name or "Real estate project")
        desc_pl = d.get("full_description_pl") or d.get("description_pl") or desc_en
        short_pl = d.get("short_description_pl") or (desc_pl[:200] if desc_pl else short_en)
        if not d.get("short_description_en"):
            d["short_description_en"] = short_en
        if not d.get("short_description_pl"):
            d["short_description_pl"] = short_pl
        if desc_en and not d.get("full_description_en"):
            d["full_description_en"] = desc_en
        if desc_pl and not d.get("full_description_pl"):
            d["full_description_pl"] = desc_pl

        # 4. Media
        media_list = d.get("media") or d.get("images") or []
        if isinstance(media_list, str):
            media_list = [media_list]
        if not d.get("media") and media_list:
            d["media"] = list(media_list)
        elif not d.get("media"):
            # Provide sensible fallback image if legacy creation called without media
            d["media"] = ["https://tajlandia.com/images/default-property.jpg"]

        # 5. Pricing aliases
        if d.get("price_numeric_min") is not None and d.get("price_from") is None:
            d["price_from"] = d.get("price_numeric_min")
        if d.get("price_numeric_max") is not None and d.get("price_to") is None:
            d["price_to"] = d.get("price_numeric_max")

        # 6. Units & Area aliases
        if d.get("units") is not None and d.get("total_units") is None:
            d["total_units"] = d.get("units")
        if d.get("area_sqm_min") is not None and d.get("area_from_sqm") is None:
            d["area_from_sqm"] = d.get("area_sqm_min")
        if d.get("area_sqm_max") is not None and d.get("area_to_sqm") is None:
            d["area_to_sqm"] = d.get("area_sqm_max")

        # 7. Status alias
        if d.get("status") and not d.get("completion_status"):
            d["completion_status"] = d.get("status")

        # 8. Address alias
        if d.get("address") and not d.get("address_en"):
            d["address_en"] = d.get("address")

        # 9. Active alias
        if d.get("is_public") is not None and d.get("active") is None:
            d["active"] = d.get("is_public")

        return d


class PropertyUpdateRequest(BaseModel):
    # --- 1. Basic Info ---
    project_name_pl: Optional[str] = Field(None, description="Project Name (PL)")
    project_name_en: Optional[str] = Field(None, description="Project Name (EN)")
    property_type: Optional[str] = Field(None, description="Project Type")
    slug_pl: Optional[str] = Field(None, description="Slug (PL)")
    slug_en: Optional[str] = Field(None, description="Slug (EN)")
    subtitle_pl: Optional[str] = Field(None, description="Subtitle (PL)")
    subtitle_en: Optional[str] = Field(None, description="Subtitle (EN)")
    country: Optional[str] = Field(None, description="Country")
    city: Optional[str] = Field(None, description="City")
    websites: Optional[List[str]] = Field(None, description="Sites this project belongs to")

    # --- 2. Pricing ---
    currency: Optional[str] = Field(None, description="Currency")
    price_from: Optional[float] = Field(None, description="Price From")
    price_to: Optional[float] = Field(None, description="Price To")
    expected_roi: Optional[str] = Field(None, description="Expected ROI (%)")
    rental_yield: Optional[str] = Field(None, description="Rental Yield (%)")

    # --- 3. Details ---
    completion_status: Optional[str] = Field(None, description="Completion Status")
    completion_date: Optional[str] = Field(None, description="Completion Date")
    total_units: Optional[int] = Field(None, description="Total Units")
    available_units: Optional[int] = Field(None, description="Available Units")
    area_from_sqm: Optional[float] = Field(None, description="Area From (sqm)")
    area_to_sqm: Optional[float] = Field(None, description="Area To (sqm)")
    amenities: Optional[List[str]] = Field(None, description="Amenities")
    tags: Optional[List[str]] = Field(None, description="Tags")

    # --- 4. Description PL ---
    short_description_pl: Optional[str] = Field(None, description="Short Description (PL)")
    full_description_pl: Optional[str] = Field(None, description="Full Description (PL)")

    # --- 5. Description EN ---
    short_description_en: Optional[str] = Field(None, description="Short Description (EN)")
    full_description_en: Optional[str] = Field(None, description="Full Description (EN)")

    # --- 6. Media ---
    media: Optional[List[str]] = Field(None, description="Media images")
    video_url: Optional[str] = Field(None, description="Video URL")

    # --- 7. Location ---
    search_location: Optional[str] = Field(None, description="Search Location")
    latitude: Optional[float] = Field(None, description="Latitude")
    longitude: Optional[float] = Field(None, description="Longitude")
    address_en: Optional[str] = Field(None, description="Address (EN)")
    address_pl: Optional[str] = Field(None, description="Address (PL)")
    neighborhood_lifestyle: Optional[str] = Field(None, description="Neighborhood & Lifestyle (AI)")

    # --- 8. Layouts ---
    layouts: Optional[List[Dict[str, Any]]] = Field(None, description="Layouts")

    # --- 9. SEO ---
    seo_title_pl: Optional[str] = Field(None, description="SEO Title (PL)")
    seo_title_en: Optional[str] = Field(None, description="SEO Title (EN)")
    seo_description_pl: Optional[str] = Field(None, description="SEO Description (PL)")
    seo_description_en: Optional[str] = Field(None, description="SEO Description (EN)")

    # --- 10. Publish ---
    active: Optional[bool] = Field(None, description="Active status")
    featured: Optional[bool] = Field(None, description="Featured status")
    top_list: Optional[bool] = Field(None, description="Top List status")

    # Legacy field aliases
    property_name: Optional[str] = None
    project_name: Optional[str] = None
    status: Optional[str] = None
    location: Optional[str] = None
    province: Optional[str] = None
    district: Optional[str] = None
    address: Optional[str] = None
    price_display: Optional[str] = None
    price_numeric_min: Optional[float] = None
    price_numeric_max: Optional[float] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    area_sqm_min: Optional[float] = None
    area_sqm_max: Optional[float] = None
    has_pool: Optional[bool] = None
    beach_access: Optional[bool] = None
    sea_view: Optional[bool] = None
    description: Optional[str] = None
    images: Optional[List[str]] = None
    cover_image: Optional[str] = None
    is_public: Optional[bool] = None


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
    Directly retrieves from Cloud Firestore 'projects' (ADMIN_FIRESTORE_*).
    """
    fs_output = []
    fs_answered = False  # Firestore matched something: an empty page past the end is still Firestore's answer
    try:
        from app.services.firestore_service import search_properties_firestore
        fs_props = search_properties_firestore(
            location=location,
            property_type=property_type,
            min_price=min_price,
            max_price=max_price,
            bedrooms=bedrooms,
            has_pool=has_pool,
            websites=staff.selected_websites,
            target="admin",
            status=status,
            # No native offset in the catalog search: fetch through the end of the requested page, then slice.
            limit=offset + limit
        )
        if fs_props:
            fs_answered = True
            for p in fs_props[offset:offset + limit]:
                fs_output.append({
                    "property_id": p.get("property_id"),
                    "property_name": p.get("name") or p.get("property_name"),
                    "project_name": p.get("project_name") or p.get("name"),
                    "property_type": p.get("property_type"),
                    "status": p.get("status"),
                    "province": p.get("province"),
                    "district": p.get("district"),
                    "area": p.get("area"),
                    "price_display": p.get("price"),
                    "bedrooms": p.get("bedrooms"),
                    "has_pool": p.get("has_pool"),
                    "websites": p.get("websites") or [],
                    "images": p.get("images") or [],
                    "is_public": p.get("is_public", True)
                })
    except Exception as e:
        logger.warning(f"Error querying Firestore properties in list_admin_properties_endpoint: {e}")

    if fs_answered:
        return {
            "count": len(fs_output),
            "offset": offset,
            "limit": limit,
            "selected_websites": staff.selected_websites,
            "properties": fs_output
        }

    # Fallback to local SQL database if Firestore returned nothing (e.g. offline unit testing)
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
        img_list = [getattr(img, "image_url", img) for img in p.images] if p.images else []
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
            # Unassigned legacy properties are served under tajlandia.com (see build_property_query /
            # property_belongs_to_websites); report that here too so the list never claims a property
            # belongs to "no site" while actually returning it under a selected one.
            "websites": list(p.websites) if p.websites else ["tajlandia.com"],
            "images": img_list,
            "is_public": p.is_public
        })
    return {
        "count": len(output),
        "offset": offset,
        "limit": limit,
        "selected_websites": staff.selected_websites,
        "properties": output
    }


@router.get("/properties/{property_id}")
def get_admin_property_endpoint(
    property_id: str,
    include_archived: bool = Query(False, description="Admin / super admin only: also return a soft-deleted (archived) listing."),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Session = Depends(get_db)
):
    """
    Retrieves a single property listing from Cloud Firestore (primary) or local DB.
    Strictly verifies that the property belongs to the staff member's currently selected websites.
    Archived (soft-deleted) listings return 404 unless an admin passes include_archived=true.
    """
    from app.services.firestore_service import (
        get_property_by_id_from_firestore,
        get_raw_property_document,
        is_soft_deleted_record,
    )

    if include_archived and staff.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can view archived listings.",
        )

    # 1. Check Cloud Firestore (Primary)
    fs_prop = None
    try:
        fs_prop = get_property_by_id_from_firestore(property_id, target="admin", include_inactive=include_archived)
    except Exception:
        fs_prop = None

    prop = None
    if not fs_prop and db is not None:
        prop = db.query(Property).filter(Property.property_id == property_id).first()
        if prop is not None and not include_archived:
            try:
                raw = get_raw_property_document(property_id, target="admin")
            except Exception:
                raw = None
            # Firestore is authoritative: an archived doc hides a stale SQL row too.
            if is_soft_deleted_record(prop) or (raw is not None and is_soft_deleted_record(raw)):
                prop = None

    target_obj = fs_prop or prop
    if not target_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found."
        )
    if not property_belongs_to_websites(target_obj, staff.selected_websites):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied: Property '{property_id}' is not part of your currently selected website(s) "
                f"{staff.selected_websites}."
            )
        )

    if fs_prop:
        # Admins edit the stored record: show the stored name, not the public display-cleaned one
        # (the card presenter shortens names, e.g. drops "Residence").
        _raw = fs_prop.get("raw_firestore_data") or {}
        _stored_name = _raw.get("name") or _raw.get("title")
        if isinstance(_stored_name, dict):
            _stored_name = _stored_name.get("en") or _stored_name.get("pl") or next((v for v in _stored_name.values() if v), None)
        return {
            "found": True,
            "property_id": fs_prop.get("property_id"),
            "property_name": _stored_name or fs_prop.get("name"),
            "archived": is_soft_deleted_record(_raw),
            "project_name": fs_prop.get("project_name") or fs_prop.get("name"),
            "property_type": fs_prop.get("property_type"),
            "status": fs_prop.get("status"),
            "location": {
                "country": fs_prop.get("province"),
                "province": fs_prop.get("province"),
                "district": fs_prop.get("district"),
                "area": fs_prop.get("area"),
                "address": fs_prop.get("address")
            },
            "pricing": {
                "display": fs_prop.get("price"),
                "numeric_min": fs_prop.get("price_numeric_min"),
                "currency": fs_prop.get("currency") or "THB"
            },
            "details": {
                "bedrooms": fs_prop.get("bedrooms"),
                "bathrooms": None,
                "area": fs_prop.get("area")
            },
            "amenities": fs_prop.get("highlights") or [],
            "websites": fs_prop.get("websites") or [],
            "images": fs_prop.get("images") or [],
            "is_public": fs_prop.get("is_public", True)
        }

    images_urls = [getattr(img, "image_url", img) for img in prop.images] if prop.images else []
    return {
        "found": True,
        "property_id": prop.property_id,
        "property_name": prop.property_name,
        "project_name": prop.project_name,
        "project_name_pl": getattr(prop, "project_name_pl", None),
        "project_name_en": getattr(prop, "project_name_en", None) or prop.property_name,
        "slug_pl": getattr(prop, "slug_pl", None),
        "slug_en": getattr(prop, "slug_en", None),
        "subtitle_pl": getattr(prop, "subtitle_pl", None),
        "subtitle_en": getattr(prop, "subtitle_en", None),
        "property_type": prop.property_type,
        "status": prop.status,
        "completion_status": getattr(prop, "completion_status", None) or prop.status,
        "completion_date": getattr(prop, "completion_date", None),
        "location": {
            "country": prop.country,
            "city": getattr(prop, "city", None) or prop.province,
            "province": prop.province,
            "district": prop.district,
            "area": prop.area,
            "address": prop.address,
            "address_en": getattr(prop, "address_en", None) or prop.address,
            "address_pl": getattr(prop, "address_pl", None),
            "search_location": getattr(prop, "search_location", None),
            "neighborhood_lifestyle": getattr(prop, "neighborhood_lifestyle", None),
            "latitude": prop.latitude,
            "longitude": prop.longitude,
        },
        "pricing": {
            "display": prop.price_display,
            "price_from": float(prop.price_numeric_min) if prop.price_numeric_min is not None else None,
            "price_to": float(prop.price_numeric_max) if prop.price_numeric_max is not None else None,
            "numeric_min": float(prop.price_numeric_min) if prop.price_numeric_min is not None else None,
            "numeric_max": float(prop.price_numeric_max) if prop.price_numeric_max is not None else None,
            "currency": prop.currency,
            "expected_roi": getattr(prop, "expected_roi", None),
            "rental_yield": getattr(prop, "rental_yield", None),
        },
        "details": {
            "bedrooms": prop.bedrooms_display,
            "bathrooms": prop.bathrooms,
            "area": prop.area_display,
            "total_units": getattr(prop, "units", None),
            "available_units": getattr(prop, "available_units", None),
            "area_from_sqm": float(prop.area_sqm_min) if getattr(prop, "area_sqm_min", None) is not None else None,
            "area_to_sqm": float(prop.area_sqm_max) if getattr(prop, "area_sqm_max", None) is not None else None,
        },
        "descriptions": {
            "short_description_pl": getattr(prop, "short_description_pl", None),
            "full_description_pl": getattr(prop, "description_pl", None),
            "short_description_en": getattr(prop, "short_description_en", None),
            "full_description_en": getattr(prop, "description_en", None) or prop.description,
        },
        "media": images_urls,
        "images": images_urls,
        "video_url": getattr(prop, "video_url", None),
        "layouts": getattr(prop, "layouts", []) or [],
        "seo": {
            "seo_title_pl": getattr(prop, "seo_title_pl", None),
            "seo_title_en": getattr(prop, "seo_title_en", None),
            "seo_description_pl": getattr(prop, "seo_description_pl", None),
            "seo_description_en": getattr(prop, "seo_description_en", None),
        },
        "publish": {
            "active": prop.is_public,
            "featured": getattr(prop, "is_featured", False),
            "top_list": getattr(prop, "is_top_list", False),
        },
        "amenities": prop.amenities,
        "tags": getattr(prop, "tags", []) or [],
        "websites": prop.websites or [],
        "is_public": prop.is_public
    }


def _property_scope_target(property_id: str, db: Session):
    """Raw Firestore document or SQL row used for existence and website checks."""
    from app.services.firestore_service import get_raw_property_document
    raw = None
    try:
        raw = get_raw_property_document(property_id, target="admin")
    except Exception:
        raw = None
    prop = db.query(Property).filter(Property.property_id == property_id).first() if db is not None else None
    return raw, prop


@router.post("/properties", status_code=status.HTTP_201_CREATED)
def create_property_endpoint(
    req: PropertyCreateRequest,
    overwrite: Optional[bool] = Query(None, description="Not supported. Use PUT to update an existing property."),
    allow_without_undo: bool = Query(False, description="Admin only: write even if the undo snapshot cannot be saved."),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db)
):
    """
    Create a property in Postgres and Firestore after validation.
    Workers are read-only. A duplicate id or name returns 409.
    """
    import uuid
    from app.services.action_service import rest_create_property

    if overwrite is True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="overwrite=true is not supported; use update",
        )

    pid = req.property_id or f"prop-{uuid.uuid4().hex[:8]}"

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

    fields = req.model_dump(exclude={"images", "media", "cover_image", "property_id"})
    images_list = getattr(req, "media", None) or getattr(req, "images", None) or []
    # Reconcile aliases into standard DB field names
    fields["property_name"] = req.project_name_en or fields.get("property_name") or req.project_name_pl
    fields["project_name"] = req.project_name or req.project_name_en
    fields["province"] = req.province or req.city or "Phuket"
    fields["description"] = req.full_description_en or req.short_description_en or fields.get("description")
    # Map the bilingual request fields onto the stored columns. `status` is required by validation and is what
    # search and the chatbot read; without this every create failed with 422 "status is required".
    if req.completion_status:
        fields["status"] = req.completion_status
    if req.address_en and not req.address:
        fields["address"] = req.address_en
    if req.full_description_pl is not None:
        fields["description_pl"] = req.full_description_pl
    if req.full_description_en is not None:
        fields["description_en"] = req.full_description_en
    if req.area_from_sqm is not None:
        fields["area_sqm_min"] = req.area_from_sqm
    if req.area_to_sqm is not None:
        fields["area_sqm_max"] = req.area_to_sqm
    if req.total_units is not None:
        fields["units"] = req.total_units
    if req.price_from is not None and fields.get("price_numeric_min") is None:
        fields["price_numeric_min"] = req.price_from
    if req.price_to is not None and fields.get("price_numeric_max") is None:
        fields["price_numeric_max"] = req.price_to
    if req.active is not None:
        fields["is_public"] = bool(req.active)
    if req.featured is not None:
        fields["is_featured"] = bool(req.featured)
    if req.top_list is not None:
        fields["is_top_list"] = bool(req.top_list)
    return rest_create_property(
        db,
        staff,
        fields,
        property_id=pid,
        websites=assigned_websites,
        images=images_list,
        cover_image=req.cover_image,
        allow_without_undo=allow_without_undo,
    )


@router.get("/properties/{property_id}/consistency")
def property_consistency_endpoint(
    property_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    """Admin-only comparison of the Firestore document and the Postgres row."""
    from app.services.action_service import rest_property_consistency

    raw, prop = _property_scope_target(property_id, db)
    target_obj = raw or prop
    if not target_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found.",
        )
    if not property_belongs_to_websites(target_obj, staff.selected_websites):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied: Property '{property_id}' does not belong to your currently selected website(s) "
                f"{staff.selected_websites}."
            ),
        )
    return rest_property_consistency(db, property_id)


@router.put("/properties/{property_id}")
def update_property_endpoint(
    property_id: str,
    req: PropertyUpdateRequest,
    allow_without_undo: bool = Query(False),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db)
):
    """
    Update a property that already exists in Firestore, Postgres, or both.
    A Postgres-only row is not given a nameless Firestore stub.
    """
    from app.services.action_service import rest_update_property

    raw, prop = _property_scope_target(property_id, db)
    target_obj = raw or prop
    if not target_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found."
        )
    if not property_belongs_to_websites(target_obj, staff.selected_websites):
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

    update_data = req.model_dump(exclude_unset=True, exclude={"images", "media", "cover_image"})
    images_list = req.media if req.media is not None else req.images
    if req.project_name_en is not None and "property_name" not in update_data:
        update_data["property_name"] = req.project_name_en
    if req.city is not None and "province" not in update_data:
        update_data["province"] = req.city
    if req.price_from is not None and "price_numeric_min" not in update_data:
        update_data["price_numeric_min"] = req.price_from
    if req.price_to is not None and "price_numeric_max" not in update_data:
        update_data["price_numeric_max"] = req.price_to
    if req.completion_status is not None and "status" not in update_data:
        update_data["status"] = req.completion_status
    if req.address_en is not None and "address" not in update_data:
        update_data["address"] = req.address_en
    if req.full_description_en is not None and "description_en" not in update_data:
        update_data["description_en"] = req.full_description_en
    if req.full_description_pl is not None and "description_pl" not in update_data:
        update_data["description_pl"] = req.full_description_pl
    if req.full_description_en is not None and "description" not in update_data:
        update_data["description"] = req.full_description_en
    elif req.short_description_en is not None and "description" not in update_data:
        update_data["description"] = req.short_description_en
    if req.area_from_sqm is not None and "area_sqm_min" not in update_data:
        update_data["area_sqm_min"] = req.area_from_sqm
    if req.area_to_sqm is not None and "area_sqm_max" not in update_data:
        update_data["area_sqm_max"] = req.area_to_sqm
    if req.total_units is not None and "units" not in update_data:
        update_data["units"] = req.total_units
    if req.active is not None and "is_public" not in update_data:
        update_data["is_public"] = bool(req.active)
    if req.featured is not None and "is_featured" not in update_data:
        update_data["is_featured"] = bool(req.featured)
    if req.top_list is not None and "is_top_list" not in update_data:
        update_data["is_top_list"] = bool(req.top_list)
    return rest_update_property(
        db,
        staff,
        property_id,
        update_data,
        websites=assigned_websites,
        images=images_list,
        cover_image=req.cover_image,
        allow_without_undo=allow_without_undo,
    )


@router.delete("/properties/{property_id}")
def delete_property_endpoint(
    property_id: str,
    hard_delete: bool = False,
    confirm_property_id: Optional[str] = Query(
        None,
        description="Required when hard_delete=true. Must equal the property id.",
    ),
    allow_without_undo: bool = Query(False),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db)
):
    """
    Soft-delete (archive) a property, or permanently delete it when hard_delete=true
    and confirm_property_id matches the property id.
    """
    from app.services.action_service import rest_delete_property

    raw, prop = _property_scope_target(property_id, db)
    target_obj = raw or prop
    if not target_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with ID '{property_id}' not found."
        )
    if not property_belongs_to_websites(target_obj, staff.selected_websites):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied: Property '{property_id}' does not belong to your currently selected website(s) "
                f"{staff.selected_websites}."
            )
        )

    return rest_delete_property(
        db,
        staff,
        property_id,
        hard_delete=hard_delete,
        confirm_property_id=confirm_property_id,
        allow_without_undo=allow_without_undo,
    )


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
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
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
    target_id: Optional[str] = Query(None, description="Filter by entity ID (e.g. property slug or user email)"),
    updated_by: Optional[str] = Query(None, description="Filter by admin email/ID who performed the update"),
    limit: int = Query(50, ge=1, le=500, description="Page limit"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
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
    block_admin_user,
    AdminUserDirectoryItem,
    CreateUserRequest,
    UpdateUserRequest
)

def _brochure_config_labels() -> Dict[str, Any]:
    """
    Non-secret brochure config labels, merged into /system/config-status.
    Never returns API keys or secrets.
    """
    llm_on = bool(getattr(settings, "BROCHURE_LLM_ENABLED", False)) and bool(
        (getattr(settings, "ANTHROPIC_API_KEY", None) or "").strip()
    )
    return {
        "brochure_llm": "on" if llm_on else "off",
        "brochure_llm_enabled": bool(getattr(settings, "BROCHURE_LLM_ENABLED", False)),
        "brochure_llm_model": str(
            getattr(settings, "BROCHURE_LLM_MODEL", "claude-sonnet-5-5") or "claude-sonnet-5-5"
        ),
        "brochure_engine": str(getattr(settings, "BROCHURE_ENGINE", "reportlab") or "reportlab"),
        "openai_model_label": str(getattr(settings, "ANTHROPIC_MODEL", "") or "claude-opus-5-5"),
    }


@router.get("/brochure/preview", tags=["Admin Brochure"])
def admin_brochure_preview(
    property_id: str = Query(..., description="Property / project id"),
    template: int = Query(1, ge=1, le=7, description="Visual theme 1–7"),
    lang: str = Query("en", description="Language: en, pl, or th"),
    site_origin: Optional[str] = Query(None, description="Tenant site origin"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Session = Depends(get_db),
):
    """
    Admin-only design preview: return a single-property brochure PDF for a chosen theme.
    Does not change the public chat / offer dossier paths.
    """
    from fastapi.responses import Response

    from app.services.brochure_data import load_brochure_data
    from app.services.brochure_renderer import render_brochure_pdf
    from app.services.brochure_themes import get_theme

    language = lang if lang in ("en", "pl", "th") else "en"
    try:
        data = load_brochure_data(
            db=db,
            property_id=property_id,
            site_origin=site_origin,
            language=language,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    theme = get_theme(template)
    logger.info(
        "brochure_theme id=%s name=%s property=%s lang=%s preview=1",
        theme.id,
        theme.name,
        data.property_id or property_id,
        language,
    )
    try:
        # Deterministic preview (no Sonnet) for fast design review
        pdf_bytes = render_brochure_pdf(
            data,
            use_cache=False,
            copy=None,
            ai_footer=None,
            theme_id=template,
        )
    except Exception as e:
        logger.error("Admin brochure preview failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Brochure preview failed") from e

    filename = f"brochure_preview_{property_id}_t{template}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ==============================================================================
# 10. Admin Answer Corrections (direct, without chat log remark)
# ==============================================================================

VALID_CORRECTION_SCOPES = ("global", "site_specific", "admin_only")
VALID_CORRECTION_CATEGORIES = ("property_fact", "agency_policy", "pricing_rule", "contact_info", "clarification")


class AdminCorrectionCreateRequest(BaseModel):
    trigger_query: str = Field(..., min_length=3, max_length=2000,
                              description="The user query this correction applies to")
    original_bot_answer: Optional[str] = Field(None,
                                              description="The original bot answer (read-only snapshot)")
    corrected_answer: str = Field(..., min_length=3, max_length=5000,
                                 description="The ground-truth corrected answer")
    scope: str = Field("global", description="'global', 'site_specific', or 'admin_only'")
    category: str = Field("clarification",
                         description="'property_fact', 'agency_policy', 'pricing_rule', 'contact_info', 'clarification'")
    expires_at: Optional[str] = Field(None, description="Optional ISO 8601 expiry date")


@router.post("/corrections", tags=["Admin Chat Logs"], status_code=status.HTTP_201_CREATED)
def create_admin_correction(
    body: AdminCorrectionCreateRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Session = Depends(get_db)
):
    """
    Create a new answer correction directly (without going through a chat log remark).
    Immediately proposes it for review. Super admins are auto-approved inline.
    """
    from app.services import chat_log_remarks as remarks_svc

    scope_val = (body.scope or "global").strip()
    category_val = (body.category or "clarification").strip()

    if scope_val not in VALID_CORRECTION_SCOPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"scope must be one of {VALID_CORRECTION_SCOPES}."
        )
    if category_val not in VALID_CORRECTION_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"category must be one of {VALID_CORRECTION_CATEGORIES}."
        )

    try:
        correction = remarks_svc.create_correction_direct(
            staff=staff,
            trigger_query=body.trigger_query,
            corrected_answer=body.corrected_answer,
            original_answer=body.original_bot_answer,
            scope_type=scope_val,
            category=category_val,
            expires_at=body.expires_at,
        )
    except remarks_svc.RemarkError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    # Super admins auto-approve the correction immediately
    if staff.role == "super_admin":
        try:
            correction = remarks_svc.decide_correction_direct(
                db=db,
                staff=staff,
                correction_id=correction["correction_id"],
                decision="approve",
                note="Auto-approved by super_admin on creation.",
            )
        except remarks_svc.RemarkError as exc:
            # Non-fatal: correction was created but not auto-approved
            logger.warning("Auto-approve failed for correction %s: %s", correction.get("correction_id"), exc.message)

    return {
        "correction_id": correction.get("correction_id"),
        "status": correction.get("status"),
        **correction,
    }


router.get("/users", response_model=List[AdminUserDirectoryItem], tags=["Admin Directory Management"])(list_admin_users_with_google_status)
router.post("/users", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Admin Directory Management"])(create_admin_user)
router.get("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(get_admin_user_details)
router.patch("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(update_admin_user)
router.put("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(update_admin_user)
router.delete("/users/{user_id}", tags=["Admin Directory Management"])(delete_admin_user)
router.post("/users/{user_id}/block", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(block_admin_user)
