"""
Admin API: Google Drive folder bulk ingestion (all supported formats).

Nothing here writes listings except POST /jobs/{job_id}/approve, which needs confirm=true from an admin.
"""
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedStaff, require_admin_or_super_admin_with_sites
from app.db.session import get_db
from app.services import drive_folder_ingest as ingest
from app.services import drive_formats as fmts

router = APIRouter(prefix="/admin/drive-ingestion", tags=["Admin Drive Ingestion"])


class CreateJobRequest(BaseModel):
    folder_url: str = Field(..., description="Google Drive folder URL", example="https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUv")
    recursive: Optional[bool] = Field(None, description="Include subfolders (default from DRIVE_INGEST_RECURSIVE_DEFAULT)")
    batch_size: Optional[int] = Field(None, ge=1, description="Files per batch (default 10, max DRIVE_INGEST_MAX_BATCH_SIZE)")
    websites: Optional[List[str]] = Field(None, description="Target websites for approved listings (default: selected websites)")


class RetryRequest(BaseModel):
    include_skipped_ocr: bool = Field(False, description="Also re-queue files skipped because OCR was off")


class DraftUpdateRequest(BaseModel):
    fields: Optional[Dict[str, Any]] = Field(None, description="Corrected values; recorded as admin-provided")
    action: Optional[str] = Field(None, description="Override the proposed action: create, update or skip")
    resolve_decisions: Optional[List[str]] = Field(None, description="Fields whose flagged value the admin accepts (e.g. a non-THB price)")
    reject: bool = Field(False, description="Reject the draft (it will never be saved)")


class ApproveRequest(BaseModel):
    draft_ids: Optional[List[str]] = Field(None, description="Drafts to save")
    all_ready: bool = Field(False, description="Approve every draft whose action is create/update without open issues")
    confirm: bool = Field(False, description="Must be true. Explicit confirmation that these drafts may be written")


class RollbackRequest(BaseModel):
    confirm: bool = Field(False, description="Must be true. Undo every approved draft of the job")


