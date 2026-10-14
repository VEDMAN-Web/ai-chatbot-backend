from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlalchemy.orm import Session
import copy
import logging

from app.db.models import AuditLog

logger = logging.getLogger(__name__)

# Keys that must never be stored in plaintext in audit logs
SENSITIVE_KEYS = {
    "api_key", "token", "secret", "password", "authorization", 
    "anthropic_api_key", "openai_api_key", "client_secret", "credential"
}

def sanitize_payload(obj: Any) -> Any:
    """Recursively scrub sensitive keys and credentials from logged payloads."""
    if isinstance(obj, dict):
        sanitized = {}
        for k, v in obj.items():
            k_low = k.lower()
            # Do not redact LLM usage metrics like 'total_tokens', 'prompt_tokens', etc.
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
    request_id: Optional[str] = None
) -> AuditLog:
    """
    Creates an immutable, append-only audit log entry in the audit_logs table.
    Enforces server-side sanitization of secrets.
    """
    safe_args = sanitize_payload(tool_arguments or {})
    safe_payload = sanitize_payload(proposed_payload or {})

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

    try:
        db.add(log_entry)
        db.commit()
        db.refresh(log_entry)

        # Sync to Firestore admin_audit_log collection
        from app.core.config import settings
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
            records.append({
                "id": str(l.id),
                "admin_email": l.staff_user_id or "system",
                "action": l.tool_name,
                "status": l.status,
                "arguments": l.tool_arguments or {},
                "proposed_payload": l.proposed_payload or {},
                "confirmed_by_staff": l.confirmed_by_staff,
                "execution_result": l.execution_result,
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
                records.append({
                    "id": fl.get("id"),
                    "admin_email": fl.get("adminEmail") or fl.get("adminUid") or "admin_system",
                    "action": fl.get("action"),
                    "status": details.get("status", "SUCCESS") if isinstance(details, dict) else "SUCCESS",
                    "arguments": details.get("arguments", {}) if isinstance(details, dict) else {},
                    "proposed_payload": details.get("payload", {}) if isinstance(details, dict) else {},
                    "confirmed_by_staff": details.get("confirmed", True) if isinstance(details, dict) else True,
                    "execution_result": str(details),
                    "error_message": details.get("error") if isinstance(details, dict) else None,
                    "conversation_id": fl.get("targetId") if fl.get("targetType") == "conversation" else None,
                    "pending_action_id": fl.get("targetId") if fl.get("targetType") == "action" else None,
                    "created_at": fl.get("createdAt"),
                    "source": "firestore"
                })
                total_count += 1
        except Exception as fe:
            logger.warning(f"Could not fetch Firestore logs for activity query: {fe}")

    # Deduplicate by ID and sort descending
    seen_ids = set()
    deduped = []
    for r in sorted(records, key=lambda x: str(x.get("created_at") or ""), reverse=True):
        rid = r.get("id")
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
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

        history_items.append({
            "change_id": str(l.id),
            "updated_by": l.staff_user_id or "system",
            "action": l.tool_name,
            "target_type": extracted_target_type,
            "target_id": str(extracted_target_id),
            "changes_summary": changes_summary,
            "full_payload": payload,
            "status": l.status,
            "confirmed_by_staff": l.confirmed_by_staff,
            "execution_result": l.execution_result,
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
            history_items.append({
                "change_id": fl.get("id"),
                "updated_by": fl.get("adminEmail") or fl.get("adminUid") or "admin_system",
                "action": fl.get("action"),
                "target_type": fl.get("targetType") or "unknown",
                "target_id": fl.get("targetId") or "N/A",
                "changes_summary": details.get("payload", {}) if isinstance(details, dict) else details,
                "full_payload": details if isinstance(details, dict) else {"details": details},
                "status": details.get("status", "SUCCESS") if isinstance(details, dict) else "SUCCESS",
                "confirmed_by_staff": details.get("confirmed", True) if isinstance(details, dict) else True,
                "execution_result": str(details),
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
