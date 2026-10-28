"""Super-admin instruction and remark API. Writes are Firestore-only and super_admin only."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedStaff, require_super_admin
from app.db.session import get_db
from app.services.instruction_store import (
    InstructionStoreError,
    get_store,
    lookup_chat_excerpt,
    review_conversation,
)

router = APIRouter(prefix="/admin/superadmin", tags=["Super Admin Instructions"])


class InstructionBody(BaseModel):
    title: Optional[str] = None
    rule: Optional[str] = None
    good_example: Optional[str] = ""
    avoid_example: Optional[str] = ""
    targets: Optional[List[str]] = None
    websites: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    supersedes: Optional[List[str]] = None
    source_remarks: Optional[List[str]] = None


class RemarkBody(BaseModel):
    conversation_id: str
    message_index: int = 0
    remark_text: str
    expected_behavior: str = ""
    severity: str = "normal"
    targets: Optional[List[str]] = None


class RollbackBody(BaseModel):
    version: int = Field(..., ge=1)


class ImportBody(BaseModel):
    instructions: List[Dict[str, Any]]


def _actor(staff: AuthenticatedStaff) -> str:
    if (getattr(staff, "role", "") or "").lower() != "super_admin":
        raise HTTPException(status_code=403, detail="Only a super admin can read or change instructions.")
    return staff.email or "super_admin"


def _call(fn):
    try:
        return fn()
    except InstructionStoreError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _with_excerpt(db: Session, remark: Dict[str, Any]) -> Dict[str, Any]:
    shown = dict(remark)
    shown["excerpt"] = lookup_chat_excerpt(db, remark.get("conversation_id"), int(remark.get("message_index") or 0))
    return shown


@router.get("/instructions/export")
def export_instructions(staff: AuthenticatedStaff = Depends(require_super_admin)):
    payload = _call(lambda: get_store().export_zip())
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=superadmin-instructions.zip"},
    )


@router.post("/instructions/import")
def import_instructions(
    body: ImportBody,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    saved = _call(lambda: get_store().import_bundle(body.model_dump(), _actor(staff)))
    return {"saved": len(saved), "instructions": saved}


@router.post("/instructions")
def create_instruction(
    body: InstructionBody,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    doc = _call(lambda: get_store().create(body.model_dump(exclude_unset=True), _actor(staff)))
    return doc


@router.get("/instructions")
def list_instructions(
    status: Optional[str] = None,
    target: Optional[str] = None,
    website: Optional[str] = None,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    return {"instructions": _call(lambda: get_store().list(status=status, target=target, website=website))}


@router.get("/instructions/{instruction_id}")
def get_instruction(
    instruction_id: str,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    return _call(lambda: get_store().get(instruction_id))


@router.patch("/instructions/{instruction_id}")
def update_instruction(
    instruction_id: str,
    body: InstructionBody,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    return _call(lambda: get_store().update(instruction_id, body.model_dump(exclude_unset=True), _actor(staff)))


@router.post("/instructions/{instruction_id}/activate")
def activate_instruction(
    instruction_id: str,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    return _call(lambda: get_store().activate(instruction_id, _actor(staff)))


@router.post("/instructions/{instruction_id}/archive")
def archive_instruction(
    instruction_id: str,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    return _call(lambda: get_store().archive(instruction_id, _actor(staff)))


@router.post("/instructions/{instruction_id}/rollback")
def rollback_instruction(
    instruction_id: str,
    body: RollbackBody,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    return _call(lambda: get_store().rollback(instruction_id, body.version, _actor(staff)))


@router.post("/remarks")
def create_remark(
    body: RemarkBody,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    saved = _call(lambda: get_store().add_remark(body.model_dump(), _actor(staff)))
    return _with_excerpt(db, saved)


@router.get("/remarks")
def list_remarks(
    status: Optional[str] = None,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    rows = _call(lambda: get_store().list_remarks(status=status))
    return {"remarks": [_with_excerpt(db, row) for row in rows]}


@router.post("/remarks/{remark_id}/convert")
def convert_remark(
    remark_id: str,
    body: Optional[InstructionBody] = None,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    fields = body.model_dump(exclude_unset=True) if body else {}
    result = _call(lambda: get_store().convert_remark(remark_id, _actor(staff), fields))
    result["remark"] = _with_excerpt(db, result["remark"])
    return result


@router.post("/remarks/{remark_id}/dismiss")
def dismiss_remark(
    remark_id: str,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    return _call(lambda: get_store().dismiss_remark(remark_id, _actor(staff)))


@router.get("/conversations/{conversation_id}")
def read_conversation_review(
    conversation_id: str,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Read-only chat, pending actions and audit rows. Nothing is written."""
    return _call(lambda: review_conversation(db, conversation_id))


@router.get("/conversations")
def list_conversation_logs(
    search: Optional[str] = Query(None, max_length=200),
    role: Optional[str] = Query(None, max_length=50),
    model: Optional[str] = Query(None, max_length=80),
    website: Optional[str] = Query(None, max_length=100, description="Only this website (the panel's chat selector)"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    """The list the Admin Panel's chat-log page loads. It reads the same chat log as /admin/chat-logs/conversations.

    A row's `conversation_id` is the session id (a UUID), which is what the panel opens with the review endpoint below.
    """
    from app.core.tenant import normalize_website_domain
    from app.services import chat_log_query as query

    site = None
    if website:
        site = normalize_website_domain(website)
        if not site:
            raise HTTPException(status_code=422, detail="Unknown website.")
    filters = query.ListFilters(q=search, role=role, model=model, website=site, date_from=start_date, date_to=end_date)
    try:
        found = query.list_conversations(staff, filters, page=1, page_size=offset + limit)
        client = None
        logs = []
        for item in found["items"][offset:offset + limit]:
            user = item.get("user") or {}
            answer = item.get("first_answer")
            if answer is None:  # logged before answer previews were stored
                client = client or query._require_client()
                replies = [m for m in query._read_messages(client, item["conversation_id"], limit=2) if m["role"] == "assistant"]
                answer = (replies[0]["content"] or "")[:160] if replies else ""
            logs.append({
                "conversation_id": item.get("session_id") or item.get("conversation_id"),
                "user_name": user.get("name"),
                "user_email": user.get("email"),
                "role": user.get("role"),
                "project": item.get("website"),
                "model": item.get("model"),
                "created_at": item.get("last_message_at") or item.get("created_at"),
                "user_message": item.get("first_question") or "",
                "ai_response": answer or "",
            })
    except query.ChatLogUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"total": found["total"], "limit": limit, "offset": offset, "logs": logs}
