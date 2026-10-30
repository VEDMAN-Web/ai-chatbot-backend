import json
import os
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List, Generator
from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from app.core.deps import get_db
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
    confirmation_token: Optional[str] = Field(None, description="Required when action is confirm")
    attached_images: Optional[List[str]] = Field(
        None,
        max_length=10,
        description="Image URLs already uploaded by this admin chat. They are not attached until the change is confirmed.",
    )


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


# 1. Admin Chat (Unified Action & Chatbot Endpoint)
@router.post("/chat", response_model=None)
def admin_chat_endpoint(
    req: AdminChatRequest,
    request: Request,
    stream: bool = Query(False, description="If true, return Server-Sent Events (status then final verified text)"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Any = Depends(get_db)
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
    from app.core.rate_limit import enforce_rate_limit

    user_msg = (req.message or "").strip()
    enforce_rate_limit(
        request,
        scope="admin_chat",
        identities=[staff.email],
        limit=int(settings.RATE_LIMIT_ADMIN_CHAT),
        window_seconds=int(settings.RATE_LIMIT_ADMIN_CHAT_WINDOW_SEC),
        message="Too many admin chat requests. Try again later.",
    )
    if "http://" in user_msg.lower() or "https://" in user_msg.lower():
        enforce_rate_limit(
            request,
            scope="url_import",
            identities=[staff.email],
            limit=int(settings.RATE_LIMIT_IMPORT_AND_SUPERADMIN),
            window_seconds=int(settings.RATE_LIMIT_IMPORT_WINDOW_SEC),
            message="Too many URL import requests. Try again later.",
        )
    if not user_msg and not req.action and not req.attached_images:
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

    def _run() -> Dict[str, Any]:
        return process_admin_chat(
            db=db,
            session_id=req.session_id,
            message=user_msg,
            staff_user=staff,
            site_origin=active_origin,
            requested_language=req.language,
            action=req.action,
            pending_action_id=req.pending_action_id,
            confirmation_token=req.confirmation_token,
            attached_images=list(req.attached_images or []) or None,
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
            "confirmation_token": res.get("confirmation_token"),
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
    db: Any = Depends(get_db)
):
    """
    Retrieves full admin multi-turn conversation history from Firestore.
    Includes tool invocations, results, and action proposals.
    Strictly isolated from public chatbot conversations.
    """
    from app.services.conversation_service import get_or_create_conversation

    conv = get_or_create_conversation(
        db=db,
        session_id=session_id,
        agent_type="admin",
    )
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
    db: Any = Depends(get_db)
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


@router.post("/property-images/upload")
def upload_property_images_endpoint(
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
):
    """
    Store 1 to 10 property images. This does not attach them to a listing.
    The admin chat command proposes that change, and confirm writes it.
    """
    _limit_admin_write(request, staff)
    from app.services.admin_image_storage import store_uploads

    stored = store_uploads(list(files or []))
    return {"files": stored}


@router.get("/property-images/files/{filename}")
def serve_property_image_endpoint(filename: str):
    """Public image URL used by the admin chat and the public chatbot."""
    from fastapi.responses import Response

    from app.services.admin_image_storage import load_property_image

    try:
        content, media = load_property_image(filename)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")
    return Response(content=content, media_type=media)


def _limit_admin_write(request: Optional[Request], staff: AuthenticatedStaff) -> None:
    from app.core.rate_limit import enforce_rate_limit

    enforce_rate_limit(
        request,
        scope="admin_write",
        identities=[getattr(staff, "email", None)],
        limit=int(settings.RATE_LIMIT_ADMIN_WRITE),
        window_seconds=int(settings.RATE_LIMIT_ADMIN_WRITE_WINDOW_SEC),
        message="Too many admin write requests. Try again later.",
    )


@router.post("/actions/{pending_action_id}/confirm")
def confirm_pending_action_endpoint(
    pending_action_id: str,
    request: Request,
    body: Optional[ActionConfirmRequest] = None,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Any = Depends(get_db),
):
    """
    Explicit Two-Step Confirmation endpoint for write actions.
    Validates token, server-side expiration, and permissions, then executes the action.
    """
    _limit_admin_write(request, staff)
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
    db: Any = Depends(get_db)
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
    db: Any = Depends(get_db)
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
        # Add/Create from an offer-style document: show a preview and wait for the admin's confirmation.
        if operation in ("create", "write"):
            from app.services.admin_offer_import import OFFER_EXTENSIONS, stage_document_import

            staged = stage_document_import(db, staff, content, filename)
            if staged is not None:
                return staged
            if filename.lower().rsplit(".", 1)[-1] in OFFER_EXTENSIONS:
                # A PDF/Word/text file that is not a property list: say so instead of treating every line as a property row.
                raise BatchOperationError(
                    "I could not find any property sections in this document, so nothing was created. "
                    "For Add/Create upload a CSV or Excel file with the columns name, property_type, province, "
                    "price and status, or an offer with one section per property (title, location, price in THB, status)."
                )
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
    request: Request,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Any = Depends(get_db),
):
    """
    Instantly rolls back a previous batch operation within its 6-day retention window.
    Restores the exact document states from the saved snapshot.
    """
    _limit_admin_write(request, staff)
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
    limit: int = 50,
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

class PropertyCreateRequest(BaseModel):
    property_id: Optional[str] = Field(None, example="villa-horizon-phuket", description="Unique property ID. If omitted, will be auto-generated.")
    property_name: Optional[str] = Field(None, example="Villa Horizon Luxury Residences", description="Property title/name")
    project_name: Optional[str] = Field(None, example="Villa Horizon")
    property_type: Optional[str] = Field(None, example="Villa", description="'Villa', 'Condominium', 'Townhouse'")
    status: Optional[str] = Field(None, example="Ready to move in", description="'Ready to move in', 'Under construction', 'Off-plan'")
    location: Optional[str] = Field("Phuket, Thailand", example="Bang Tao, Phuket")
    province: Optional[str] = Field("Phuket", example="Phuket")
    district: Optional[str] = Field(None, example="Thalang")
    address: Optional[str] = Field(None, example="123 Lagoon Road, Choeng Thale")
    price_display: Optional[str] = Field(None, example="from 18.5 million THB")
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
    images: Optional[List[str]] = Field(default_factory=list, example=["https://varsoviaestate.com/media/pool.jpg", "https://varsoviaestate.com/media/bedroom.jpg"])
    cover_image: Optional[str] = Field(None, example="https://varsoviaestate.com/media/pool.jpg")
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
    images: Optional[List[str]] = Field(None, example=["https://varsoviaestate.com/media/new-image.jpg"])
    cover_image: Optional[str] = Field(None, example="https://varsoviaestate.com/media/new-cover.jpg")
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
    db: Any = Depends(get_db)
):
    """
    Fetches property listings strictly scoped to the staff member's currently selected websites.
    Directly retrieves from Cloud Firestore 'projects' (ADMIN_FIRESTORE_*).
    """
    fs_output = []
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
            limit=limit
        )
        if fs_props:
            for p in fs_props:
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

    if fs_output:
        return {
            "count": len(fs_output),
            "selected_websites": staff.selected_websites,
            "properties": fs_output
        }
    return {
        "count": 0,
        "selected_websites": staff.selected_websites,
        "properties": []
    }


