from typing import Optional, Dict, Any, FrozenSet, List
from datetime import datetime, timezone
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger(__name__)

AUDIT_WRITE_FAILED_MSG = "Could not record the audit log, nothing was changed"

# Keys that must never be stored in plaintext in audit logs
SENSITIVE_KEYS = {
    "api_key", "token", "secret", "password", "authorization",
    "anthropic_api_key", "openai_api_key", "client_secret", "credential"
}

# Read-only / non-mutating tools: audit failure must never crash the chat turn
READ_ONLY_AUDIT_TOOLS: FrozenSet[str] = frozenset({
    "search_properties_full",
    "get_database_inventory_stats",
    "get_property_knowledge",
    "fetch_drive_file",
    "generate_report",
    "get_buyer_leads",
    "check_google_workspace_status",
    "generate_client_offer_dossier",
    "generate_property_brochure",
    "get_batch_operations_history",
    "admin:read_chat_logs",
    "admin:read_audit_logs",
    "admin:read_visitor_logs",
    "auth:login",
    "auth:logout",
    "auth:login_failed",
})


class AuditPersistenceError(RuntimeError):
    """Raised when a write-path audit row cannot be persisted."""

    def __init__(self, message: str = AUDIT_WRITE_FAILED_MSG):
        super().__init__(message)


def is_read_only_audit_tool(tool_name: Optional[str]) -> bool:
    """True for inventory/search/report/log reads — audit failure must not raise."""
    if not tool_name:
        return False
    name = str(tool_name).strip()
    if name in READ_ONLY_AUDIT_TOOLS:
        return True
    low = name.lower()
    if low.startswith("admin:read") or low.startswith("auth:"):
        return True
    if "list" in low and ("log" in low or "report" in low or "lead" in low):
        return True
    return False


def sanitize_payload(obj: Any) -> Any:
    """Recursively scrub sensitive keys and credentials from logged payloads."""
    if isinstance(obj, dict):
        sanitized = {}
        for k, v in obj.items():
            k_low = k.lower()
            if k_low.endswith("_tokens") or k_low in ["token_count", "tokens_count"]:
                sanitized[k] = sanitize_payload(v)
            elif any(sens in k_low for sens in SENSITIVE_KEYS):
                sanitized[k] = "[REDACTED_SECRET]"
            else:
                sanitized[k] = sanitize_payload(v)
        return sanitized
    elif isinstance(obj, list):
        return [sanitize_payload(item) for item in obj]
    return obj


def record_audit_log(
    db: Session,
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
    raise_on_failure: Optional[bool] = None,
):
    """
    Creates an immutable audit log entry via audit_repo (Postgres / Firestore / both).
    Does not write the legacy admin_audit_log mirror.

    On failure: read-only tools return an ephemeral entry; write tools raise
    AuditPersistenceError with AUDIT_WRITE_FAILED_MSG.
    """
    from app.core.config import settings
    from app.repositories import audit_repo

    safe_args = sanitize_payload(tool_arguments or {})
    safe_payload = sanitize_payload(proposed_payload or {})

    class _Ephemeral:
        pass

    if not getattr(settings, "ENABLE_AUDIT_LOGGING", False):
        entry = _Ephemeral()
        entry.id = f"audit_ephemeral_{datetime.now(timezone.utc).timestamp()}"
        entry.staff_user_id = staff_user_id
        entry.tool_name = tool_name
        entry.tool_arguments = safe_args
        entry.proposed_payload = safe_payload
        entry.confirmed_by_staff = confirmed_by_staff
        entry.execution_result = execution_result
        entry.status = status
        entry.error_message = error_message
        entry.conversation_id = conversation_id
        entry.pending_action_id = pending_action_id
        entry.request_id = request_id
        entry.created_at = datetime.now(timezone.utc)
        setattr(entry, "is_ephemeral", True)
        setattr(entry, "audit_logging_disabled", True)
        logger.warning(
            "ENABLE_AUDIT_LOGGING is off — not persisting audit for tool=%s pending=%s "
            "(ephemeral id %s must not be shown as a real audit id)",
            tool_name,
            pending_action_id,
            entry.id,
        )
        return entry

    try:
        record = audit_repo.create(
            db,
            tool_name=tool_name,
            staff_user_id=staff_user_id,
            tool_arguments=safe_args,
            proposed_payload=safe_payload,
            confirmed_by_staff=confirmed_by_staff,
            execution_result=execution_result,
            status=status,
            error_message=error_message,
            conversation_id=conversation_id,
            pending_action_id=pending_action_id,
            request_id=request_id,
        )
        return record
    except Exception as e:
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        logger.error(f"Failed to record audit log: {e}")
        entry = _Ephemeral()
        entry.id = f"audit_failed_{datetime.now(timezone.utc).timestamp()}"
        entry.staff_user_id = staff_user_id
        entry.tool_name = tool_name
        entry.tool_arguments = safe_args
        entry.proposed_payload = safe_payload
        entry.confirmed_by_staff = confirmed_by_staff
        entry.execution_result = execution_result
        entry.status = status
        entry.error_message = error_message
        entry.conversation_id = conversation_id
        entry.pending_action_id = pending_action_id
        entry.request_id = request_id
        entry.created_at = datetime.now(timezone.utc)
        setattr(entry, "is_ephemeral", True)
        setattr(entry, "audit_write_failed", True)
        should_raise = (
            raise_on_failure
            if raise_on_failure is not None
            else not is_read_only_audit_tool(tool_name)
        )
        if should_raise:
            raise AuditPersistenceError(AUDIT_WRITE_FAILED_MSG) from e
        return entry


