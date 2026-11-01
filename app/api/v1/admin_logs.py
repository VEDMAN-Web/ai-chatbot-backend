"""
Admin viewer for visitor chat_query_logs and admin_chat_logs.
GET only for log reads. Purge is super_admin POST.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core.auth import (
    AuthenticatedStaff,
    require_admin_or_super_admin,
    require_super_admin,
)
from app.core.deps import get_db
from app.services.admin_logs_service import (
    ADMIN_COLLECTION,
    VISITOR_COLLECTION,
    parse_bool_param,
    parse_date_param,
    purge_chat_logs,
    query_logs,
    query_session_turns,
    query_sessions,
    query_stats,
)
from app.services.audit_service import record_audit_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin Logs"])


class PurgeBody(BaseModel):
    dry_run: bool = Field(True, description="When true, only count eligible rows.")


def _is_super(staff: AuthenticatedStaff) -> bool:
    return (getattr(staff, "role", "") or "").lower() == "super_admin"


def _allowed_sites(staff: AuthenticatedStaff) -> List[str]:
    return list(getattr(staff, "allowed_websites", None) or [])


def _audit_read(
    db: Any,
    staff: AuthenticatedStaff,
    endpoint: str,
    filters: Dict[str, Any],
) -> None:
    try:
        record_audit_log(
            db=db,
            tool_name="admin:read_logs",
            staff_user_id=getattr(staff, "email", None) or getattr(staff, "user_id", None),
            tool_arguments={"endpoint": endpoint, "filters": filters},
            proposed_payload={"action": "read_logs", "endpoint": endpoint},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Read {endpoint}",
            conversation_id=None,
        )
    except Exception as exc:
        logger.warning("Audit for log read failed: %s", exc)


def _build_filters(
    *,
    site: Optional[str],
    language: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    session_id: Optional[str],
    q: Optional[str],
    fallback_type: Optional[str],
    flagged: Optional[str],
    corrected: Optional[str],
    property_id: Optional[str],
    intent: Optional[str],
) -> Dict[str, Any]:
    try:
        parsed_from = parse_date_param(date_from)
        parsed_to = parse_date_param(date_to, end_of_day=True)
        flagged_b = parse_bool_param(flagged)
        corrected_b = parse_bool_param(corrected)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {
        "site": site,
        "language": language,
        "date_from": parsed_from,
        "date_to": parsed_to,
        "session_id": session_id,
        "q": q,
        "fallback_type": fallback_type,
        "flagged": flagged_b,
        "corrected": corrected_b,
        "property_id": property_id,
        "intent": intent,
    }


def _common_query(
    *,
    site: Optional[str] = None,
    language: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    fallback_type: Optional[str] = None,
    flagged: Optional[str] = None,
    corrected: Optional[str] = None,
    property_id: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    filters = _build_filters(
        site=site,
        language=language,
        date_from=date_from,
        date_to=date_to,
        session_id=session_id,
        q=q,
        fallback_type=fallback_type,
        flagged=flagged,
        corrected=corrected,
        property_id=property_id,
        intent=intent,
    )
    return {"filters": filters, "limit": limit, "offset": offset}


# --------------- visitor logs (admin + super_admin) ---------------


@router.get("/query-logs")
def list_query_logs(
    request: Request,
    site: Optional[str] = None,
    language: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    fallback_type: Optional[str] = None,
    flagged: Optional[str] = None,
    corrected: Optional[str] = None,
    property_id: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Any = Depends(get_db),
):
    params = _common_query(
        site=site, language=language, date_from=date_from, date_to=date_to,
        session_id=session_id, q=q, fallback_type=fallback_type, flagged=flagged,
        corrected=corrected, property_id=property_id, intent=intent,
        limit=limit, offset=offset,
    )
    _audit_read(db, staff, "GET /admin/query-logs", {k: str(v) for k, v in params["filters"].items() if v is not None})
    return query_logs(
        VISITOR_COLLECTION,
        filters=params["filters"],
        limit=params["limit"],
        offset=params["offset"],
        allowed_websites=_allowed_sites(staff),
        super_admin=_is_super(staff),
    )


@router.get("/query-logs/sessions")
def list_query_sessions(
    site: Optional[str] = None,
    language: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    fallback_type: Optional[str] = None,
    flagged: Optional[str] = None,
    corrected: Optional[str] = None,
    property_id: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Any = Depends(get_db),
):
    params = _common_query(
        site=site, language=language, date_from=date_from, date_to=date_to,
        session_id=session_id, q=q, fallback_type=fallback_type, flagged=flagged,
        corrected=corrected, property_id=property_id, intent=intent,
        limit=limit, offset=offset,
    )
    _audit_read(db, staff, "GET /admin/query-logs/sessions", {k: str(v) for k, v in params["filters"].items() if v is not None})
    return query_sessions(
        VISITOR_COLLECTION,
        filters=params["filters"],
        limit=params["limit"],
        offset=params["offset"],
        allowed_websites=_allowed_sites(staff),
        super_admin=_is_super(staff),
    )


@router.get("/query-logs/sessions/{session_id}")
def get_query_session(
    session_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Any = Depends(get_db),
):
    _audit_read(db, staff, "GET /admin/query-logs/sessions/{session_id}", {"session_id": session_id})
    return query_session_turns(
        VISITOR_COLLECTION,
        session_id,
        allowed_websites=_allowed_sites(staff),
        super_admin=_is_super(staff),
    )


@router.get("/query-logs/stats")
def query_log_stats(
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Any = Depends(get_db),
):
    _audit_read(db, staff, "GET /admin/query-logs/stats", {})
    return query_stats(
        VISITOR_COLLECTION,
        allowed_websites=_allowed_sites(staff),
        super_admin=_is_super(staff),
    )


# --------------- admin chat logs (super_admin only) ---------------


@router.get("/chat-logs")
def list_chat_logs(
    site: Optional[str] = None,
    language: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    fallback_type: Optional[str] = None,
    flagged: Optional[str] = None,
    corrected: Optional[str] = None,
    property_id: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    params = _common_query(
        site=site, language=language, date_from=date_from, date_to=date_to,
        session_id=session_id, q=q, fallback_type=fallback_type, flagged=flagged,
        corrected=corrected, property_id=property_id, intent=intent,
        limit=limit, offset=offset,
    )
    _audit_read(db, staff, "GET /admin/chat-logs", {k: str(v) for k, v in params["filters"].items() if v is not None})
    return query_logs(
        ADMIN_COLLECTION,
        filters=params["filters"],
        limit=params["limit"],
        offset=params["offset"],
        allowed_websites=_allowed_sites(staff),
        super_admin=True,
    )


@router.get("/chat-logs/sessions")
def list_chat_sessions(
    site: Optional[str] = None,
    language: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    fallback_type: Optional[str] = None,
    flagged: Optional[str] = None,
    corrected: Optional[str] = None,
    property_id: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    params = _common_query(
        site=site, language=language, date_from=date_from, date_to=date_to,
        session_id=session_id, q=q, fallback_type=fallback_type, flagged=flagged,
        corrected=corrected, property_id=property_id, intent=intent,
        limit=limit, offset=offset,
    )
    _audit_read(db, staff, "GET /admin/chat-logs/sessions", {k: str(v) for k, v in params["filters"].items() if v is not None})
    return query_sessions(
        ADMIN_COLLECTION,
        filters=params["filters"],
        limit=params["limit"],
        offset=params["offset"],
        allowed_websites=_allowed_sites(staff),
        super_admin=True,
    )


@router.get("/chat-logs/sessions/{session_id}")
def get_chat_session(
    session_id: str,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _audit_read(db, staff, "GET /admin/chat-logs/sessions/{session_id}", {"session_id": session_id})
    return query_session_turns(
        ADMIN_COLLECTION,
        session_id,
        allowed_websites=_allowed_sites(staff),
        super_admin=True,
    )


@router.get("/chat-logs/stats")
def chat_log_stats(
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    _audit_read(db, staff, "GET /admin/chat-logs/stats", {})
    return query_stats(
        ADMIN_COLLECTION,
        allowed_websites=_allowed_sites(staff),
        super_admin=True,
    )


@router.post("/maintenance/purge-chat-logs")
def purge_chat_logs_endpoint(
    body: Optional[PurgeBody] = None,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Any = Depends(get_db),
):
    dry_run = True if body is None else bool(body.dry_run)
    result = purge_chat_logs(dry_run=dry_run)
    try:
        record_audit_log(
            db=db,
            tool_name="admin:purge_chat_logs",
            staff_user_id=getattr(staff, "email", None) or getattr(staff, "user_id", None),
            tool_arguments={"dry_run": dry_run},
            proposed_payload={"action": "purge_chat_logs", "result": result},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=(
                f"dry_run eligible={result.get('total_eligible')}"
                if dry_run
                else f"deleted={result.get('total_deleted')}"
            ),
        )
    except Exception as exc:
        logger.warning("Audit for purge-chat-logs failed: %s", exc)
    return result
