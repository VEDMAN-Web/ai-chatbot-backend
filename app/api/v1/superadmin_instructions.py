"""Super-admin instruction and remark API. Writes are Firestore-only and super_admin only."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.auth import AuthenticatedStaff, require_super_admin
from app.core.deps import get_db
from app.services.audit_service import record_audit_log
from app.services.instruction_store import (
    InstructionStoreError,
    get_store,
    lookup_chat_excerpt,
    review_conversation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/superadmin", tags=["Super Admin Instructions"])


def _limit_superadmin_write(request: Optional[Request], staff: AuthenticatedStaff) -> None:
    from app.core.config import settings
    from app.core.rate_limit import enforce_rate_limit

    enforce_rate_limit(
        request,
        scope="superadmin_write",
        identities=[getattr(staff, "email", None)],
        limit=int(settings.RATE_LIMIT_IMPORT_AND_SUPERADMIN),
        window_seconds=int(settings.RATE_LIMIT_IMPORT_WINDOW_SEC),
        message="Too many super-admin write requests. Try again later.",
    )


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


def _audit_instruction(db: Any, staff: AuthenticatedStaff, tool_name: str, arguments: dict, result: str = "ok") -> None:
    try:
        record_audit_log(
            db=db,
            tool_name=tool_name,
            staff_user_id=_actor(staff),
            tool_arguments=arguments,
            proposed_payload={"action": tool_name, **arguments},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=result,
        )
    except Exception as exc:
        logger.warning("Instruction audit failed for %s: %s", tool_name, exc)


def _call(fn):
    try:
        return fn()
    except InstructionStoreError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _with_excerpt(db: Any, remark: Dict[str, Any]) -> Dict[str, Any]:
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
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    saved = _call(lambda: get_store().import_bundle(body.model_dump(), _actor(staff)))
    _audit_instruction(db, staff, "instruction:import", {"count": len(saved)}, f"imported {len(saved)}")
    return {"saved": len(saved), "instructions": saved}


@router.post("/instructions")
def create_instruction(
    body: InstructionBody,
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    doc = _call(lambda: get_store().create(body.model_dump(exclude_unset=True), _actor(staff)))
    _audit_instruction(db, staff, "instruction:create", {"instruction_id": doc.get("id")}, "created")
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
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    doc = _call(lambda: get_store().update(instruction_id, body.model_dump(exclude_unset=True), _actor(staff)))
    _audit_instruction(db, staff, "instruction:update", {"instruction_id": instruction_id}, "updated")
    return doc


@router.post("/instructions/{instruction_id}/activate")
def activate_instruction(
    instruction_id: str,
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    doc = _call(lambda: get_store().activate(instruction_id, _actor(staff)))
    _audit_instruction(db, staff, "instruction:activate", {"instruction_id": instruction_id}, "activated")
    return doc


@router.post("/instructions/{instruction_id}/archive")
def archive_instruction(
    instruction_id: str,
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    doc = _call(lambda: get_store().archive(instruction_id, _actor(staff)))
    _audit_instruction(db, staff, "instruction:archive", {"instruction_id": instruction_id}, "archived")
    return doc


@router.post("/instructions/{instruction_id}/rollback")
def rollback_instruction(
    instruction_id: str,
    body: RollbackBody,
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    doc = _call(lambda: get_store().rollback(instruction_id, body.version, _actor(staff)))
    _audit_instruction(
        db, staff, "instruction:rollback",
        {"instruction_id": instruction_id, "version": body.version},
        "rolled_back",
    )
    return doc


@router.post("/remarks")
def create_remark(
    body: RemarkBody,
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    saved = _call(lambda: get_store().add_remark(body.model_dump(), _actor(staff)))
    return _with_excerpt(db, saved)


@router.get("/remarks")
def list_remarks(
    status: Optional[str] = None,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    rows = _call(lambda: get_store().list_remarks(status=status))
    return {"remarks": [_with_excerpt(db, row) for row in rows]}


@router.post("/remarks/{remark_id}/convert")
def convert_remark(
    remark_id: str,
    request: Request,
    body: Optional[InstructionBody] = None,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
    fields = body.model_dump(exclude_unset=True) if body else {}
    result = _call(lambda: get_store().convert_remark(remark_id, _actor(staff), fields))
    result["remark"] = _with_excerpt(db, result["remark"])
    return result


@router.post("/remarks/{remark_id}/dismiss")
def dismiss_remark(
    remark_id: str,
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    _limit_superadmin_write(request, staff)
    return _call(lambda: get_store().dismiss_remark(remark_id, _actor(staff)))


@router.get("/conversations/{conversation_id}")
def read_conversation_review(
    conversation_id: str,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    """Read-only chat, pending actions and audit rows. Nothing is written."""
    return _call(lambda: review_conversation(db, conversation_id))


@router.get("/conversations")
def list_conversation_logs(
    search: Optional[str] = Query(None, max_length=200),
    role: Optional[str] = Query(None, max_length=50),
    model: Optional[str] = Query(None, max_length=80),
    review_status: Optional[str] = Query(None, pattern="^(pending|reviewed)$", description="The panel's review badge"),
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
    filters = query.ListFilters(q=search, role=role, model=model, website=site, review_status=review_status,
                                date_from=start_date, date_to=end_date)
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
                "review_status": item.get("review_status"),
                "created_at": item.get("last_message_at") or item.get("created_at"),
                "user_message": item.get("first_question") or "",
                "ai_response": answer or "",
            })
    except query.ChatLogUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"total": found["total"], "limit": limit, "offset": offset, "logs": logs}


class ConversationReviewBody(BaseModel):
    message_index: Optional[int] = Field(None, ge=0, description="Which AI response (0-based message number); default the first answer, as listed")
    is_satisfactory: bool = Field(False, description="The 'current AI response is correct and satisfactory' checkbox")
    instructions: Optional[str] = Field(None, max_length=600, description="Admin Instructions: guidance for similar questions in future")
    website: Optional[str] = Field(None, max_length=100, description="Website the guidance applies to; default the conversation's own website")


def _review_state(target: Dict[str, Any]) -> Dict[str, Any]:
    header, index = target["header"], target["index"]
    saved = (header.get("reviews") or {}).get(str(index))
    if saved:
        saved = {
            "status": saved.get("status"), "reviewed_by": saved.get("by"),
            "reviewed_at": saved["at"].isoformat() if hasattr(saved.get("at"), "isoformat") else saved.get("at"),
            "instruction_id": saved.get("instructionId"),
        }
    return {
        "conversation_id": target["key"].split("__", 1)[-1],
        "message_index": index,
        "agent_type": header.get("agentType"),
        "website": header.get("website") or None,
        "model": header.get("model"),
        "language": header.get("language"),
        "user_question": (target["question"] or {}).get("content") or "",
        "ai_response": target["answer"].get("content") or "",
        "review_status": "reviewed" if saved else "pending",
        "review": saved,
    }


def _review_target(conversation_id: str, message_index: Optional[int]) -> Dict[str, Any]:
    from app.services import chat_log_query as query
    try:
        return query.load_review_target(conversation_id, message_index)
    except query.ReviewTargetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except query.ChatLogUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/conversations/{conversation_id}/review")
def get_conversation_review_state(
    conversation_id: str,
    message_index: Optional[int] = Query(None, ge=0),
    staff: AuthenticatedStaff = Depends(require_super_admin),
):
    """What the 'Review AI Response' dialog shows: the full question, the full AI response and whether it is reviewed."""
    return _review_state(_review_target(conversation_id, message_index))


@router.post("/conversations/{conversation_id}/review")
def save_conversation_review(
    conversation_id: str,
    body: ConversationReviewBody,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    """Save Review. Either tick 'satisfactory', or give instructions, which become an active instruction for that bot and website."""
    from app.core.tenant import normalize_website_domain
    from app.services import chat_log_query as query
    from app.services.audit_service import AuditUnavailable, record_sensitive_audit

    actor = _actor(staff)
    text = (body.instructions or "").strip()
    if body.is_satisfactory and text:
        raise HTTPException(status_code=422, detail="Choose one: mark the response satisfactory, or write instructions.")
    if not body.is_satisfactory and not text:
        raise HTTPException(status_code=422, detail="Tick 'correct and satisfactory' or write instructions for the AI.")
    target = _review_target(conversation_id, body.message_index)
    header, index, key = target["header"], target["index"], target["key"]

    site = "all"
    if body.website:
        site = normalize_website_domain(body.website)
        if not site:
            raise HTTPException(status_code=422, detail="Unknown website.")
    elif header.get("website"):
        site = header["website"]
    bot = "public_chat" if header.get("agentType") == "public" else "admin_chat"

    try:
        record_sensitive_audit(
            db=db, tool_name="conversation_review", staff_user_id=staff.email or getattr(staff, "user_id", None) or "super_admin",
            proposed_payload={"conversation_id": key, "message_index": index, "satisfactory": body.is_satisfactory,
                              "target": None if body.is_satisfactory else bot, "website": None if body.is_satisfactory else site,
                              "instructions_length": len(text)},
            confirmed_by_staff=True, status="PENDING",
        )
    except AuditUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if body.is_satisfactory:
        saved = query.save_review(key, index, "satisfactory", actor)
        return {**_review_state({**target, "header": {**header, "reviews": {**(header.get("reviews") or {}), str(index): saved}}}),
                "outcome": "satisfactory", "instruction": None,
                "message": "Review saved: the response is marked correct and satisfactory."}

    store = get_store()
    remark = None
    try:
        remark = store.add_remark({
            "conversation_id": key, "message_index": index, "remark_text": "Reviewer guidance for answers like this one.",
            "expected_behavior": text, "severity": "normal", "targets": [bot],
        }, actor)
        converted = store.convert_remark(remark["id"], actor, {
            "title": f"Reviewer guidance ({site})", "rule": text, "targets": [bot], "websites": [site],
            "languages": ["all"], "priority": 50,
        })
        instruction = store.activate(converted["instruction"]["id"], actor)
    except InstructionStoreError as exc:
        if remark:
            try:
                store.dismiss_remark(remark["id"], actor)
            except InstructionStoreError:
                pass
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    active = instruction.get("status") == "active"
    saved = query.save_review(key, index, "instruction_active" if active else instruction.get("status") or "needs_clarification",
                              actor, instruction_id=instruction["id"], remark_id=remark["id"])
    conflict = instruction.get("conflict_report") or None
    return {
        **_review_state({**target, "header": {**header, "reviews": {**(header.get("reviews") or {}), str(index): saved}}}),
        "outcome": "instruction_active" if active else instruction.get("status"),
        "instruction": {"id": instruction["id"], "status": instruction.get("status"), "targets": instruction.get("targets"),
                        "websites": instruction.get("websites"), "version": instruction.get("version"), "conflict": conflict},
        "message": (f"Review saved. {instruction['id']} is active for {bot} on {site}; it applies to new answers within a minute."
                    if active else
                    f"Review saved, but {instruction['id']} conflicts with an active instruction ({', '.join((conflict or {}).get('against') or [])}) "
                    "and is not active yet. Resolve it in Instructions."),
    }