@router.get("/properties/{property_id}")
def get_admin_property_endpoint(
    property_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_employee_with_sites),
    db: Any = Depends(get_db)
):
    """
    Retrieves a single property listing from Cloud Firestore.
    Strictly verifies that the property belongs to the staff member's currently selected websites.
    """
    # 1. Check Cloud Firestore (Primary)
    fs_prop = None
    try:
        from app.services.firestore_service import get_property_by_id_from_firestore
        fs_prop = get_property_by_id_from_firestore(property_id, target="admin")
    except Exception:
        fs_prop = None

    target_obj = fs_prop
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
        return {
            "found": True,
            "property_id": fs_prop.get("property_id"),
            "property_name": fs_prop.get("name"),
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


def _property_scope_target(property_id: str, db: Any):
    """Raw Firestore document used for existence and website checks (None when missing)."""
    from app.services.firestore_service import get_raw_property_document
    try:
        return get_raw_property_document(property_id, target="admin")
    except Exception:
        return None


@router.post("/properties", status_code=status.HTTP_201_CREATED)
def create_property_endpoint(
    req: PropertyCreateRequest,
    request: Request,
    overwrite: Optional[bool] = Query(None, description="Not supported. Use PUT to update an existing property."),
    allow_without_undo: bool = Query(False, description="Admin only: write even if the undo snapshot cannot be saved."),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Any = Depends(get_db),
):
    """
    Create a property in Firestore after validation.
    Workers are read-only. A duplicate id or name returns 409.
    """
    _limit_admin_write(request, staff)
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

    fields = req.model_dump(exclude={"images", "cover_image", "property_id"})
    return rest_create_property(
        db,
        staff,
        fields,
        property_id=pid,
        websites=assigned_websites,
        images=req.images or [],
        cover_image=req.cover_image,
        allow_without_undo=allow_without_undo,
    )


@router.put("/properties/{property_id}")
def update_property_endpoint(
    property_id: str,
    req: PropertyUpdateRequest,
    request: Request,
    allow_without_undo: bool = Query(False),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Any = Depends(get_db),
):
    """
    Update a property that already exists in Firestore.
    """
    _limit_admin_write(request, staff)
    from app.services.action_service import rest_update_property

    target_obj = _property_scope_target(property_id, db)
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

    update_data = req.model_dump(exclude_unset=True, exclude={"images", "cover_image"})
    return rest_update_property(
        db,
        staff,
        property_id,
        update_data,
        websites=assigned_websites,
        images=req.images,
        cover_image=req.cover_image,
        allow_without_undo=allow_without_undo,
    )


@router.delete("/properties/{property_id}")
def delete_property_endpoint(
    property_id: str,
    request: Request,
    hard_delete: bool = False,
    confirm_property_id: Optional[str] = Query(
        None,
        description="Required when hard_delete=true. Must equal the property id.",
    ),
    allow_without_undo: bool = Query(False),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Any = Depends(get_db),
):
    """
    Soft-delete (archive) a property, or permanently delete it when hard_delete=true
    and confirm_property_id matches the property id.
    """
    _limit_admin_write(request, staff)
    from app.services.action_service import rest_delete_property
    if hard_delete:
        if (staff.role or "").lower() != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permanent delete is restricted to super admins. Use archive instead.",
            )
        if not getattr(settings, "ALLOW_HARD_DELETE", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permanent delete is disabled on this environment (ALLOW_HARD_DELETE is off).",
            )

    target_obj = _property_scope_target(property_id, db)
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
# Taught answers (Module 2): FAQ answers the public bot serves before any model call
# ==============================================================================

