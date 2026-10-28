import ast
import json
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from sqlalchemy.orm import Session
import copy
import logging
import re

from app.db.models import AuditLog

logger = logging.getLogger(__name__)

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
    request_id: Optional[str] = None
) -> AuditLog:
    """
    Creates an immutable, append-only audit log entry in the audit_logs table.
    Enforces server-side sanitization of secrets.
    """
    safe_args = sanitize_payload(tool_arguments or {})
    safe_payload = sanitize_payload(proposed_payload or {})
    execution_result = scrub_text(execution_result)
    error_message = scrub_text(error_message)

    log_entry = AuditLog(
        staff_user_id=staff_user_id,
        tool_name=tool_name,
        tool_arguments=safe_args,
        proposed_payload=safe_payload,
        confirmed_by_staff=confirmed_by_staff,
        execution_result=execution_result,
        status=status,
        error_message=error_message,
        conversation_id=conversation_id,
        pending_action_id=pending_action_id,
        request_id=request_id,
        created_at=datetime.now(timezone.utc)
    )

    from app.core.config import settings
    if not getattr(settings, "ENABLE_AUDIT_LOGGING", False):
        # Keep an in-memory/ephemeral entry for debugging — never present as a real audit id
        log_entry.id = f"audit_ephemeral_{datetime.now(timezone.utc).timestamp()}"
        setattr(log_entry, "is_ephemeral", True)
        setattr(log_entry, "audit_logging_disabled", True)
        logger.warning(
            "ENABLE_AUDIT_LOGGING is off — not persisting audit for tool=%s pending=%s "
            "(ephemeral id %s must not be shown as a real audit id)",
            tool_name,
            pending_action_id,
            log_entry.id,
        )
        return log_entry

    try:
        db.add(log_entry)
        db.commit()
        db.refresh(log_entry)

        # Sync to Firestore admin_audit_log collection
        if getattr(settings, "ENABLE_FIRESTORE_SYNC", False):
            try:
                from app.services.firestore_service import create_audit_log_in_firestore
                create_audit_log_in_firestore(
                    admin_email=staff_user_id or "system",
                    action=tool_name,
                    details={
                        "arguments": safe_args,
                        "payload": safe_payload,
                        "status": status,
                        "confirmed": confirmed_by_staff,
                        "error": error_message
                    },
                    target_type="action",
                    target_id=pending_action_id or conversation_id or "",
                    admin_uid=staff_user_id
                )
            except Exception as fe:
                logger.warning(f"Firestore audit log sync error: {fe}")

        return log_entry
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record audit log: {e}")
        raise e


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
    Queries administrative activity logs across PostgreSQL and Cloud Firestore.
    Provides filtering, pagination, and unified log structures.
    """
    records = []
    total_count = 0

    if source in ("postgres", "all"):
        query = db.query(AuditLog)

        if admin_email:
            query = query.filter(AuditLog.staff_user_id.ilike(f"%{admin_email.strip()}%"))

        if action:
            query = query.filter(AuditLog.tool_name.ilike(f"%{action.strip()}%"))

        if status:
            query = query.filter(AuditLog.status.ilike(status.strip()))

        if start_date:
            query = query.filter(AuditLog.created_at >= start_date)

        if end_date:
            query = query.filter(AuditLog.created_at <= end_date)

        total_count = query.count()
        pg_logs = query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()

        for l in pg_logs:
            arguments, proposed, result = dedupe_log_sections(
                l.tool_arguments or {}, l.proposed_payload or {}, l.execution_result, l.status
            )
            records.append({
                "id": str(l.id),
                "admin_email": l.staff_user_id or "system",
                "action": l.tool_name,
                "status": l.status,
                "arguments": arguments or {},
                "proposed_payload": proposed or {},
                "confirmed_by_staff": l.confirmed_by_staff,
                "execution_result": result,
                "error_message": l.error_message,
                "conversation_id": l.conversation_id,
                "pending_action_id": l.pending_action_id,
                "created_at": l.created_at.isoformat() if l.created_at else None,
                "source": "postgres"
            })

    # Include Firestore audit logs if requested
    if source in ("firestore", "all"):
        try:
            from app.services.firestore_service import get_audit_logs_from_firestore
            fs_logs = get_audit_logs_from_firestore(
                admin_email=admin_email,
                action=action,
                limit=limit
            )
            for fl in fs_logs:
                details = fl.get("details") or {}
                fs_status = details.get("status", "SUCCESS") if isinstance(details, dict) else "SUCCESS"
                arguments, proposed, result = dedupe_log_sections(
                    details.get("arguments", {}) if isinstance(details, dict) else {},
                    details.get("payload", {}) if isinstance(details, dict) else {},
                    details,   # the whole details object repeats the two above; only what is new is kept
                    fs_status,
                )
                records.append({
                    "id": fl.get("id"),
                    "admin_email": fl.get("adminEmail") or fl.get("adminUid") or "admin_system",
                    "action": fl.get("action"),
                    "status": fs_status,
                    "arguments": arguments or {},
                    "proposed_payload": proposed or {},
                    "confirmed_by_staff": details.get("confirmed", True) if isinstance(details, dict) else True,
                    "execution_result": result,
                    "error_message": details.get("error") if isinstance(details, dict) else None,
                    "conversation_id": fl.get("targetId") if fl.get("targetType") == "conversation" else None,
                    "pending_action_id": fl.get("targetId") if fl.get("targetType") == "action" else None,
                    "created_at": fl.get("createdAt"),
                    "source": "firestore"
                })
                total_count += 1
        except Exception as fe:
            logger.warning(f"Could not fetch Firestore logs for activity query: {fe}")

    # Deduplicate by ID, then by activity (the same action written to both stores has two ids; PostgreSQL wins)
    seen_ids = set()
    seen_activity = set()
    deduped = []
    for r in sorted(records, key=lambda x: (str(x.get("created_at") or "")[:19], x.get("source") == "postgres", str(x.get("created_at") or "")), reverse=True):
        rid = r.get("id")
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
        activity = _activity_key(r)
        if activity[2] and activity in seen_activity:
            continue
        seen_activity.add(activity)
        deduped.append(r)

    paged = deduped[offset : offset + limit] if source == "firestore" else deduped[:limit]

    return {
        "total": len(deduped) if source != "postgres" else total_count,
        "limit": limit,
        "offset": offset,
        "logs": paged
    }


def query_data_change_history(
    db: Session,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    updated_by: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> Dict[str, Any]:
    """
    Chronological audit trail of who updated which data across the platform
    (properties, users, batch operations, etc.).
    """
    query = db.query(AuditLog)

    if updated_by:
        query = query.filter(AuditLog.staff_user_id.ilike(f"%{updated_by.strip()}%"))

    mutation_tools = [
        "update_listing", "create_listing", "delete_listing",
        "batch_operation", "batch_file", "auth:register",
        "auth:change_password", "auth:reset_password",
        "confirm_action", "admin:update_user", "admin:delete_user"
    ]

    if target_type:
        tt = target_type.strip().lower()
        if "prop" in tt or "listing" in tt:
            query = query.filter(AuditLog.tool_name.in_(["update_listing", "create_listing", "delete_listing", "batch_operation"]))
        elif "user" in tt or "auth" in tt:
            query = query.filter(AuditLog.tool_name.in_(["auth:register", "auth:change_password", "auth:reset_password", "admin:update_user", "admin:delete_user"]))
        else:
            query = query.filter(AuditLog.tool_name.ilike(f"%{tt}%"))
    else:
        query = query.filter(AuditLog.tool_name.in_(mutation_tools) | AuditLog.tool_name.ilike("%update%") | AuditLog.tool_name.ilike("%delete%") | AuditLog.tool_name.ilike("%create%"))

    total = query.count()
    logs = query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()

    history_items = []
    for l in logs:
        args = l.tool_arguments or {}
        payload = l.proposed_payload or {}
        
        extracted_target_id = (
            args.get("property_id") or payload.get("property_id") or
            args.get("email") or payload.get("email") or
            l.pending_action_id or l.request_id or "N/A"
        )

        if target_id and target_id.strip().lower() not in str(extracted_target_id).lower():
            continue

        extracted_target_type = "property"
        if "auth" in l.tool_name or "user" in l.tool_name:
            extracted_target_type = "user"
        elif "batch" in l.tool_name:
            extracted_target_type = "batch_operation"

        changes_summary = {}
        if isinstance(payload.get("fields"), dict):
            changes_summary = payload.get("fields")
        elif isinstance(payload, dict):
            changes_summary = {k: v for k, v in payload.items() if k not in ("action", "email", "property_id")}

        changes_summary, full_payload, result = dedupe_log_sections(changes_summary, payload, l.execution_result, l.status)
        history_items.append({
            "change_id": str(l.id),
            "updated_by": l.staff_user_id or "system",
            "action": l.tool_name,
            "target_type": extracted_target_type,
            "target_id": str(extracted_target_id),
            "changes_summary": changes_summary or {},
            "full_payload": full_payload or {},
            "status": l.status,
            "confirmed_by_staff": l.confirmed_by_staff,
            "execution_result": result,
            "timestamp": l.created_at.isoformat() if l.created_at else None
        })

    # Include Firestore data changes
    try:
        from app.services.firestore_service import get_audit_logs_from_firestore
        fs_logs = get_audit_logs_from_firestore(
            admin_email=updated_by,
            target_type=target_type,
            target_id=target_id,
            limit=limit
        )
        for fl in fs_logs:
            details = fl.get("details") or {}
            fs_status = details.get("status", "SUCCESS") if isinstance(details, dict) else "SUCCESS"
            summary, full, result = dedupe_log_sections(
                details.get("payload", {}) if isinstance(details, dict) else details,
                details if isinstance(details, dict) else {"details": details},
                details,
                fs_status,
            )
            history_items.append({
                "change_id": fl.get("id"),
                "updated_by": fl.get("adminEmail") or fl.get("adminUid") or "admin_system",
                "action": fl.get("action"),
                "target_type": fl.get("targetType") or "unknown",
                "target_id": fl.get("targetId") or "N/A",
                "changes_summary": summary or {},
                "full_payload": full or {},
                "status": fs_status,
                "confirmed_by_staff": details.get("confirmed", True) if isinstance(details, dict) else True,
                "execution_result": result,
                "timestamp": fl.get("createdAt")
            })
    except Exception as fe:
        logger.warning(f"Could not fetch Firestore data history: {fe}")

    # Deduplicate and sort
    seen = set()
    deduped_history = []
    for h in sorted(history_items, key=lambda x: str(x.get("timestamp") or ""), reverse=True):
        cid = h.get("change_id")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        deduped_history.append(h)

    return {
        "total": len(deduped_history),
        "limit": limit,
        "offset": offset,
        "history": deduped_history[:limit]
    }


class AuditUnavailable(Exception):
    """The audit trail cannot be written, so a sensitive change must not go ahead."""


def record_sensitive_audit(db: Session, **kwargs: Any) -> AuditLog:
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