def _http(exc: ingest.IngestionError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/formats")
def supported_formats(staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites)):
    """Formats, how each is processed, and formats reported as unsupported."""
    return {
        "supported": [
            {"format": "pdf", "extensions": [".pdf"], "mime_types": ["application/pdf"], "processing": "text per page; scanned pages via OCR when enabled"},
            {"format": "docx", "extensions": [".docx"], "mime_types": [fmts.MIME_DOCX], "processing": "paragraphs, headings, tables, headers, footers"},
            {"format": "google_doc", "mime_types": [fmts.GOOGLE_DOC], "processing": "exported as .docx, then parsed like Word"},
            {"format": "xlsx", "extensions": [".xlsx"], "mime_types": [fmts.MIME_XLSX], "processing": "every worksheet, header row, one record per row (cached values; formulas never run)"},
            {"format": "xls", "extensions": [".xls"], "mime_types": ["application/vnd.ms-excel"], "processing": "every worksheet via xlrd"},
            {"format": "csv", "extensions": [".csv", ".tsv"], "mime_types": ["text/csv"], "processing": "delimiter detection, one record per row"},
            {"format": "google_sheet", "mime_types": [fmts.GOOGLE_SHEET], "processing": "exported as .xlsx (all sheets), then parsed like Excel"},
            {"format": "pptx", "extensions": [".pptx"], "mime_types": [fmts.MIME_PPTX], "processing": "titles, text, tables and notes per slide"},
            {"format": "google_slides", "mime_types": [fmts.GOOGLE_SLIDES], "processing": "exported as .pptx, then parsed like PowerPoint"},
            {"format": "txt/md/rtf", "extensions": [".txt", ".md", ".rtf"], "mime_types": ["text/plain", "text/markdown", "application/rtf"], "processing": "text blocks; markdown headings and tables; RTF control words removed"},
            {"format": "html", "extensions": [".html", ".htm"], "mime_types": ["text/html"], "processing": "visible text, headings, tables, meta, JSON-LD; scripts ignored"},
            {"format": "json", "extensions": [".json"], "mime_types": ["application/json"], "processing": "objects or arrays of listings -> records"},
            {"format": "xml", "extensions": [".xml"], "mime_types": ["application/xml", "text/xml"], "processing": "repeated elements -> records (defusedxml; DTD/entities refused)"},
            {"format": "image", "extensions": [".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif", ".bmp"], "processing": "OCR via Claude vision when DRIVE_INGEST_OCR_ENABLED"},
            {"format": "zip", "extensions": [".zip"], "mime_types": ["application/zip"], "processing": "supported members extracted in memory with size, count and ratio limits; no nesting"},
        ],
        "unsupported": [{"format": k, "reason": v} for k, v in fmts.UNSUPPORTED_REASONS.items()]
        + [{"format": "encrypted / password-protected files", "reason": "reported as encrypted; no password workflow"},
           {"format": "executables and scripts", "reason": "never opened"},
           {"format": "other Google Workspace types (Forms, Drawings, Sites, shortcuts)", "reason": "not exportable as documents"}],
        "ocr_enabled": bool(ingest._ocr_available()),
        "limits": {
            "default_batch_size": ingest.default_batch_size(),
            "max_batch_size": ingest.max_batch_size(),
            "max_file_bytes": fmts.max_file_bytes(),
        },
    }


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_ingestion_job(
    body: CreateJobRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    """Validate the folder, discover files and start processing in the background. Returns counts by format."""
    try:
        job = ingest.create_job(db, staff, body.folder_url, recursive=body.recursive, batch_size=body.batch_size,
                                websites=body.websites, idempotency_key=(idempotency_key or None))
    except ingest.IngestionError as exc:
        raise _http(exc)
    return ingest.job_view(job)


@router.get("/jobs")
def list_ingestion_jobs(
    limit: int = Query(20, ge=1, le=100),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    try:
        store = ingest.get_store()
    except ingest.IngestionError as exc:
        raise _http(exc)
    jobs = store.jobs_by("staff_email", staff.email)
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return {"jobs": [ingest.job_view(j) for j in jobs[:limit]]}


@router.get("/jobs/{job_id}")
def get_ingestion_job(
    job_id: str,
    include_files: bool = Query(False),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    try:
        return ingest.job_view(ingest.load_job(job_id, staff), include_files=include_files)
    except ingest.IngestionError as exc:
        raise _http(exc)


@router.get("/jobs/{job_id}/files")
def list_ingestion_files(
    job_id: str,
    status_filter: Optional[str] = Query(None, alias="status", description="pending, processing, completed, partially_completed, failed, skipped"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    try:
        ingest.load_job(job_id, staff)
    except ingest.IngestionError as exc:
        raise _http(exc)
    files = [ingest.file_view(f) for f in ingest.get_store().files(job_id)]
    if status_filter:
        files = [f for f in files if f["status"] == status_filter]
    return {"job_id": job_id, "files": files}


@router.get("/jobs/{job_id}/events")
def stream_ingestion_progress(
    job_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    """Server-sent events: a `progress` event whenever counts change, `done` when the job stops."""
    try:
        ingest.load_job(job_id, staff)
    except ingest.IngestionError as exc:
        raise _http(exc)

    def events():
        last = None
        started = time.monotonic()
        while time.monotonic() - started < 1800:
            job = ingest.get_store().get_job(job_id) or {}
            snapshot = {k: job.get(k) for k in ("status", "progress_percent", "files_done", "total_files", "counts",
                                                 "batches_done", "batches_total", "current_batch", "draft_counts", "error")}
            if snapshot != last:
                yield f"event: progress\ndata: {json.dumps(snapshot, default=str)}\n\n"
                last = snapshot
            if job.get("status") not in ingest.ACTIVE_JOB_STATUSES:
                yield f"event: done\ndata: {json.dumps(snapshot, default=str)}\n\n"
                return
            yield ": keep-alive\n\n"
            time.sleep(2)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/jobs/{job_id}/retry")
def retry_ingestion_job(
    job_id: str,
    body: Optional[RetryRequest] = None,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    """Re-process failed files only. Completed files are not reprocessed."""
    try:
        res = ingest.retry_job(db, staff, job_id, include_skipped_ocr=bool(body and body.include_skipped_ocr))
    except ingest.IngestionError as exc:
        raise _http(exc)
    return {"job": ingest.job_view(res["job"]), "requeued": res["requeued"], "attempts_exhausted": res["attempts_exhausted"]}


@router.post("/jobs/{job_id}/resume")
def resume_ingestion_job(
    job_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    try:
        res = ingest.resume_job(db, staff, job_id)
    except ingest.IngestionError as exc:
        raise _http(exc)
    return {"job": ingest.job_view(res["job"]), "resumed": res["resumed"], "message": res["message"]}


@router.post("/jobs/{job_id}/cancel")
def cancel_ingestion_job(
    job_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    try:
        return ingest.job_view(ingest.cancel_job(db, staff, job_id))
    except ingest.IngestionError as exc:
        raise _http(exc)


@router.get("/jobs/{job_id}/drafts")
def list_ingestion_drafts(
    job_id: str,
    action: Optional[str] = Query(None, description="create, update, skip or manual_review"),
    review_status: Optional[str] = Query(None, description="pending_review, approved, rejected, failed"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    """The review screen data: values, evidence and source per field, confidence, validation, duplicates, conflicts,
    the matched existing listing and the proposed action. Nothing is saved until /approve."""
    try:
        job = ingest.load_job(job_id, staff)
    except ingest.IngestionError as exc:
        raise _http(exc)
    drafts = [ingest.draft_view(d) for d in ingest.get_store().drafts(job_id)]
    if action:
        drafts = [d for d in drafts if d["effective_action"] == action]
    if review_status:
        drafts = [d for d in drafts if d["review_status"] == review_status]
    return {"job_id": job_id, "job_status": job.get("status"), "draft_counts": job.get("draft_counts"),
            "ready_to_approve": ingest.ready_draft_ids(job_id), "drafts": drafts}


@router.get("/jobs/{job_id}/drafts/{draft_id}")
def get_ingestion_draft(
    job_id: str,
    draft_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
):
    try:
        ingest.load_job(job_id, staff)
    except ingest.IngestionError as exc:
        raise _http(exc)
    draft = ingest.get_store().get_draft(draft_id)
    if not draft or draft.get("job_id") != job_id:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return ingest.draft_view(draft)


@router.patch("/jobs/{job_id}/drafts/{draft_id}")
def update_ingestion_draft(
    job_id: str,
    draft_id: str,
    body: DraftUpdateRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    try:
        draft = ingest.update_draft(db, staff, job_id, draft_id, fields=body.fields, action=body.action,
                                    resolve_decisions=body.resolve_decisions, reject=body.reject)
    except ingest.IngestionError as exc:
        raise _http(exc)
    return ingest.draft_view(draft)


@router.post("/jobs/{job_id}/approve")
def approve_ingestion_drafts(
    job_id: str,
    body: ApproveRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    """Write the selected drafts to Postgres and Firestore (validation, undo snapshot and audit log per listing)."""
    try:
        ingest.load_job(job_id, staff)
        ids = list(body.draft_ids or [])
        if body.all_ready:
            ids = list(dict.fromkeys(ids + ingest.ready_draft_ids(job_id)))
        return ingest.approve_drafts(db, staff, job_id, ids, confirm=body.confirm)
    except ingest.IngestionError as exc:
        raise _http(exc)


@router.post("/jobs/{job_id}/rollback")
def rollback_ingestion_job(
    job_id: str,
    body: RollbackRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin_with_sites),
    db: Session = Depends(get_db),
):
    """Undo every listing this job saved, using the existing 6-day undo snapshots."""
    try:
        return ingest.rollback_job(db, staff, job_id, confirm=body.confirm)
    except ingest.IngestionError as exc:
        raise _http(exc)
