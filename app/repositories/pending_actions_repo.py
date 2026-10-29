"""
Pending actions repository — Firestore.

Callers must use this module; never query PendingAction or the Firestore client directly.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.repositories import _fs_utils as fs
from app.repositories.store_mode import require_firestore_client

logger = logging.getLogger(__name__)

COLLECTION = "pending_actions"

ACTION_STATUS_PENDING = "PENDING"
ACTION_STATUS_CONFIRMED = "CONFIRMED"
ACTION_STATUS_EXECUTED = "EXECUTED"
ACTION_STATUS_REJECTED = "REJECTED"
ACTION_STATUS_EXPIRED = "EXPIRED"
ACTION_STATUS_FAILED = "FAILED"
ACTION_STATUS_SUPERSEDED = "SUPERSEDED"


class PendingActionRecord:
    """Duck-typed stand-in for the SQLAlchemy PendingAction row."""

    def __init__(self, data: Dict[str, Any]):
        self._data = dict(data or {})
        self.id = str(self._data.get("id") or "")
        self.confirmation_token = self._data.get("confirmation_token") or ""
        self.staff_user_id = (
            self._data.get("staff_email")
            or self._data.get("staff_user_id")
            or ""
        )
        self.staff_email = self.staff_user_id
        self.tool_name = self._data.get("tool_name") or ""
        self.action_payload = self._data.get("action_payload") or {}
        self.status = self._data.get("status") or ACTION_STATUS_PENDING
        self.conversation_id = self._data.get("conversation_id")
        self.execution_result = self._data.get("result")
        if self.execution_result is None:
            self.execution_result = self._data.get("execution_result")
        self.result = self.execution_result
        self.error_message = self._data.get("error") or self._data.get("error_message")
        self.error = self.error_message
        self.expires_at = fs.parse_dt(self._data.get("expires_at")) or datetime.now(timezone.utc)
        self.confirmed_at = fs.parse_dt(self._data.get("confirmed_at"))
        self.executed_at = fs.parse_dt(self._data.get("executed_at"))
        self.created_at = fs.parse_dt(self._data.get("created_at")) or datetime.now(timezone.utc)
        self.confirmed_by = self._data.get("confirmed_by")
        self.undo_info = self._data.get("undo_info")

    def to_firestore_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "confirmation_token": self.confirmation_token,
            "staff_email": self.staff_email,
            "staff_user_id": self.staff_user_id,
            "tool_name": self.tool_name,
            "action_payload": self.action_payload or {},
            "status": self.status,
            "conversation_id": self.conversation_id,
            "result": self.execution_result,
            "execution_result": self.execution_result,
            "error": self.error_message,
            "error_message": self.error_message,
            "expires_at": fs.to_iso(self.expires_at),
            "confirmed_at": fs.to_iso(self.confirmed_at),
            "executed_at": fs.to_iso(self.executed_at),
            "created_at": fs.to_iso(self.created_at),
            "confirmed_by": self.confirmed_by,
            "undo_info": self.undo_info,
        }


def _is_expired(action: PendingActionRecord, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    exp = action.expires_at
    if exp is None:
        return False
    if exp.tzinfo is None:
        return exp < now.replace(tzinfo=None)
    return exp < now


def create(
    db: Any,
    *,
    staff_email: str,
    tool_name: str,
    action_payload: Dict[str, Any],
    conversation_id: Optional[str] = None,
    expires_in_minutes: Optional[int] = None,
    confirmation_token: Optional[str] = None,
) -> PendingActionRecord:
    from app.core.config import settings

    minutes = (
        expires_in_minutes
        if expires_in_minutes is not None
        else getattr(settings, "ADMIN_PENDING_ACTION_TTL_MIN", None)
        or settings.ACTION_EXPIRATION_MINUTES
    )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=int(minutes or 30))
    token = confirmation_token or f"act_{secrets.token_hex(16)}"
    doc_id = str(uuid.uuid4())
    record = PendingActionRecord({
        "id": doc_id,
        "confirmation_token": token,
        "staff_email": staff_email,
        "staff_user_id": staff_email,
        "tool_name": tool_name,
        "action_payload": action_payload or {},
        "status": ACTION_STATUS_PENDING,
        "conversation_id": conversation_id,
        "expires_at": expires_at,
        "created_at": now,
    })

    client = require_firestore_client(fs.admin_fs_client(), store_name="pending_actions")
    if client is not None:
        fs.set_document(client, COLLECTION, record.id, record.to_firestore_dict())
    return record


def get(db: Any, pending_action_id: str) -> PendingActionRecord:
    action: Optional[PendingActionRecord] = None

    action = _fs_get(pending_action_id)

    if action is None:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pending action '{pending_action_id}' not found.",
        )

    if action.status == ACTION_STATUS_PENDING and _is_expired(action):
        action = update_fields(
            db,
            action.id,
            status=ACTION_STATUS_EXPIRED,
            error="Expired",
        ) or action
        action.status = ACTION_STATUS_EXPIRED
    return action


def _fs_get(pending_action_id: str) -> Optional[PendingActionRecord]:
    client = fs.admin_fs_client()
    if client is None or not pending_action_id:
        return None
    needle = str(pending_action_id).strip()
    # Direct document id (UUID) — never full-collection scan for these
    data = fs.get_document(client, COLLECTION, needle)
    if data:
        return PendingActionRecord(data)
    # Token lookup only (act_…). Avoid streaming the whole collection on every UUID miss
    # (that hangs unit tests against a live Firestore client).
    if not needle.startswith("act_"):
        return None
    try:
        query = client.collection(COLLECTION).where("confirmation_token", "==", needle).limit(1)
        snaps = list(query.stream())
        if snaps:
            return PendingActionRecord(fs.doc_to_dict(snaps[0]))
    except Exception as e:
        logger.debug("Firestore token query failed: %s", e)
    return None


def list_by_conversation(
    db: Any,
    conversation_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 20,
) -> List[PendingActionRecord]:
    rows: List[PendingActionRecord] = []
    rows = _fs_list_conversation(conversation_id, status=status, limit=limit)
    return rows


def list_open_pending(db: Any, conversation_id: str, limit: int = 10) -> List[PendingActionRecord]:
    items = list_by_conversation(db, conversation_id, status=ACTION_STATUS_PENDING, limit=limit)
    # Lazy-expire
    out = []
    for a in items:
        if _is_expired(a):
            update_fields(db, a.id, status=ACTION_STATUS_EXPIRED, error="Expired")
            continue
        out.append(a)
    return out


def _fs_list_conversation(
    conversation_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 20,
) -> List[PendingActionRecord]:
    client = fs.admin_fs_client()
    if client is None:
        return []
    cid = str(conversation_id)
    docs: List[Dict[str, Any]] = []
    try:
        query = client.collection(COLLECTION).where("conversation_id", "==", cid)
        docs = [fs.doc_to_dict(s) for s in query.stream()]
    except AttributeError:
        # Dict FakeFirestore without .where — filter an in-memory stream
        docs = [
            d for d in (fs.doc_to_dict(s) for s in fs.stream_collection(client, COLLECTION))
            if str(d.get("conversation_id") or "") == cid
        ]
    except Exception as e:
        logger.warning("Firestore pending list by conversation failed: %s", e)
        return []
    if status:
        docs = [d for d in docs if str(d.get("status") or "") == status]
    docs.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return [PendingActionRecord(d) for d in docs[:limit]]


def list_pending_for_property(
    db: Any,
    *,
    property_id: str,
    conversation_id: Optional[str] = None,
) -> List[PendingActionRecord]:
    """PENDING rows that target the same property (for supersede)."""
    candidates: List[PendingActionRecord] = []
    if conversation_id:
        candidates = list_by_conversation(db, conversation_id, status=ACTION_STATUS_PENDING, limit=50)
    else:
        client = fs.admin_fs_client()
        if client:
            for snap in fs.stream_collection(client, COLLECTION):
                d = fs.doc_to_dict(snap)
                if str(d.get("status") or "") == ACTION_STATUS_PENDING:
                    candidates.append(PendingActionRecord(d))

    pid = str(property_id).strip().lower()
    matched = []
    for a in candidates:
        payload = a.action_payload or {}
        old_pid = (
            payload.get("property_id")
            or payload.get("target_property_id")
            or ((payload.get("change_diff") or {}).get("property_id") if isinstance(payload.get("change_diff"), dict) else None)
        )
        if old_pid and str(old_pid).strip().lower() == pid:
            matched.append(a)
    return matched


def update_fields(
    db: Any,
    pending_action_id: str,
    **fields: Any,
) -> Optional[PendingActionRecord]:
    current = None
    try:
        current = get(db, pending_action_id)
    except Exception:
        current = _fs_get(pending_action_id)
    if current is None:
        return None

    mapping = {
        "status": "status",
        "result": "execution_result",
        "execution_result": "execution_result",
        "error": "error_message",
        "error_message": "error_message",
        "confirmed_by": "confirmed_by",
        "confirmed_at": "confirmed_at",
        "executed_at": "executed_at",
        "undo_info": "undo_info",
        "action_payload": "action_payload",
    }
    for key, attr in mapping.items():
        if key in fields:
            setattr(current, attr, fields[key])
            if attr == "execution_result":
                current.result = fields[key]
            if attr == "error_message":
                current.error = fields[key]

    client = fs.admin_fs_client()
    if client is not None:
        fs.set_document(client, COLLECTION, current.id, current.to_firestore_dict(), merge=True)
    return current


def delete(db: Any, pending_action_id: str) -> None:
    client = fs.admin_fs_client()
    if client is not None:
        fs.delete_document(client, COLLECTION, pending_action_id)


def _as_record(action: Any) -> PendingActionRecord:
    if isinstance(action, PendingActionRecord):
        return action
    return PendingActionRecord({
        "id": str(getattr(action, "id", "")),
        "confirmation_token": getattr(action, "confirmation_token", None),
        "staff_email": getattr(action, "staff_user_id", None) or getattr(action, "staff_email", None),
        "staff_user_id": getattr(action, "staff_user_id", None) or getattr(action, "staff_email", None),
        "tool_name": getattr(action, "tool_name", None),
        "action_payload": getattr(action, "action_payload", None) or {},
        "status": getattr(action, "status", None),
        "conversation_id": getattr(action, "conversation_id", None),
        "result": getattr(action, "execution_result", None),
        "error": getattr(action, "error_message", None),
        "expires_at": getattr(action, "expires_at", None),
        "confirmed_at": getattr(action, "confirmed_at", None),
        "executed_at": getattr(action, "executed_at", None),
        "created_at": getattr(action, "created_at", None),
    })


def claim_confirm(
    db: Any,
    pending_action_id: str,
    *,
    staff_email: str,
    known_action: Any = None,
) -> PendingActionRecord:
    """
    Atomically claim a PENDING (non-expired) action for confirm.
    Double-confirm races: only the first caller gets CONFIRMED; others see current status.
    """
    now = datetime.now(timezone.utc)

    client = fs.admin_fs_client()
    # Skip clients that cannot run transactions (mocks / broken stubs) without logging noise.
    if client is not None and hasattr(client, "run_transaction"):
        def _txn(txn):
            ref = client.collection(COLLECTION).document(str(pending_action_id))
            snap = txn.get(ref)
            data = None
            if hasattr(snap, "to_dict"):
                exists = getattr(snap, "exists", True)
                data = snap.to_dict() if exists else None
            elif isinstance(snap, dict):
                data = snap
            if not data:
                # Fake overlay may return Snap with exists False
                raise LookupError("not_found")
            data = dict(data)
            data["id"] = data.get("id") or pending_action_id
            status = str(data.get("status") or "")
            if status != ACTION_STATUS_PENDING:
                rec = PendingActionRecord(data)
                setattr(rec, "claim_won", False)
                return rec
            exp = fs.parse_dt(data.get("expires_at"))
            if exp and (exp < now if exp.tzinfo else exp < now.replace(tzinfo=None)):
                data["status"] = ACTION_STATUS_EXPIRED
                data["error"] = "Expired"
                txn.set(ref, data, merge=True)
                rec = PendingActionRecord(data)
                setattr(rec, "claim_won", False)
                return rec
            data["status"] = ACTION_STATUS_CONFIRMED
            data["confirmed_by"] = staff_email
            data["confirmed_at"] = fs.to_iso(now)
            txn.set(ref, data, merge=True)
            rec = PendingActionRecord(data)
            setattr(rec, "claim_won", True)
            return rec

        try:
            claimed = fs.run_transaction(client, _txn)
            return claimed
        except LookupError:
            # Doc missing in FS — fall through to known_action
            pass
        except Exception as e:
            logger.warning("Firestore confirm transaction failed (%s); falling back", e)

    # Fallback path (no Firestore transaction available)
    action: Optional[PendingActionRecord] = None
    try:
        action = get(db, pending_action_id)
    except Exception:
        action = None
    if action is None and known_action is not None:
        action = _as_record(known_action)
    if action is None:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pending action '{pending_action_id}' not found.",
        )
    if action.status != ACTION_STATUS_PENDING:
        setattr(action, "claim_won", False)
        return action
    if _is_expired(action, now):
        expired = update_fields(db, action.id, status=ACTION_STATUS_EXPIRED, error="Expired") or action
        setattr(expired, "claim_won", False)
        return expired

    # Firestore transaction unavailable or the doc was missing: last writer wins.
    confirmed = update_fields(
        db,
        action.id,
        status=ACTION_STATUS_CONFIRMED,
        confirmed_by=staff_email,
        confirmed_at=now,
    ) or action
    setattr(confirmed, "claim_won", True)
    return confirmed


def claim_reject(
    db: Any,
    pending_action_id: str,
    *,
    staff_email: str,
    reason: Optional[str] = None,
) -> PendingActionRecord:
    now = datetime.now(timezone.utc)

    client = fs.admin_fs_client()
    if client is not None:
        def _txn(txn):
            ref = client.collection(COLLECTION).document(str(pending_action_id))
            snap = txn.get(ref)
            data = snap.to_dict() if hasattr(snap, "to_dict") else None
            if data is None and getattr(snap, "exists", False) is False:
                raise LookupError("not_found")
            if not data:
                raise LookupError("not_found")
            data = dict(data)
            data["id"] = data.get("id") or pending_action_id
            if str(data.get("status") or "") != ACTION_STATUS_PENDING:
                return PendingActionRecord(data)
            data["status"] = ACTION_STATUS_REJECTED
            data["error"] = reason or "Rejected by staff."
            data["error_message"] = data["error"]
            data["confirmed_by"] = staff_email
            data["confirmed_at"] = fs.to_iso(now)
            txn.set(ref, data, merge=True)
            return PendingActionRecord(data)

        try:
            rejected = fs.run_transaction(client, _txn)
            return rejected
        except LookupError:
            from fastapi import HTTPException, status
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pending action not found.")
        except Exception as e:
            logger.warning("Firestore reject transaction failed (%s); falling back", e)

    action = get(db, pending_action_id)
    if action.status != ACTION_STATUS_PENDING:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action cannot be rejected. Current status is '{action.status}'.",
        )
    return update_fields(
        db,
        action.id,
        status=ACTION_STATUS_REJECTED,
        error=reason or "Rejected by staff.",
        confirmed_by=staff_email,
        confirmed_at=now,
    ) or action
