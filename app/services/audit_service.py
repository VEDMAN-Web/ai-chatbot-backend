import ast
import json
from typing import Optional, Dict, Any, FrozenSet, List, Tuple
from datetime import datetime, timezone
import logging
import re

logger = logging.getLogger(__name__)

AUDIT_WRITE_FAILED_MSG = "Could not record the audit log, nothing was changed"

# Keys that must never be stored in plaintext in audit logs
SENSITIVE_KEYS = {
    "api_key", "token", "secret", "password", "authorization",
    "anthropic_api_key", "openai_api_key", "client_secret", "credential"
}

# Keys that hold message content (it may contain a client's personal data). The audit trail keeps who the
# message went to and its subject, plus the size of the content, but not the content itself.
CONTENT_KEYS = {"body", "html_body"}
EXACT_SENSITIVE_KEYS = {"otp", "pin", "pwd", "cookie", "set-cookie", "x-api-key"}

# Credentials that appear inside text values (error messages, URLs, headers copied into a result).
_TEXT_SECRET_PATTERNS = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED_SECRET]"),
    (re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}"), "Basic [REDACTED_SECRET]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{3,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{10,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\b1//[A-Za-z0-9._-]{10,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\bact_[0-9a-fA-F]{16,}"), "[REDACTED_SECRET]"),
    (re.compile(r"(?i)([?&;\s](?:access_token|refresh_token|id_token|token|api_key|apikey|key|secret|client_secret|password|signature|sig|code)=)[^&\s\"']+"),
     r"\1[REDACTED_SECRET]"),
]


def scrub_text(value: Optional[str]) -> Optional[str]:
    """Removes credentials from free text before it is stored in the audit trail."""
    if not isinstance(value, str) or not value:
        return value
    for pattern, replacement in _TEXT_SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value
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
    """Recursively scrub sensitive keys, message content and credentials from logged payloads."""
    if isinstance(obj, dict):
        sanitized = {}
        for k, v in obj.items():
            k_low = str(k).lower()
            # Do not redact LLM usage metrics like 'total_tokens', 'prompt_tokens', etc.
            if k_low.endswith("_tokens") or k_low in ["token_count", "tokens_count"]:
                sanitized[k] = sanitize_payload(v)
            elif k_low in EXACT_SENSITIVE_KEYS or any(sens in k_low for sens in SENSITIVE_KEYS):
                sanitized[k] = "[REDACTED_SECRET]"
            elif k_low in CONTENT_KEYS and isinstance(v, str):
                sanitized[k] = f"[CONTENT NOT LOGGED: {len(v)} characters]"
            else:
                sanitized[k] = sanitize_payload(v)
        return sanitized
    elif isinstance(obj, (list, tuple)):
        return [sanitize_payload(item) for item in obj]
    elif isinstance(obj, str):
        return scrub_text(obj)
    return obj


def record_audit_log(
    db: Any,
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
    Creates an immutable audit log entry via audit_repo (Firestore).
    Does not write the legacy admin_audit_log mirror.

    On failure: read-only tools return an ephemeral entry; write tools raise
    AuditPersistenceError with AUDIT_WRITE_FAILED_MSG.
    """
    from app.core.config import settings
    from app.repositories import audit_repo

    safe_args = sanitize_payload(tool_arguments or {})
    safe_payload = sanitize_payload(proposed_payload or {})
    execution_result = scrub_text(execution_result)
    error_message = scrub_text(error_message)

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


_REPEAT_MIN_TEXT = 40   # shorter values ("500 THB", "Sold Out") are never treated as repeats


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _strip_repeats(value: Any, seen: set) -> Any:
    """Copy of `value` where every object or long text that already appeared (in `seen`) is left out, and empty parts are dropped."""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if not item:
                    continue
                marker = _canon(item)
                if marker in seen:
                    continue
                seen.add(marker)
                item = _strip_repeats(item, seen)
                if item in ({}, []):
                    continue
            elif isinstance(item, str):
                if not item.strip():
                    continue
                if len(item) >= _REPEAT_MIN_TEXT:
                    if item in seen:
                        continue
                    seen.add(item)
            elif item is None:
                continue
            cleaned[key] = item
        return cleaned
    if isinstance(value, list):
        return [_strip_repeats(item, seen) if isinstance(item, (dict, list)) else item for item in value]
    return value


def _as_object(value: Any) -> Any:
    """A stored result may be JSON text or the text of a Python dict; give back the object when it is one."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            for parse in (json.loads, ast.literal_eval):
                try:
                    return parse(text)
                except Exception:
                    continue
    return value


def dedupe_log_sections(first: Any, second: Any, result: Any, status: Optional[str] = None) -> Tuple[Any, Any, Any]:
    """One log row carries the same data in several places. Keep each piece where it first appears:
    `first`, then `second`, then `result`. Returns the three cleaned sections (`result` stays a non-empty text)."""
    first = _as_object(first)
    second = _as_object(second)
    seen: set = set()
    if isinstance(second, dict) and isinstance(second.get("fields"), dict) and second.get("fields") == second.get("fields_to_update"):
        second = {k: v for k, v in second.items() if k != "fields"}   # the panel reads fields_to_update
    first_clean = _strip_repeats(first, seen) if isinstance(first, (dict, list)) else first
    second_clean = _strip_repeats(second, seen) if isinstance(second, (dict, list)) else second
    result_obj = _as_object(result)
    if isinstance(result_obj, (dict, list)):
        cleaned = _strip_repeats(result_obj, seen)
        result_clean = json.dumps(cleaned, default=str) if cleaned else (status or "OK")
    elif isinstance(result_obj, str) and len(result_obj) >= _REPEAT_MIN_TEXT and result_obj in seen:
        result_clean = status or "OK"
    else:
        result_clean = result_obj
    return first_clean, second_clean, result_clean


def _activity_key(record: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """The same activity written to both stores has different ids; this is what identifies it."""
    return (
        str(record.get("action") or "").lower(),
        str(record.get("admin_email") or "").lower(),
        str(record.get("created_at") or "")[:19],
        str(record.get("pending_action_id") or record.get("conversation_id") or ""),
    )


def query_admin_activity_logs(
    db: Any,
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
        arguments, proposed, exec_result = dedupe_log_sections(
            row.tool_arguments or {}, row.proposed_payload or {}, row.execution_result, row.status
        )
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
            "arguments": arguments or {},
            "tool_arguments": arguments or {},
            "proposed_payload": proposed or {},
            "execution_result": exec_result,
        })
    return {
        "total": result.get("total") or len(records),
        "logs": records,
        "limit": limit,
        "offset": offset,
        "source": source,
    }


def query_data_change_history(
    db: Any,
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

        changes_summary, full_payload, exec_result = dedupe_log_sections(changes_summary, payload, row.execution_result, row.status)
        history_items.append({
            "change_id": str(row.id),
            "updated_by": row.staff_user_id or "system",
            "action": tool,
            "target_type": extracted_target_type,
            "target_id": str(extracted_target_id),
            "changes_summary": changes_summary or {},
            "full_payload": full_payload or {},
            "status": row.status,
            "confirmed_by_staff": row.confirmed_by_staff,
            "execution_result": exec_result,
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


class AuditUnavailable(Exception):
    """The audit trail cannot be written, so a sensitive change must not go ahead."""


def record_sensitive_audit(db: Any, **kwargs: Any) -> Any:
    """
    Fail-safe audit for sensitive writes (corrections, roles, imports). The entry is written BEFORE the change.
    If auditing is switched off or the entry cannot be stored, AuditUnavailable is raised and the caller must
    refuse the change, instead of proceeding without the required trail.
    """
    from app.core.config import settings

    if not getattr(settings, "ENABLE_AUDIT_LOGGING", False):
        raise AuditUnavailable("Audit logging is disabled, so this change is blocked.")
    try:
        entry = record_audit_log(db=db, **kwargs)
    except Exception as exc:
        raise AuditUnavailable("The audit trail could not be written, so this change is blocked.") from exc
    if getattr(entry, "is_ephemeral", False):
        raise AuditUnavailable("The audit trail could not be written, so this change is blocked.")
    return entry
