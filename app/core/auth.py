from typing import Any
from datetime import datetime
from typing import List, Optional, Dict
from fastapi import Depends, HTTPException, Security, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from pydantic import BaseModel, Field

from app.core.deps import get_db
from app.repositories import users_repo
from app.core.config import settings
from app.core.tenant import (
    get_user_domain_key,
    get_current_env_domain,
    get_current_env_domain_key,
    ALL_CANONICAL_WEBSITES,
    normalize_website_domain,
    normalize_website_list
)

security_bearer = HTTPBearer(auto_error=False)
header_staff_email = APIKeyHeader(name="X-Staff-Email", auto_error=False)
header_staff_token = APIKeyHeader(name="X-Staff-Token", auto_error=False)


def get_role_permissions(role: Optional[str]) -> Dict[str, bool]:
    """
    Returns granular RBAC permissions for a given user role.
    Hierarchy:
      - super_admin: Full system authority (manage admins, workers, delete accounts, view directory)
      - admin: Management authority over workers (create/update/delete workers, view directory)
      - worker / employee: Operational authority (admin chat, properties, cannot manage users)
    """
    r = (role or "").strip().lower()
    if r == "super_admin":
        return {
            "can_manage_super_admins": True,
            "can_manage_admins": True,
            "can_manage_workers": True,
            "can_delete_users": True,
            "can_view_directory": True,
            "can_access_admin_chat": True,
            "can_edit_properties": True
        }
    elif r == "admin":
        return {
            "can_manage_super_admins": False,
            "can_manage_admins": False,
            "can_manage_workers": True,
            "can_delete_users": True,  # workers only
            "can_view_directory": True,
            "can_access_admin_chat": True,
            "can_edit_properties": True
        }
    elif r in ["worker", "employee"]:
        return {
            "can_manage_super_admins": False,
            "can_manage_admins": False,
            "can_manage_workers": False,
            "can_delete_users": False,
            "can_view_directory": False,
            "can_access_admin_chat": True,
            "can_edit_properties": False
        }
    return {
        "can_manage_super_admins": False,
        "can_manage_admins": False,
        "can_manage_workers": False,
        "can_delete_users": False,
        "can_view_directory": False,
        "can_access_admin_chat": False,
        "can_edit_properties": False
    }


class AuthenticatedStaff(BaseModel):
    user_id: str
    email: str
    name: Optional[str] = None
    role: str  # strictly resolved from database: 'super_admin', 'admin', 'worker', 'employee', 'public'
    domain: Optional[str] = None
    domain_key: Optional[str] = None
    session_id: Optional[str] = None
    status: str = "active"  # 'active', 'blocked', 'deactivated'
    permissions: Optional[Dict[str, bool]] = None
    allowed_websites: List[str] = Field(default_factory=list)
    selected_websites: List[str] = Field(default_factory=list)

    class Config:
        from_attributes = True


def resolve_user_allowed_websites(user: Optional[object], role: Optional[str]) -> List[str]:
    """
    Resolves the list of canonical website domains a user is authorized to access.
    Super Admins have universal access to all 3 sites by default.
    Other users are restricted to the specific sites assigned by Super Admin.
    """
    r = (role or "").strip().lower()
    if r == "super_admin":
        if user and getattr(user, "allowed_websites", None):
            custom = normalize_website_list(user.allowed_websites)
            if custom:
                return custom
        return list(ALL_CANONICAL_WEBSITES)

    if user:
        if getattr(user, "allowed_websites", None):
            custom = normalize_website_list(user.allowed_websites)
            if custom:
                return custom
        if hasattr(user, "preferences") and isinstance(user.preferences, dict):
            pref_sites = user.preferences.get("allowed_websites") or user.preferences.get("websites")
            if pref_sites:
                custom = normalize_website_list(pref_sites)
                if custom:
                    return custom
        if getattr(user, "domain", None):
            canon = normalize_website_domain(user.domain)
            if canon:
                return [canon]

    return [get_current_env_domain()]


def _insecure_dev_auth_allowed() -> bool:
    """
    "dev-token-<email>" bearer tokens and the bare X-Staff-Email header skip the password check.
    They exist only for local development and are never accepted in production.
    """
    if str(getattr(settings, "ENV", "") or getattr(settings, "ENVIRONMENT", "")).strip().lower() in {"production", "prod"}:
        return False
    return bool(getattr(settings, "ALLOW_INSECURE_DEV_AUTH", False))


