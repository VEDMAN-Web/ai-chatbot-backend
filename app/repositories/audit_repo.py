"""
Audit log repository — Postgres / Firestore / both.

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
from app.repositories.store_mode import (
    reads_firestore_first,
    reads_postgres,
    require_firestore_client,
    store_mode,
    writes_firestore,
    writes_postgres,
)

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


def _mode() -> str:
    return store_mode("STORE_AUDIT")


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

    mode = _mode()
    wrote = False
    pg_ok = False
    errors: List[str] = []
    pg_required = writes_postgres(mode) and db is not None

    if pg_required:
        try:
            _pg_insert(db, record)
            wrote = True
            pg_ok = True
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            errors.append(f"postgres:{e}")
            logger.error("Audit postgres write failed: %s", e)

    if writes_firestore(mode):
        client = fs.admin_fs_client()
        if mode == "firestore":
            client = require_firestore_client(client, store_name="audit_logs")
        if client is not None:
            try:
                fs.set_document(client, COLLECTION, record.id, record.to_firestore_dict())
                wrote = True
            except Exception as e:
                errors.append(f"firestore:{e}")
                logger.error("Audit firestore write failed: %s", e)

    # Dual-store: Postgres failure is hard-fail even if Firestore succeeded
    # (unit tests mock only SQL; production both-mode must not silently drop SQL).
    if pg_required and not pg_ok:
        raise RuntimeError("; ".join(errors) or "audit postgres write failed")
    if not wrote:
        raise RuntimeError("; ".join(errors) or "audit write failed")
    return record


def _pg_insert(db: Any, record: AuditLogRecord) -> None:
    from app.db.models import AuditLog

    row = AuditLog(
        id=uuid.UUID(record.id),
        staff_user_id=record.staff_user_id,
        tool_name=record.tool_name,
        tool_arguments=record.tool_arguments,
        proposed_payload=record.proposed_payload,
        confirmed_by_staff=record.confirmed_by_staff,
        execution_result=record.execution_result,
        status=record.status,
        error_message=record.error_message,
        conversation_id=record.conversation_id,
        pending_action_id=record.pending_action_id,
        request_id=record.request_id,
        created_at=record.created_at.replace(tzinfo=None) if record.created_at.tzinfo else record.created_at,
    )
    db.add(row)
    db.commit()
    try:
        db.refresh(row)
        record.id = str(row.id)
        record.created_at = row.created_at if getattr(row.created_at, "tzinfo", None) else (
            row.created_at.replace(tzinfo=timezone.utc) if row.created_at else record.created_at
        )
    except Exception:
        pass


def update_fields(db: Any, audit_id: str, **fields: Any) -> Optional[AuditLogRecord]:
    mode = _mode()
    record = get_by_id(db, audit_id)
    if record is None:
        return None
    for key in (
        "status", "execution_result", "error_message", "proposed_payload",
        "tool_arguments", "confirmed_by_staff",
    ):
        if key in fields:
            setattr(record, key, fields[key])
    if writes_firestore(mode):
        client = fs.admin_fs_client()
        if client is not None:
            fs.set_document(client, COLLECTION, record.id, record.to_firestore_dict(), merge=True)
    if writes_postgres(mode) and db is not None:
        try:
            from app.db.models import AuditLog
            row = db.query(AuditLog).filter(AuditLog.id == uuid.UUID(str(audit_id))).first()
            if row:
                for key in ("status", "execution_result", "error_message", "proposed_payload", "tool_arguments", "confirmed_by_staff"):
                    if key in fields:
                        setattr(row, key, fields[key])
                db.commit()
        except Exception as e:
            logger.warning("Audit postgres update failed: %s", e)
    return record


def get_by_id(db: Any, audit_id: str) -> Optional[AuditLogRecord]:
    mode = _mode()
    if reads_firestore_first(mode):
        client = fs.admin_fs_client()
        data = fs.get_document(client, COLLECTION, audit_id) if client else None
        if data:
            return AuditLogRecord(data)
    if reads_postgres(mode) and db is not None:
        try:
            from app.db.models import AuditLog
            row = db.query(AuditLog).filter(AuditLog.id == uuid.UUID(str(audit_id))).first()
            if row:
                return AuditLogRecord({
                    "id": str(row.id),
                    "staff_user_id": row.staff_user_id,
                    "tool_name": row.tool_name,
                    "tool_arguments": row.tool_arguments,
                    "proposed_payload": row.proposed_payload,
                    "confirmed_by_staff": row.confirmed_by_staff,
                    "execution_result": row.execution_result,
                    "status": row.status,
                    "error_message": row.error_message,
                    "conversation_id": row.conversation_id,
                    "pending_action_id": row.pending_action_id,
                    "request_id": row.request_id,
                    "created_at": row.created_at,
                })
        except Exception:
            pass
    return None


def list_by_conversation(db: Any, conversation_id: str, limit: int = 100) -> List[AuditLogRecord]:
    mode = _mode()
    rows: List[AuditLogRecord] = []
    if reads_firestore_first(mode):
        rows = _fs_query(conversation_id=conversation_id, limit=limit)
    if (not rows) and reads_postgres(mode) and db is not None:
        rows = _pg_query(db, conversation_id=conversation_id, limit=limit)
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
    mode = _mode()
    records: List[AuditLogRecord] = []

    if reads_firestore_first(mode):
        records = _fs_query(
            staff_email=staff_email,
            tool_name=tool_name,
            status=status,
            start_date=start_date,
            end_date=end_date,
            conversation_id=conversation_id,
            limit=limit + offset + 200,
        )
    pg_records: List[AuditLogRecord] = []
    if reads_postgres(mode) and db is not None:
        pg_records = _pg_query(
            db,
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

    if reads_firestore_first(mode):
        merged = _dedupe_merge(records, pg_records, legacy)
    else:
        merged = _dedupe_merge(pg_records, records, legacy)

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


def _pg_query(
    db: Any,
    *,
    staff_email: Optional[str] = None,
    tool_name: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    conversation_id: Optional[str] = None,
    limit: int = 100,
) -> List[AuditLogRecord]:
    from app.db.models import AuditLog

    try:
        query = db.query(AuditLog)
        if staff_email:
            query = query.filter(AuditLog.staff_user_id.ilike(f"%{staff_email.strip()}%"))
        if tool_name:
            query = query.filter(AuditLog.tool_name.ilike(f"%{tool_name.strip()}%"))
        if status:
            query = query.filter(AuditLog.status.ilike(status.strip()))
        if conversation_id:
            query = query.filter(AuditLog.conversation_id == str(conversation_id))
        if start_date:
            query = query.filter(AuditLog.created_at >= start_date)
        if end_date:
            query = query.filter(AuditLog.created_at <= end_date)
        rows = query.order_by(AuditLog.created_at.desc()).limit(limit).all()
        return [
            AuditLogRecord({
                "id": str(r.id),
                "staff_user_id": r.staff_user_id,
                "tool_name": r.tool_name,
                "tool_arguments": r.tool_arguments,
                "proposed_payload": r.proposed_payload,
                "confirmed_by_staff": r.confirmed_by_staff,
                "execution_result": r.execution_result,
                "status": r.status,
                "error_message": r.error_message,
                "conversation_id": r.conversation_id,
                "pending_action_id": r.pending_action_id,
                "request_id": r.request_id,
                "created_at": r.created_at,
            })
            for r in rows or []
        ]
    except Exception as e:
        logger.warning("Audit postgres query failed: %s", e)
        return []


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
