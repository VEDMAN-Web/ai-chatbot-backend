"""Chat log review: list and open the logged conversations of the user bot and the admin bot."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.auth import AuthenticatedStaff
from app.core.permissions import (
    CHAT_LOGS_APPROVE_CORRECTION,
    CHAT_LOGS_REMARK,
    CHAT_LOGS_VIEW_ALL,
    CHAT_LOGS_VIEW_OWN,
    CHAT_LOGS_VIEW_PUBLIC,
    require_permission,
)
from app.core.tenant import normalize_website_domain
from app.core.deps import get_db
from app.services import chat_log_query as query
from app.services import chat_log_remarks as remarks
from app.services.audit_service import AuditUnavailable

router = APIRouter(prefix="/admin/chat-logs", tags=["Admin Chat Logs"])

_can_review = require_permission(CHAT_LOGS_VIEW_OWN, CHAT_LOGS_VIEW_PUBLIC, CHAT_LOGS_VIEW_ALL)
_can_remark = require_permission(CHAT_LOGS_REMARK)
_can_approve = require_permission(CHAT_LOGS_APPROVE_CORRECTION)


def _run(action: Callable):
    """Run a remark/correction operation and turn its failures into HTTP errors."""
    try:
        return action()
    except remarks.RemarkError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except (query.ChatLogUnavailable, AuditUnavailable) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/conversations")
def list_logged_conversations(
    agent_type: Optional[str] = Query(None, pattern="^(public|admin)$", description="'public' (user bot) or 'admin'"),
    role: Optional[str] = Query(None, max_length=50, description="Role of the person chatting: public, super_admin, admin, worker"),
    user: Optional[str] = Query(None, max_length=200, description="User id, email part or session id"),
    website: Optional[str] = Query(None, max_length=100),
    conversation_id: Optional[str] = Query(None, max_length=400),
    property_id: Optional[str] = Query(None, max_length=128),
    remark_status: Optional[str] = Query(None, pattern="^(none|open|in_review|resolved|rejected)$"),
    q: Optional[str] = Query(None, max_length=200, description="Text in a question or an answer"),
    date_from: Optional[datetime] = Query(None, description="Last message on or after (ISO 8601; no offset means UTC)"),
    date_to: Optional[datetime] = Query(None, description="Last message on or before"),
    sort: str = Query("last_message_at", pattern="^(last_message_at|created_at|message_count)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
    staff: AuthenticatedStaff = Depends(_can_review),
):
    """Conversations the caller may see, filtered, searched, sorted and paged on the server."""
    site = None
    if website:
        site = normalize_website_domain(website)
        if not site or site not in (staff.allowed_websites or []):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to that website.")
    filters = query.ListFilters(
        agent_type=agent_type, role=role, user=user, website=site, conversation_id=conversation_id,
        property_id=property_id, remark_status=remark_status, q=q, date_from=date_from, date_to=date_to,
    )
    try:
        return query.list_conversations(staff, filters, sort=sort, order=order, page=page, page_size=page_size)
    except query.ChatLogUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/conversations/{conversation_id}")
def open_logged_conversation(
    conversation_id: str,
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(100, ge=1, le=200),
    staff: AuthenticatedStaff = Depends(_can_review),
):
    """One conversation with its messages in chronological order."""
    try:
        result = query.get_conversation(staff, conversation_id, page=page, page_size=page_size)
    except query.ChatLogUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if result is None:  # also used for a conversation the caller may not see, so its existence is not revealed
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return result


# ---- remarks (internal) -----------------------------------------------------------------------------

class RemarkCreate(BaseModel):
    issue: str = Field(..., description="What is wrong or missing in the response")
    suggested_answer: Optional[str] = Field(None, description="Optional: what the answer should have been (not applied to the bot)")


class RemarkUpdate(BaseModel):
    issue: Optional[str] = None
    suggested_answer: Optional[str] = None
    clear_suggested_answer: bool = False
    status: Optional[str] = Field(None, description="open, in_review, resolved or rejected (reviewers only)")


@router.post("/conversations/{conversation_id}/messages/{message_id}/remarks", status_code=status.HTTP_201_CREATED)
def add_remark(conversation_id: str, message_id: str, body: RemarkCreate, staff: AuthenticatedStaff = Depends(_can_remark)):
    """An internal remark on one bot response. It never changes what the bot says."""
    return _run(lambda: remarks.create_remark(staff, conversation_id, message_id, body.issue, body.suggested_answer))


@router.get("/remarks")
def list_remarks(
    conversation_id: Optional[str] = Query(None, max_length=400),
    message_id: Optional[str] = Query(None, max_length=50),
    remark_status: Optional[str] = Query(None, alias="status", pattern="^(open|in_review|resolved|rejected)$"),
    author: Optional[str] = Query(None, max_length=200),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
    staff: AuthenticatedStaff = Depends(_can_review),
):
    return _run(lambda: remarks.list_remarks(staff, conversation_id, message_id, remark_status, author, page, page_size))


@router.get("/remarks/{remark_id}")
def get_remark(remark_id: str, staff: AuthenticatedStaff = Depends(_can_review)):
    return _run(lambda: remarks.get_remark(staff, remark_id))


@router.patch("/remarks/{remark_id}")
def edit_remark(remark_id: str, body: RemarkUpdate, staff: AuthenticatedStaff = Depends(_can_remark)):
    """The author (or a reviewer) edits the text; only a reviewer changes the status. Every change is a revision."""
    return _run(lambda: remarks.update_remark(staff, remark_id, body.issue, body.suggested_answer, body.status, body.clear_suggested_answer))


@router.get("/remarks/{remark_id}/revisions")
def remark_revisions(remark_id: str, staff: AuthenticatedStaff = Depends(_can_review)):
    return {"items": _run(lambda: remarks.list_revisions(staff, "remark", remark_id))}


# ---- answer corrections (reviewed) -------------------------------------------------------------------

class CorrectionPropose(BaseModel):
    corrected_answer: str = Field(..., description="What the bot should have answered")
    websites: Optional[List[str]] = Field(None, description="Websites it applies to (default: the conversation's website)")
    languages: Optional[List[str]] = Field(None, description="Language codes it applies to (default: all)")
    property_ids: Optional[List[str]] = Field(None, description="Properties it applies to (default: none specific)")


class CorrectionUpdate(BaseModel):
    corrected_answer: Optional[str] = None
    websites: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    property_ids: Optional[List[str]] = None


class Decision(BaseModel):
    note: Optional[str] = Field(None, description="Reason, kept with the decision")


@router.post("/remarks/{remark_id}/correction", status_code=status.HTTP_201_CREATED)
def propose_correction(remark_id: str, body: CorrectionPropose, staff: AuthenticatedStaff = Depends(_can_remark)):
    """Propose a corrected answer. It is only a proposal until a super admin approves it."""
    return _run(lambda: remarks.propose_correction(staff, remark_id, body.corrected_answer, body.websites, body.languages, body.property_ids))


@router.get("/corrections")
def list_corrections(
    correction_status: Optional[str] = Query(None, alias="status", pattern="^(proposed|approved|rejected|inactive)$"),
    conversation_id: Optional[str] = Query(None, max_length=400),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
    staff: AuthenticatedStaff = Depends(_can_review),
):
    return _run(lambda: remarks.list_corrections(staff, correction_status, conversation_id, page, page_size))


@router.get("/corrections/{correction_id}")
def get_correction(correction_id: str, staff: AuthenticatedStaff = Depends(_can_review)):
    return _run(lambda: remarks.get_correction(staff, correction_id))


@router.patch("/corrections/{correction_id}")
def edit_correction(correction_id: str, body: CorrectionUpdate, staff: AuthenticatedStaff = Depends(_can_remark)):
    """Editing returns the correction to 'proposed': an edited answer has to be approved again."""
    return _run(lambda: remarks.update_correction(staff, correction_id, body.corrected_answer, body.websites, body.languages, body.property_ids))


@router.get("/corrections/{correction_id}/revisions")
def correction_revisions(correction_id: str, staff: AuthenticatedStaff = Depends(_can_review)):
    return {"items": _run(lambda: remarks.list_revisions(staff, "correction", correction_id))}


@router.post("/corrections/{correction_id}/approve")
def approve_correction(correction_id: str, body: Decision = Decision(), staff: AuthenticatedStaff = Depends(_can_approve), db: Any = Depends(get_db)):
    return _run(lambda: remarks.decide_correction(db, staff, correction_id, "approve", body.note))


@router.post("/corrections/{correction_id}/reject")
def reject_correction(correction_id: str, body: Decision = Decision(), staff: AuthenticatedStaff = Depends(_can_approve), db: Any = Depends(get_db)):
    return _run(lambda: remarks.decide_correction(db, staff, correction_id, "reject", body.note))


@router.post("/corrections/{correction_id}/deactivate")
def deactivate_correction(correction_id: str, body: Decision = Decision(), staff: AuthenticatedStaff = Depends(_can_approve), db: Any = Depends(get_db)):
    return _run(lambda: remarks.decide_correction(db, staff, correction_id, "deactivate", body.note))


# ---- user feedback (thumbs up / down) -------------------------------------------------------

class FeedbackRequest(BaseModel):
    feedback: str = Field(..., pattern="^(thumbs_up|thumbs_down)$",
                         description="User feedback signal: 'thumbs_up' or 'thumbs_down'")
    comment: Optional[str] = Field(None, max_length=500,
                                   description="Optional free-text comment (max 500 chars)")


@router.patch("/conversations/{conversation_id}/messages/{message_seq}/feedback")
def submit_user_feedback(
    conversation_id: str,
    message_seq: str,
    body: FeedbackRequest,
    staff: AuthenticatedStaff = Depends(_can_review),
):
    """Adds user feedback (thumbs_up/thumbs_down) to a logged assistant message.

    Updates the Firestore document at:
        chat_logs/{conversation_id}/messages/{message_seq}
    Setting: user_feedback = {feedback, comment, recorded_at}
    """
    from datetime import datetime, timezone
    from app.services import chat_log_query as _query

    def _run_feedback():
        client = _query._require_client()
        # Verify the conversation exists and caller may see it
        conv_snap = client.collection("chat_logs").document(conversation_id).get()
        if not conv_snap.exists or not _query.can_view(conv_snap.to_dict() or {}, staff):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Conversation not found.")
        msg_ref = (
            client.collection("chat_logs")
            .document(conversation_id)
            .collection("messages")
            .document(message_seq)
        )
        msg_snap = msg_ref.get()
        if not msg_snap.exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Message not found.")
        msg_data = msg_snap.to_dict() or {}
        if msg_data.get("role") != "assistant":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Feedback can only be submitted on assistant messages."
            )
        recorded_at = datetime.now(timezone.utc).isoformat()
        user_feedback = {
            "feedback": body.feedback,
            "comment": body.comment,
            "recorded_at": recorded_at,
        }
        msg_ref.update({"user_feedback": user_feedback})
        return {
            "conversation_id": conversation_id,
            "message_seq": message_seq,
            "user_feedback": user_feedback,
            "updated": True,
        }

    return _run(lambda: _run_feedback())
