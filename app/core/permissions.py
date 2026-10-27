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

ALL_PERMISSIONS: FrozenSet[str] = frozenset({
    CHAT_LOGS_VIEW_OWN,
    CHAT_LOGS_VIEW_PUBLIC,
    CHAT_LOGS_VIEW_ALL,
    CHAT_LOGS_REMARK,
    CHAT_LOGS_APPROVE_CORRECTION,
})

ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    "super_admin": ALL_PERMISSIONS,
    "admin": frozenset({CHAT_LOGS_VIEW_OWN, CHAT_LOGS_VIEW_PUBLIC, CHAT_LOGS_REMARK}),
    "worker": frozenset({CHAT_LOGS_VIEW_OWN}),
}


def normalize_role(role: str | None) -> str:
    cleaned = (role or "").strip().lower()
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