def query_admin_activity_logs(
    db: Session,
    admin_email: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
    source: str = "all"
) -> Dict[str, Any]:
    """
    Queries administrative activity logs via audit_repo (merges legacy admin_audit_log).
    """
    from app.repositories import audit_repo

    result = audit_repo.query(
        db,
        staff_email=admin_email,
        tool_name=action,
        status=status,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    records = []
    for row in result.get("logs") or []:
        records.append({
            "id": row.id,
            "admin_email": row.staff_user_id,
            "staff_user_id": row.staff_user_id,
            "action": row.tool_name,
            "tool_name": row.tool_name,
            "status": row.status,
            "confirmed_by_staff": row.confirmed_by_staff,
            "error_message": row.error_message,
            "conversation_id": row.conversation_id,
            "pending_action_id": row.pending_action_id,
            "created_at": row.created_at.isoformat() if getattr(row.created_at, "isoformat", None) else str(row.created_at),
            "source": "audit_logs",
            "tool_arguments": row.tool_arguments,
            "proposed_payload": row.proposed_payload,
            "execution_result": row.execution_result,
        })
    return {
        "total": result.get("total") or len(records),
        "logs": records,
        "limit": limit,
        "offset": offset,
        "source": source,
    }


def query_data_change_history(
    db: Session,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    updated_by: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Data-history view: audit rows that look like listing/property mutations.
    Merges legacy admin_audit_log via audit_repo (no new mirror writes).
    """
    from app.repositories import audit_repo

    result = audit_repo.query(
        db,
        staff_email=updated_by,
        limit=max(limit + offset + 200, 200),
        offset=0,
    )
    history_items: List[Dict[str, Any]] = []
    mutate_hints = (
        "create_or_update_listing", "propose_listing_update", "ingest_property",
        "execute_batch", "manage_property_images", "admin_rest_", "undo_batch",
        "property", "listing",
    )
    for row in result.get("logs") or []:
        tool = (row.tool_name or "")
        tool_l = tool.lower()
        payload = row.proposed_payload if isinstance(row.proposed_payload, dict) else {}
        extracted_target_id = (
            payload.get("property_id")
            or payload.get("target_property_id")
            or row.pending_action_id
            or "N/A"
        )
        extracted_target_type = "property"
        if "user" in tool_l or "auth" in tool_l:
            extracted_target_type = "user"
        elif "batch" in tool_l:
            extracted_target_type = "batch_operation"

        if target_type and extracted_target_type != target_type:
            continue
        if target_id and str(target_id) not in str(extracted_target_id) and str(target_id) not in str(payload):
            continue
        if not any(h in tool_l for h in mutate_hints) and not target_id:
            continue

        changes_summary = {}
        if isinstance(payload.get("fields"), dict):
            changes_summary = payload.get("fields")
        elif isinstance(payload, dict):
            changes_summary = {
                k: v for k, v in payload.items()
                if k not in ("action", "email", "property_id")
            }

        history_items.append({
            "change_id": str(row.id),
            "updated_by": row.staff_user_id or "system",
            "action": tool,
            "target_type": extracted_target_type,
            "target_id": str(extracted_target_id),
            "changes_summary": changes_summary,
            "full_payload": payload,
            "status": row.status,
            "confirmed_by_staff": row.confirmed_by_staff,
            "execution_result": row.execution_result,
            "timestamp": row.created_at.isoformat() if getattr(row.created_at, "isoformat", None) else str(row.created_at),
        })

    history_items.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
    page = history_items[offset: offset + limit]
    return {
        "total": len(history_items),
        "limit": limit,
        "offset": offset,
        "history": page,
    }