def _guard_store(fn):
    """Run a user-store call. A missing store is a 503, never a silent success."""
    try:
        return fn()
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication store is temporarily unavailable. Please try again shortly.",
        ) from e


def _load_session(db, token: str):
    return _guard_store(lambda: users_repo.get_session_by_token(db, token))


def _load_user_by_id(db, user_id):
    return _guard_store(lambda: users_repo.get_by_id(db, user_id))


def get_current_user_from_db(
    db: Any = Depends(get_db),
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Security(security_bearer),
    staff_email_hdr: Optional[str] = Security(header_staff_email),
    staff_token_hdr: Optional[str] = Security(header_staff_token)
) -> AuthenticatedStaff:
    """
    Backend-verified authentication dependency.
    Identity and role are ALWAYS resolved from the user store (users_repo), never from client input.
    Never trusts client-supplied role or staff_id fields.
    """
    resolved_email: Optional[str] = None

    if bearer_creds and bearer_creds.credentials:
        token = bearer_creds.credentials.strip()

        # 1. Check if token exists in the session store (users_repo: Firestore by default)
        existing_session = _load_session(db, token)
        if existing_session:
            user = _load_user_by_id(db, existing_session.user_id)
            user_status = getattr(user, "status", None) or ("blocked" if not getattr(user, "is_active", True) else "active")
            if not user or not getattr(user, "is_active", True) or user_status == "blocked":
                # Instantly terminate session
                if existing_session.revoked_at is None:
                    users_repo.revoke_session(db, existing_session)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User account is blocked. All active sessions have been terminated. Access denied."
                )
            if existing_session.revoked_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Any has been revoked or logged out.",
                    headers={"WWW-Authenticate": "Bearer"}
                )
            if existing_session.expires_at <= datetime.utcnow():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Any has expired. Please log in again.",
                    headers={"WWW-Authenticate": "Bearer"}
                )
            resolved_role = user.role or "worker"
            resolved_domain = getattr(user, "domain", None) or (user.preferences.get("domain") if isinstance(user.preferences, dict) else None) or get_current_env_domain()
            resolved_domain_key = get_user_domain_key(user, resolved_domain)
            allowed_sites = resolve_user_allowed_websites(user, resolved_role)
            return AuthenticatedStaff(
                user_id=str(user.id),
                email=user.email,
                name=user.name or user.email,
                role=resolved_role,
                domain=resolved_domain,
                domain_key=resolved_domain_key,
                session_id=str(existing_session.id),
                status=user_status,
                permissions=get_role_permissions(resolved_role),
                allowed_websites=allowed_sites,
                selected_websites=list(allowed_sites)
            )

        # 2. Support standard dev tokens and service secrets
        if token.startswith("dev-token-"):
            if not _insecure_dev_auth_allowed():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or unrecognized authentication token.",
                    headers={"WWW-Authenticate": "Bearer"}
                )
            resolved_email = token.replace("dev-token-", "").strip().lower()
        elif settings.INTERNAL_API_SECRET and token == settings.INTERNAL_API_SECRET:
            # Service token authenticated: select an active super admin dynamically from the user store
            admin_user = _guard_store(lambda: users_repo.first_active_super_admin(db))
            if not admin_user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="No active administrator account found in database.",
                    headers={"WWW-Authenticate": "Bearer"}
                )
            resolved_email = admin_user.email
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or unrecognized authentication token.",
                headers={"WWW-Authenticate": "Bearer"}
            )

    elif staff_email_hdr and _insecure_dev_auth_allowed():
        resolved_email = staff_email_hdr.strip().lower()
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Authorization Bearer token.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # Verify user and role dynamically from the user store (Firestore admin_authentication by default)
    user = _guard_store(lambda: users_repo.get_by_email(db, resolved_email))

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"User account '{resolved_email}' does not exist in database.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    user_status = getattr(user, "status", None) or ("blocked" if not getattr(user, "is_active", True) else "active")
    if not getattr(user, "is_active", True) or user_status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is blocked. Access denied."
        )

    resolved_role = user.role or "worker"
    resolved_domain = getattr(user, "domain", None) or (user.preferences.get("domain") if isinstance(user.preferences, dict) else None) or get_current_env_domain()
    resolved_domain_key = get_user_domain_key(user, resolved_domain)
    allowed_sites = resolve_user_allowed_websites(user, resolved_role)
    return AuthenticatedStaff(
        user_id=str(user.id),
        email=user.email,
        name=user.name or user.email,
        role=resolved_role,
        domain=resolved_domain,
        domain_key=resolved_domain_key,
        status=user_status,
        permissions=get_role_permissions(resolved_role),
        allowed_websites=allowed_sites,
        selected_websites=list(allowed_sites)
    )


