"""
Role permission matrix, enforced by the backend.

Roles are read from the database on every request (see core/auth.py), so a role change applies immediately.
Permissions are listed per feature; add the ones a feature needs when it is built.
"""
from __future__ import annotations

from typing import Dict, FrozenSet

from fastapi import Depends, HTTPException, status

from app.core.auth import AuthenticatedStaff, get_current_user_from_db

# --- chat logs, remarks and corrections ---
CHAT_LOGS_VIEW_OWN = "chat_logs.view_own"          # the staff member's own admin-chat history
CHAT_LOGS_VIEW_PUBLIC = "chat_logs.view_public"    # website visitors' conversations on the staff member's websites
CHAT_LOGS_VIEW_ALL = "chat_logs.view_all"          # every conversation within the staff member's websites
CHAT_LOGS_REMARK = "chat_logs.remark"              # add remarks and propose corrected answers
CHAT_LOGS_APPROVE_CORRECTION = "chat_logs.approve_correction"  # approve or reject a proposed correction

# --- property data changes (REST and chat-driven alike) ---
PROPERTIES_WRITE = "properties.write"      # create or update a listing
PROPERTIES_IMPORT = "properties.import"    # Google Drive / link / bulk-file imports (they stage a change for approval)
PROPERTIES_UNDO = "properties.undo"        # roll back a bulk operation

ALL_PERMISSIONS: FrozenSet[str] = frozenset({
    PROPERTIES_WRITE,
    PROPERTIES_IMPORT,
    PROPERTIES_UNDO,
    CHAT_LOGS_VIEW_OWN,
    CHAT_LOGS_VIEW_PUBLIC,
    CHAT_LOGS_VIEW_ALL,
    CHAT_LOGS_REMARK,
    CHAT_LOGS_APPROVE_CORRECTION,
})

ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    "super_admin": ALL_PERMISSIONS,
    "admin": frozenset({
        PROPERTIES_WRITE, PROPERTIES_IMPORT, PROPERTIES_UNDO,
        CHAT_LOGS_VIEW_OWN, CHAT_LOGS_VIEW_PUBLIC, CHAT_LOGS_REMARK,
    }),
    "worker": frozenset({CHAT_LOGS_VIEW_OWN}),
}


# Admin-chat tools that change property data. Every other tool is a read, a report, or an email/calendar draft that
# already waits for the staff member's yes. The same check runs when the tool is proposed and again when it is confirmed.
TOOL_PERMISSIONS: Dict[str, str] = {
    "create_or_update_listing": PROPERTIES_WRITE,
    "propose_listing_update": PROPERTIES_WRITE,
    "ingest_property_from_drive": PROPERTIES_IMPORT,
    "propose_drive_property_ingest": PROPERTIES_IMPORT,
    "ingest_property_from_url": PROPERTIES_IMPORT,
    "execute_batch_property_action": PROPERTIES_IMPORT,
    "undo_batch_property_operation": PROPERTIES_UNDO,
    "archive_listing": PROPERTIES_WRITE,
    "undo_operation": PROPERTIES_UNDO,
}


def normalize_role(role: str | None) -> str:
    cleaned = role.strip().lower() if isinstance(role, str) else ""
    return "worker" if cleaned == "employee" else cleaned


def permissions_for(role: str | None) -> FrozenSet[str]:
    """Unknown roles (including 'public') get nothing."""
    return ROLE_PERMISSIONS.get(normalize_role(role), frozenset())


def has_permission(role: str | None, permission: str) -> bool:
    return permission in permissions_for(role)


def require_permission(*permissions: str):
    """Dependency: the caller needs at least one of the listed permissions."""
    unknown = [p for p in permissions if p not in ALL_PERMISSIONS]
    if unknown:
        raise ValueError(f"Unknown permission(s): {unknown}")

    def checker(staff: AuthenticatedStaff = Depends(get_current_user_from_db)) -> AuthenticatedStaff:
        granted = permissions_for(staff.role)
        if not any(p in granted for p in permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: role '{staff.role}' lacks the required permission.",
            )
        return staff

    return checker


def tool_denial(role: str | None, tool_name: str) -> str | None:
    """The refusal message when this role may not use the chat tool, otherwise None."""
    needed = TOOL_PERMISSIONS.get(tool_name)
    if needed is None or has_permission(role, needed):
        return None
    return f"Your role ({normalize_role(role) or 'unknown'}) is not allowed to change property data. Ask an admin or super admin."