class TaughtAnswerCreate(BaseModel):
    question: str
    answer: str
    variants: List[str] = Field(default_factory=list)
    sites: List[str] = Field(default_factory=list, description="Empty = all websites")
    language: str = "any"
    audience: str = Field("public", description="public | admin | both")


class TaughtAnswerUpdate(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    variants: Optional[List[str]] = None
    sites: Optional[List[str]] = None
    language: Optional[str] = None
    audience: Optional[str] = None
    status: Optional[str] = None


def _taught_call(fn, *args, **kwargs):
    from app.services.taught_answers import TaughtAnswerError

    try:
        return fn(*args, **kwargs)
    except TaughtAnswerError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Taught answer not found.")
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/taught-answers", tags=["Taught Answers"])
def list_taught_answers_endpoint(
    status_filter: Optional[str] = Query(None, alias="status"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    from app.services import taught_answers

    return {"items": _taught_call(taught_answers.list_answers, status=status_filter)}


@router.post("/taught-answers", status_code=status.HTTP_201_CREATED, tags=["Taught Answers"])
def create_taught_answer_endpoint(
    body: TaughtAnswerCreate,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    from app.services import taught_answers

    return _taught_call(
        taught_answers.create,
        question=body.question, answer=body.answer, variants=body.variants,
        sites=body.sites, language=body.language, audience=body.audience, created_by=staff.email,
    )


@router.put("/taught-answers/{answer_id}", tags=["Taught Answers"])
def update_taught_answer_endpoint(
    answer_id: str,
    body: TaughtAnswerUpdate,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    from app.services import taught_answers

    return _taught_call(
        taught_answers.update, answer_id,
        question=body.question, answer=body.answer, variants=body.variants,
        sites=body.sites, language=body.language, audience=body.audience, status=body.status,
        updated_by=staff.email,
    )


@router.delete("/taught-answers/{answer_id}", tags=["Taught Answers"])
def archive_taught_answer_endpoint(
    answer_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    """Archive (never hard delete) so the answer and its history stay recoverable."""
    from app.services import taught_answers

    return _taught_call(taught_answers.archive, answer_id, updated_by=staff.email)


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
    source: str = Query("all", description="'firestore' or 'all'"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Any = Depends(get_db)
):
    """
    Returns full administrative activity logs.
    Includes tool executions, staff chat sessions, database mutations, and auth actions.
    Reads from Cloud Firestore.
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
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Any = Depends(get_db)
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
        "openai_model_label": str(getattr(settings, "OPENAI_MODEL", "") or ""),
    }


@router.get("/brochure/preview", tags=["Admin Brochure"])
def admin_brochure_preview(
    property_id: str = Query(..., description="Property / project id"),
    template: int = Query(1, ge=1, le=7, description="Visual theme 1–7"),
    lang: str = Query("en", description="Language: en, pl, or th"),
    site_origin: Optional[str] = Query(None, description="Tenant site origin"),
    staff: AuthenticatedStaff = Depends(require_admin_or_employee),
    db: Any = Depends(get_db),
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


router.get("/users", response_model=List[AdminUserDirectoryItem], tags=["Admin Directory Management"])(list_admin_users_with_google_status)
router.post("/users", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Admin Directory Management"])(create_admin_user)
router.get("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(get_admin_user_details)
router.patch("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(update_admin_user)
router.put("/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory Management"])(update_admin_user)
router.delete("/users/{user_id}", tags=["Admin Directory Management"])(delete_admin_user)