def resolve_selected_websites(request: Request, staff: AuthenticatedStaff) -> List[str]:
    """
    Resolves the active selected websites (e.g. from UI checkboxes) for the current request.
    Extracts from:
      - Header: 'X-Selected-Websites', 'X-Selected-Sites', 'X-Site-Origin', 'X-Domain'
      - Query params: 'selected_websites', 'sites', 'websites', 'site', 'site_origin'
    Validates that EVERY requested website is authorized under the staff account's allowed_websites.
    If unauthorized, raises 403 Forbidden.
    If no selection is specified, defaults to the staff's full allowed_websites.
    """
    raw_selection = (
        request.headers.get("X-Selected-Websites")
        or request.headers.get("X-Selected-Sites")
        or request.headers.get("X-Site-Origin")
        or request.headers.get("X-Domain")
        or request.query_params.get("selected_websites")
        or request.query_params.get("sites")
        or request.query_params.get("websites")
        or request.query_params.get("site")
        or request.query_params.get("site_origin")
    )
    if raw_selection:
        selected = normalize_website_list(raw_selection)
        if selected:
            # Strict RBAC verification against staff.allowed_websites
            unauthorized = [s for s in selected if s not in staff.allowed_websites]
            if unauthorized:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"Access denied: You do not have permissions for website(s): {unauthorized}. "
                        f"Your authorized websites are: {staff.allowed_websites}."
                    )
                )
            return selected

    return list(staff.allowed_websites)


def get_current_staff_with_selected_websites(
    request: Request,
    staff: AuthenticatedStaff = Depends(get_current_user_from_db)
) -> AuthenticatedStaff:
    """
    Dependency that populates staff.selected_websites according to the request headers or query params,
    strictly validated against staff.allowed_websites.
    """
    staff.selected_websites = resolve_selected_websites(request, staff)
    return staff


def require_roles(allowed_roles: List[str]):
    """
    Role-Based Access Control (RBAC) dependency factory.
    Verifies that the database-resolved role belongs to allowed_roles.
    """
    def role_checker(staff: AuthenticatedStaff = Depends(get_current_user_from_db)) -> AuthenticatedStaff:
        # Handle employee alias for worker
        role_check = staff.role
        if role_check == "employee" and "worker" in allowed_roles:
            role_check = "worker"
        elif role_check == "worker" and "employee" in allowed_roles:
            role_check = "employee"

        if staff.role not in allowed_roles and role_check not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: User role '{staff.role}' lacks permission. Required: {allowed_roles}."
            )
        return staff
    return role_checker


def require_roles_with_sites(allowed_roles: List[str]):
    """
    Role-Based Access Control (RBAC) dependency factory with active website selection enforcement.
    Verifies both role and resolves authorized selected_websites.
    """
    def role_and_site_checker(
        request: Request,
        staff: AuthenticatedStaff = Depends(get_current_user_from_db)
    ) -> AuthenticatedStaff:
        role_check = staff.role
        if role_check == "employee" and "worker" in allowed_roles:
            role_check = "worker"
        elif role_check == "worker" and "employee" in allowed_roles:
            role_check = "employee"

        if staff.role not in allowed_roles and role_check not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: User role '{staff.role}' lacks permission. Required: {allowed_roles}."
            )
        staff.selected_websites = resolve_selected_websites(request, staff)
        return staff
    return role_and_site_checker


# Convenient role dependencies for RBAC
VALID_STAFF_ROLES = ["super_admin", "admin", "worker", "employee"]
ADMIN_MANAGEMENT_ROLES = ["super_admin", "admin"]

require_super_admin = require_roles(["super_admin"])
require_admin_or_super_admin = require_roles(["super_admin", "admin"])
require_admin_or_employee = require_roles(["super_admin", "admin", "worker", "employee"])
require_staff_or_worker = require_roles(["super_admin", "admin", "worker", "employee"])

require_super_admin_with_sites = require_roles_with_sites(["super_admin"])
require_admin_or_super_admin_with_sites = require_roles_with_sites(["super_admin", "admin"])
require_admin_or_employee_with_sites = require_roles_with_sites(["super_admin", "admin", "worker", "employee"])
require_staff_or_worker_with_sites = require_roles_with_sites(["super_admin", "admin", "worker", "employee"])


