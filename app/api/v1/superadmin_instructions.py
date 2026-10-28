"""Super-admin instruction and remark API. Writes are Firestore-only and super_admin only."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedStaff, require_super_admin
from app.db.session import get_db
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


def _audit_instruction(db: Session, staff: AuthenticatedStaff, tool_name: str, arguments: dict, result: str = "ok") -> None:
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
    request: Request,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
):
    _limit_superadmin_write(request, staff)
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
    request: Request,
    body: Optional[InstructionBody] = None,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Session = Depends(get_db),
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
    db: Session = Depends(get_db),
):
    """Read-only chat, pending actions and audit rows. Nothing is written."""
    return _call(lambda: review_conversation(db, conversation_id))
