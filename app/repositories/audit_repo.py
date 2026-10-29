"""
Audit log repository — Firestore.

One document per action in collection `audit_logs`.
On read, also merges legacy `admin_audit_log` documents so history is not lost.
Does NOT write the separate admin_audit_log mirror.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.repositories import _fs_utils as fs
from app.repositories.store_mode import require_firestore_client

logger = logging.getLogger(__name__)

COLLECTION = "audit_logs"
LEGACY_COLLECTION = "admin_audit_log"


class AuditLogRecord:
    def __init__(self, data: Dict[str, Any]):
        self._data = dict(data or {})
        self.id = str(self._data.get("id") or "")
        self.staff_user_id = self._data.get("staff_user_id") or self._data.get("adminEmail")
        self.tool_name = self._data.get("tool_name") or self._data.get("action") or ""
        self.tool_arguments = self._data.get("tool_arguments") or {}
        self.proposed_payload = self._data.get("proposed_payload") or {}
        self.confirmed_by_staff = bool(self._data.get("confirmed_by_staff") or self._data.get("confirmed"))
        self.execution_result = self._data.get("execution_result")
        self.status = self._data.get("status") or "SUCCESS"
        self.error_message = self._data.get("error_message") or self._data.get("error")
        self.conversation_id = self._data.get("conversation_id")
        self.pending_action_id = self._data.get("pending_action_id") or self._data.get("targetId")
        self.request_id = self._data.get("request_id")
        self.created_at = fs.parse_dt(
            self._data.get("created_at") or self._data.get("createdAt")
        ) or datetime.now(timezone.utc)

    def to_firestore_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "staff_user_id": self.staff_user_id,
            "tool_name": self.tool_name,
            "tool_arguments": self.tool_arguments or {},
            "proposed_payload": self.proposed_payload or {},
            "confirmed_by_staff": self.confirmed_by_staff,
            "execution_result": self.execution_result,
            "status": self.status,
            "error_message": self.error_message,
            "conversation_id": self.conversation_id,
            "pending_action_id": self.pending_action_id,
            "request_id": self.request_id,
            "created_at": fs.to_iso(self.created_at),
        }


def create(
    db: Any,
    *,
    tool_name: str,
    staff_user_id: Optional[str] = None,
    tool_arguments: Optional[Dict[str, Any]] = None,
    proposed_payload: Optional[Dict[str, Any]] = None,
    confirmed_by_staff: bool = False,
    execution_result: Optional[str] = None,
    status: str = "SUCCESS",
    error_message: Optional[str] = None,
    conversation_id: Optional[str] = None,
    pending_action_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogRecord:
    """Persist one audit document. Raises on total write failure (caller decides soft/hard)."""
    doc_id = str(uuid.uuid4())
    record = AuditLogRecord({
        "id": doc_id,
        "staff_user_id": staff_user_id,
        "tool_name": tool_name,
        "tool_arguments": tool_arguments or {},
        "proposed_payload": proposed_payload or {},
        "confirmed_by_staff": confirmed_by_staff,
        "execution_result": execution_result,
        "status": status,
        "error_message": error_message,
        "conversation_id": conversation_id,
        "pending_action_id": pending_action_id,
        "request_id": request_id,
        "created_at": datetime.now(timezone.utc),
    })

    client = require_firestore_client(fs.admin_fs_client(), store_name="audit_logs")
    try:
        fs.set_document(client, COLLECTION, record.id, record.to_firestore_dict())
    except Exception as e:
        logger.error("Audit firestore write failed: %s", e)
        raise RuntimeError(f"firestore:{e}") from e
    return record


def update_fields(db: Any, audit_id: str, **fields: Any) -> Optional[AuditLogRecord]:
    record = get_by_id(db, audit_id)
    if record is None:
        return None
    for key in (
        "status", "execution_result", "error_message", "proposed_payload",
        "tool_arguments", "confirmed_by_staff",
    ):
        if key in fields:
            setattr(record, key, fields[key])
    client = fs.admin_fs_client()
    if client is not None:
        fs.set_document(client, COLLECTION, record.id, record.to_firestore_dict(), merge=True)
    return record


def get_by_id(db: Any, audit_id: str) -> Optional[AuditLogRecord]:
    client = fs.admin_fs_client()
    data = fs.get_document(client, COLLECTION, audit_id) if client else None
    if data:
        return AuditLogRecord(data)
    return None


def list_by_conversation(db: Any, conversation_id: str, limit: int = 100) -> List[AuditLogRecord]:
    rows = _fs_query(conversation_id=conversation_id, limit=limit)
    # Merge legacy admin_audit_log
    legacy = _legacy_by_conversation(conversation_id, limit=limit)
    return _dedupe_merge(rows, legacy)[:limit]


def query(
    db: Any,
    *,
    staff_email: Optional[str] = None,
    tool_name: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    conversation_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    records = _fs_query(
        staff_email=staff_email,
        tool_name=tool_name,
        status=status,
        start_date=start_date,
        end_date=end_date,
        conversation_id=conversation_id,
        limit=limit + offset + 200,
    )
    legacy = _legacy_query(
        staff_email=staff_email,
        tool_name=tool_name,
        limit=limit + offset + 100,
    )

    merged = _dedupe_merge(records, legacy)

    merged.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    total = len(merged)
    page = merged[offset: offset + limit]
    return {"total": total, "logs": page, "limit": limit, "offset": offset}


def _dedupe_merge(*groups: List[AuditLogRecord]) -> List[AuditLogRecord]:
    seen = set()
    out: List[AuditLogRecord] = []
    for group in groups:
        for row in group:
            key = row.id or f"{row.tool_name}:{fs.to_iso(row.created_at)}:{row.staff_user_id}"
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def _fs_query(
    *,
    staff_email: Optional[str] = None,
    tool_name: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    conversation_id: Optional[str] = None,
    limit: int = 100,
) -> List[AuditLogRecord]:
    client = fs.admin_fs_client()
    if client is None:
        return []
    docs = [fs.doc_to_dict(s) for s in fs.stream_collection(client, COLLECTION)]
    out = []
    for d in docs:
        rec = AuditLogRecord(d)
        if staff_email and staff_email.lower() not in (rec.staff_user_id or "").lower():
            continue
        if tool_name and tool_name.lower() not in (rec.tool_name or "").lower():
            continue
        if status and status.lower() != (rec.status or "").lower():
            continue
        if conversation_id and str(rec.conversation_id or "") != str(conversation_id):
            continue
        if start_date and rec.created_at and rec.created_at < (
            start_date if start_date.tzinfo else start_date.replace(tzinfo=timezone.utc)
        ):
            continue
        if end_date and rec.created_at and rec.created_at > (
            end_date if end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
        ):
            continue
        out.append(rec)
    out.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]


def _legacy_query(
    *,
    staff_email: Optional[str] = None,
    tool_name: Optional[str] = None,
    limit: int = 100,
) -> List[AuditLogRecord]:
    client = fs.admin_fs_client()
    if client is None:
        return []
    docs = [fs.doc_to_dict(s) for s in fs.stream_collection(client, LEGACY_COLLECTION)]
    out = []
    for d in docs:
        # Map legacy shape → AuditLogRecord
        mapped = {
            "id": d.get("id") or f"legacy_{d.get('createdAt')}",
            "staff_user_id": d.get("adminEmail"),
            "tool_name": d.get("action"),
            "tool_arguments": (d.get("details") or {}).get("arguments") if isinstance(d.get("details"), dict) else {},
            "proposed_payload": (d.get("details") or {}).get("payload") if isinstance(d.get("details"), dict) else d.get("details"),
            "confirmed_by_staff": bool((d.get("details") or {}).get("confirmed")) if isinstance(d.get("details"), dict) else False,
            "status": (d.get("details") or {}).get("status") if isinstance(d.get("details"), dict) else "SUCCESS",
            "error_message": (d.get("details") or {}).get("error") if isinstance(d.get("details"), dict) else None,
            "pending_action_id": d.get("targetId"),
            "created_at": d.get("createdAt"),
            "conversation_id": None,
        }
        rec = AuditLogRecord(mapped)
        if staff_email and staff_email.lower() not in (rec.staff_user_id or "").lower():
            continue
        if tool_name and tool_name.lower() not in (rec.tool_name or "").lower():
            continue
        out.append(rec)
    out.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]


def _legacy_by_conversation(conversation_id: str, limit: int = 100) -> List[AuditLogRecord]:
    # Legacy docs rarely store conversation_id; filter targetId loosely
    all_legacy = _legacy_query(limit=limit * 2)
    cid = str(conversation_id)
    return [r for r in all_legacy if str(r.pending_action_id or "") == cid or str(r.conversation_id or "") == cid][:limit]
