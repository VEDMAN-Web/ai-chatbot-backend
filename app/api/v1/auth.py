import os
import html
import hmac
import hashlib
import json
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Security, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, ConfigDict, model_validator
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.db.session import get_db
from app.db.models import User, UserSession
from app.core.config import settings, is_weak_secret
from app.core.auth import (
    AuthenticatedStaff,
    get_current_user_from_db,
    require_roles,
    security_bearer,
    get_role_permissions,
    require_super_admin,
    require_admin_or_super_admin,
    require_staff_or_worker,
    VALID_STAFF_ROLES,
    resolve_user_allowed_websites
)
from app.core.security import hash_password, verify_password, generate_session_token
from app.services.google_services import SCOPES
from app.core.tenant import (
    get_user_domain_key,
    get_current_env_domain_key,
    get_current_env_domain,
    normalize_tenant_domain_key,
    CANONICAL_DOMAINS,
    ALL_CANONICAL_WEBSITES,
    normalize_website_domain,
    normalize_website_list
)

import logging
from app.services.audit_service import record_audit_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Multi-Tenant Authentication & Google OAuth"])

STATE_SECRET = settings.INTERNAL_API_SECRET or "tajlandia-oauth-state-secret"


def _generate_state_token(user_id: str, email: str) -> str:
    """Generates an HMAC-signed state token containing user identity and expiration timestamp."""
    exp = int(time.time()) + 900  # 15 minutes validity
    payload = f"{user_id}:{email}:{exp}"
    signature = hmac.new(STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{signature}"
    return urllib.parse.quote(raw)


def _verify_state_token(state: str) -> Optional[Dict[str, str]]:
    """Verifies HMAC signature and expiration of state token."""
    try:
        decoded = urllib.parse.unquote(state)
        parts = decoded.split(":")
        if len(parts) != 4:
            return None
        user_id, email, exp_str, sig = parts
        exp = int(exp_str)
        if time.time() > exp:
            return None
        payload = f"{user_id}:{email}:{exp_str}"
        expected_sig = hmac.new(STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return {"user_id": user_id, "email": email}
    except Exception:
        return None


class GoogleStatusResponse(BaseModel):
    connected: bool
    email: Optional[str] = None
    connected_services: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    user_email: str
    message: str


# 1. Initiate Google OAuth Login
@router.get("/google/login")
def google_oauth_login(
    redirect_mode: bool = Query(False, description="If true, 302 redirects directly to Google consent screen"),
    staff: AuthenticatedStaff = Depends(get_current_user_from_db)
):
    """
    Generates the Google OAuth2 authorization URL for the authenticated staff member.
    Allows each user to connect their personal Google Drive, Gmail, and Google Calendar.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google Client ID is not configured in settings."
        )

    state = _generate_state_token(staff.user_id, staff.email)
    scopes_str = " ".join(SCOPES + ["https://www.googleapis.com/auth/userinfo.email", "openid"])

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": scopes_str,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state
    }

    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

    if redirect_mode:
        return RedirectResponse(url=auth_url)

    return {
        "status": "success",
        "user_email": staff.email,
        "auth_url": auth_url,
        "message": "Visit this URL in your browser to authorize your personal Google Workspace account."
    }


# 2. Google OAuth Callback
@router.get("/google/callback")
def google_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Receives authorization code from Google, exchanges it for access & refresh tokens,
    and stores them isolated under the specific staff member's record in PostgreSQL.
    """
    if error:
        return HTMLResponse(
            content=f"""
            <html><body style="font-family:sans-serif; text-align:center; padding:50px;">
                <h2 style="color:#d9534f;">❌ Google Authorization Cancelled or Failed</h2>
                <p>Error reason: <code>{html.escape(error)}</code></p>
                <p><a href="/admin-ui">Return to Tajlandia Admin Panel</a></p>
            </body></html>
            """,
            status_code=400
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing required 'code' or 'state' parameters.")

    verified = _verify_state_token(state)
    if not verified:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state parameter.")

    target_email = verified["email"]
    user = db.query(User).filter(User.email.ilike(target_email)).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User with email '{target_email}' not found.")

    # Exchange authorization code for tokens with Google
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.GOOGLE_REDIRECT_URI
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            token_resp = client.post(token_url, data=data)
            if token_resp.status_code != 200:
                logger.warning("Google token exchange failed with HTTP %s", token_resp.status_code)
                raise HTTPException(status_code=500, detail="Google did not accept the authorization code.")
            token_data = token_resp.json()

            # Retrieve Google Account email profile
            access_token = token_data.get("access_token")
            userinfo_resp = client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            google_email = target_email
            if userinfo_resp.status_code == 200:
                google_email = userinfo_resp.json().get("email", target_email)

    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Google token exchange failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not complete the Google token exchange. Please try again.")

    # Store OAuth tokens in User.preferences["google_workspace"]
    prefs = dict(user.preferences or {})
    expires_in = token_data.get("expires_in", 3600)

    # Preserve existing refresh_token if Google omitted it in re-consent
    existing_gw = prefs.get("google_workspace") or {}
    refresh_token = token_data.get("refresh_token") or existing_gw.get("refresh_token")

    prefs["google_workspace"] = {
        "connected": True,
        "google_email": google_email,
        "access_token": token_data.get("access_token"),
        "refresh_token": refresh_token,
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + expires_in,
        "scopes": token_data.get("scope", "").split(),
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    user.preferences = prefs
    user.google_email = google_email
    if refresh_token:
        user.google_refresh_token = refresh_token
    db.commit()
    db.refresh(user)

    html_success = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Google Workspace Connected</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; text-align: center; padding: 60px 20px; }}
            .card {{ background: #1e293b; max-width: 520px; margin: 0 auto; padding: 40px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); border: 1px solid #334155; }}
            h1 {{ color: #10b981; font-size: 26px; margin-bottom: 12px; }}
            p {{ color: #94a3b8; font-size: 15px; line-height: 1.6; }}
            .pill {{ background: #0f766e; color: #a7f3d0; padding: 4px 10px; border-radius: 20px; font-size: 13px; font-weight: 600; display: inline-block; margin: 4px; }}
            .btn {{ display: inline-block; margin-top: 24px; padding: 12px 24px; background: #2563eb; color: #fff; text-decoration: none; border-radius: 8px; font-weight: bold; }}
            .btn:hover {{ background: #1d4ed8; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>✅ Google Account Connected!</h1>
            <p>Your Google Workspace account <strong>{html.escape(str(google_email))}</strong> is now securely linked to your Tajlandia staff profile (<code>{html.escape(str(user.email))}</code>).</p>
            <div style="margin: 20px 0;">
                <span class="pill">📁 Google Drive Connected</span>
                <span class="pill">✉️ Gmail Sending Active</span>
                <span class="pill">📅 Calendar Scheduling Active</span>
            </div>
            <p>You can now instruct the Admin Chatbot to read files from your Google Drive, draft emails from your account, and schedule appointments on your calendar.</p>
            <a class="btn" href="/admin-ui">Back to Admin Panel</a>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_success, status_code=200)


# 3. Connection Status Endpoint
@router.get("/google/status", response_model=GoogleStatusResponse)
def get_google_status(
    staff: AuthenticatedStaff = Depends(get_current_user_from_db),
    db: Session = Depends(get_db)
):
    """
    Returns the Google Workspace connection status for the authenticated user.
    """
    user = db.query(User).filter(User.email.ilike(staff.email)).first()
    gw = (user.preferences or {}).get("google_workspace") if user else None

    if gw and gw.get("connected") and gw.get("refresh_token"):
        return GoogleStatusResponse(
            connected=True,
            email=gw.get("google_email") or staff.email,
            connected_services=["drive", "gmail", "calendar"],
            scopes=gw.get("scopes", []),
            user_email=staff.email,
            message="Google Workspace is connected and active for this user account."
        )

    return GoogleStatusResponse(
        connected=False,
        email=None,
        connected_services=[],
        scopes=[],
        user_email=staff.email,
        message="No personal Google Workspace account connected. To connect, visit /api/v1/auth/google/login."
    )


# 4. Disconnect Google Account
@router.post("/google/disconnect")
def disconnect_google_oauth(
    staff: AuthenticatedStaff = Depends(get_current_user_from_db),
    db: Session = Depends(get_db)
):
    """
    Revokes and unlinks the connected Google account for the authenticated staff member.
    """
    user = db.query(User).filter(User.email.ilike(staff.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    prefs = dict(user.preferences or {})
    gw = prefs.get("google_workspace")

    # Optional: revoke token with Google
    if gw and gw.get("access_token"):
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": gw["access_token"]},
                    headers={"content-type": "application/x-www-form-urlencoded"}
                )
        except Exception:
            pass

    prefs.pop("google_workspace", None)
    user.preferences = prefs
    user.google_email = None
    user.google_refresh_token = None
    db.commit()

    return {
        "status": "disconnected",
        "user_email": staff.email,
        "message": "Google Workspace credentials have been successfully disconnected from your account."
    }


# =========================================================================
# User Login, Logout & Session Management Endpoints
# =========================================================================

class GoogleAuthInfo(BaseModel):
    connected: bool = False
    google_email: Optional[str] = None
    connected_at: Optional[str] = None
    services: list[str] = Field(default_factory=list)


def _user_by_id(db: Session, user_id: Any) -> Optional[User]:
    """The user with this id, or None. A malformed id is 'not found' instead of a database error (HTTP 500)."""
    try:
        user_uuid = uuid.UUID(str(user_id))
    except (ValueError, TypeError, AttributeError):
        return None
    return db.query(User).filter(User.id == user_uuid).first()


VALID_USER_STATUSES = ("active", "blocked", "deactivated")


def _require_manageable_by(staff: AuthenticatedStaff, user: User) -> None:
    """
    Tenant scope for user management by a non-super admin: every website the target may access must be one
    of the admin's own websites. (The older check compared domains with the server's env domain, and a user's
    domain could be rewritten at login, so it did not isolate tenants.)
    """
    if staff.role == "super_admin":
        return
    target_sites = set(resolve_user_allowed_websites(user, user.role))
    outside = sorted(target_sites - set(staff.allowed_websites or []))
    if outside:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: this account has access to websites outside your scope.",
        )


def _require_other_active_super_admin(db: Session, user: User, action: str) -> None:
    """Refuse to demote / block / deactivate / delete the last active super admin (nobody could manage the system)."""
    if (user.role or "").lower() != "super_admin":
        return
    others = db.query(User).filter(
        User.id != user.id,
        func.lower(User.role) == "super_admin",
        User.is_active.is_(True),
        or_(User.status.is_(None), func.lower(User.status) == "active"),
    ).count()
    if others == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot {action} the last active super admin. Create or activate another super admin first.",
        )


def _get_user_google_info(user: Optional[User]) -> GoogleAuthInfo:
    if not user:
        return GoogleAuthInfo()
    prefs = user.preferences or {}
    gw = prefs.get("google_workspace") or {}
    is_connected = bool((gw.get("connected") and gw.get("refresh_token")) or user.google_refresh_token)
    g_email = user.google_email or gw.get("google_email")
    services = ["drive", "gmail", "calendar"] if is_connected else []
    connected_at = gw.get("connected_at")
    return GoogleAuthInfo(
        connected=is_connected,
        google_email=g_email if is_connected else None,
        connected_at=connected_at if is_connected else None,
        services=services
    )


class UserLoginRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    email: str = Field(..., description="User email address", json_schema_extra={"example": "advisor@varsoviaestate.com"})
    password: str = Field(..., description="Account password", json_schema_extra={"example": "Password123!"})
    domain: Optional[str] = Field(None, description="Optional target website domain: 'varsoviaestate.com', 'thai.estate', or 'tajlandia.com'")
    site_origin: Optional[str] = Field(None, alias="siteOrigin")

    @model_validator(mode="before")
    @classmethod
    def pre_populate_login(cls, data: Any) -> Any:
        # Support legacy `username` field submitted by older clients — map it to email internally
        if isinstance(data, dict):
            if not data.get("email") and data.get("username"):
                data["email"] = data["username"]
            if "siteOrigin" in data and "domain" not in data:
                data["domain"] = data["siteOrigin"]
            elif "site_origin" in data and "domain" not in data:
                data["domain"] = data["site_origin"]
            elif "website" in data and "domain" not in data:
                data["domain"] = data["website"]
        return data


class UserInfoResponse(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    role: str
    domain: Optional[str] = None
    is_active: bool = True
    status: str = "active"
    permissions: Optional[Dict[str, bool]] = None
    google_auth: GoogleAuthInfo = Field(default_factory=GoogleAuthInfo)
    allowed_websites: List[str] = Field(default_factory=list, description="Permitted websites assigned to this staff account")


class UserLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserInfoResponse


class UserLogoutRequest(BaseModel):
    token: Optional[str] = None


class UserLogoutResponse(BaseModel):
    success: bool
    message: str


class AdminUserDirectoryItem(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    role: str
    domain: Optional[str] = None
    is_active: bool
    status: str = "active"
    permissions: Optional[Dict[str, bool]] = None
    created_at: Optional[datetime] = None
    google_auth: GoogleAuthInfo = Field(default_factory=GoogleAuthInfo)
    active_sessions_count: int = 0
    allowed_websites: List[str] = Field(default_factory=list, description="Permitted websites assigned to this staff account")


class BlockUserRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    blocked: bool = Field(True, description="True to block user, False to unblock")
    reason: Optional[str] = Field(None, description="Optional explanation or reason for blocking the account")


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    email: str
    password: str
    name: Optional[str] = None
    role: str = "worker"  # "worker", "admin", "super_admin", or legacy "employee"
    domain: Optional[str] = None  # Optional explicit domain; defaults to active .env domain
    websites: Optional[List[str]] = Field(None, alias="allowed_websites", description="List of authorized websites: ['varsoviaestate.com', 'tajlandia.com', 'thai.estate']")
    is_active: bool = True
    status: Optional[str] = "active"

    @model_validator(mode="before")
    @classmethod
    def pre_populate_create(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("name"):
                name_val = data.get("accountName") or data.get("account_name") or data.get("fullName") or data.get("full_name")
                if name_val:
                    data["name"] = name_val
            if "isActive" in data and "is_active" not in data:
                data["is_active"] = data["isActive"]
            if "allowedWebsites" in data and "websites" not in data:
                data["websites"] = data["allowedWebsites"]
            if "allowed_domains" in data and "websites" not in data:
                data["websites"] = data["allowed_domains"]
            if "sites" in data and "websites" not in data:
                data["websites"] = data["sites"]
        return data


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    domain: Optional[str] = None
    websites: Optional[List[str]] = Field(None, alias="allowed_websites", description="Update authorized websites: ['varsoviaestate.com', 'tajlandia.com', 'thai.estate']")
    is_active: Optional[bool] = None
    status: Optional[str] = Field(None, description="Account status: 'active', 'blocked', 'deactivated'")

    @model_validator(mode="before")
    @classmethod
    def pre_populate_update(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("name"):
                name_val = data.get("accountName") or data.get("account_name") or data.get("fullName") or data.get("full_name")
                if name_val:
                    data["name"] = name_val
            if "isActive" in data and "is_active" not in data:
                data["is_active"] = data["isActive"]
            if "allowedWebsites" in data and "websites" not in data:
                data["websites"] = data["allowedWebsites"]
            if "allowed_domains" in data and "websites" not in data:
                data["websites"] = data["allowed_domains"]
            if "sites" in data and "websites" not in data:
                data["websites"] = data["sites"]
        return data


class RegisterRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    email: str
    password: str
    name: Optional[str] = None
    role: Optional[str] = "worker"
    domain: Optional[str] = None
    websites: Optional[List[str]] = Field(None, alias="allowed_websites")
    admin_secret: Optional[str] = Field(None, alias="adminSecret")

    @model_validator(mode="before")
    @classmethod
    def pre_populate_register(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("name"):
                name_val = data.get("accountName") or data.get("account_name") or data.get("fullName") or data.get("full_name")
                if name_val:
                    data["name"] = name_val
            if not data.get("admin_secret"):
                sec_val = data.get("adminSecret") or data.get("admin_secret") or data.get("secret")
                if sec_val:
                    data["admin_secret"] = sec_val
            if "allowedWebsites" in data and "websites" not in data:
                data["websites"] = data["allowedWebsites"]
            if "allowed_domains" in data and "websites" not in data:
                data["websites"] = data["allowed_domains"]
            if "sites" in data and "websites" not in data:
                data["websites"] = data["sites"]
        return data


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., example="advisor@varsoviaestate.com", description="User email address requesting password reset")


class ForgotPasswordResponse(BaseModel):
    success: bool = True
    message: str
    email: str
    reset_token: Optional[str] = None
    expires_in: int = 1800


class ResetPasswordRequest(BaseModel):
    email: str = Field(..., example="advisor@varsoviaestate.com", description="User email address")
    reset_token: str = Field(..., description="Cryptographic reset token received from forgot-password")
    new_password: str = Field(..., min_length=4, description="New password (minimum 4 characters)")


class ResetPasswordResponse(BaseModel):
    success: bool = True
    message: str


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="Current administrative password")
    new_password: str = Field(..., min_length=4, description="New password (minimum 4 characters)")


class ChangePasswordResponse(BaseModel):
    success: bool = True
    message: str


@router.post("/login", response_model=UserLoginResponse, tags=["Authentication"])
def user_login(
    req: UserLoginRequest,
    request: Request = None,
    db: Session = Depends(get_db)
):
    """
    Authenticates an administrative / staff user with email and password,
    creating an active session token. Restricted strictly to admin roles.
    Supports dynamic domain selection via request body ('domain', 'siteOrigin')
    or HTTP header ('X-Site-Origin', 'X-Website').
    """
    raw_ident = (req.email or getattr(req, "username", None) or "").strip()
    if not raw_ident:
        raise HTTPException(status_code=400, detail="Email is required.")
    email_clean = raw_ident.lower()

    # 1. Look up & authenticate user dynamically in Cloud Firestore `admin_authentication` (Live Firebase Data)
    from app.services.firestore_auth_service import verify_firestore_admin_credentials, sync_firestore_admin_to_local_db
    
    firestore_admin = None
    try:
        firestore_admin = verify_firestore_admin_credentials(email_clean, req.password)
    except Exception as e:
        logger.warning(f"Error checking Firestore admin credentials for '{email_clean}': {e}")

    if firestore_admin:
        # Authenticated successfully via live Firebase data!
        user = sync_firestore_admin_to_local_db(db, firestore_admin, plain_password=req.password)
    else:
        # Fallback to local PostgreSQL database
        user = db.query(User).filter(func.lower(User.email) == email_clean).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
                headers={"WWW-Authenticate": "Bearer"}
            )

        # 2. Verify password against database password_hash. Done before the blocked check so that a wrong
        # password never reveals whether an account exists or is blocked.
        if not user.password_hash or not verify_password(req.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
                headers={"WWW-Authenticate": "Bearer"}
            )

    # Universal check: only an active account may log in, regardless of auth source (Firestore or DB).
    # Any non-active status ("blocked", "deactivated", ...) is refused; previously only "blocked" was.
    user_status = (getattr(user, "status", None) or "active").lower()
    if user_status != "active" or not getattr(user, "is_active", True):
        db.query(UserSession).filter(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None)
        ).update({"revoked_at": datetime.utcnow()}, synchronize_session=False)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is blocked. All active sessions have been terminated. Access denied."
        )

    # 4. Enforce staff/admin role from live account
    allowed_admin_roles = ["super_admin", "admin", "worker", "employee"]
    if user.role not in allowed_admin_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted: login is only available for administrative and employee accounts."
        )

    user_role = user.role or "worker"
    is_privileged_admin = user_role.lower() in ["super_admin", "admin"]
    user_allowed_sites = resolve_user_allowed_websites(user, user_role)

    # 5. Resolve requested domain / website context
    requested_domain_raw = (req.domain or req.site_origin or "").strip()
    if not requested_domain_raw and request is not None:
        requested_domain_raw = (
            request.headers.get("x-site-origin")
            or request.headers.get("x-website")
            or request.headers.get("x-selected-websites", "").split(",")[0]
            or ""
        ).strip()

    if requested_domain_raw:
        canon_req = normalize_website_domain(requested_domain_raw)
        if canon_req:
            # The requested website must be one the account is assigned (super admins: all sites by default).
            # It applies to this login only. It used to be saved as the account's home domain, which let an
            # admin re-home themselves into another tenant and pass the user-management tenant checks.
            if canon_req in user_allowed_sites:
                user_domain = canon_req
            else:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: Account '{user.email}' is not authorized for website '{canon_req}'. Allowed websites: {user_allowed_sites}"
                )
        else:
            user_domain = get_current_env_domain()
    else:
        env_domain = get_current_env_domain()
        # If this server instance is configured with an active .env domain and the user has permission, use it!
        if is_privileged_admin or env_domain in user_allowed_sites:
            user_domain = env_domain
        else:
            user_domain = getattr(user, "domain", None) or env_domain


    # 6. Enforce Multi-Tenant Domain Isolation (.env decided, only if ENFORCE_TENANT_ISOLATION is explicitly configured)
    enforce_isolation = getattr(settings, "ENFORCE_TENANT_ISOLATION", False) or os.getenv("ENFORCE_TENANT_ISOLATION", "").lower() in ["1", "true", "yes"]
    if enforce_isolation:
        active_env_key = get_current_env_domain_key()
        user_dom_key = get_user_domain_key(user)
        
        # Check if user's allowed_websites grants access to this server's active domain
        has_site_permission = any(
            normalize_tenant_domain_key(s) == active_env_key
            for s in user_allowed_sites
        )

        is_cross_tenant = (
            not has_site_permission
            and user_dom_key in ("tajlandia", "varsovia", "thai")
            and user_dom_key != active_env_key
        )

        if is_cross_tenant and not is_privileged_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Account belongs to domain '{user_dom_key}', but this server instance is configured for '{active_env_key}'."
            )

    # Generate session token
    session_token = generate_session_token()
    session_lifetime = timedelta(hours=settings.SESSION_EXPIRE_HOURS)
    expires_at = datetime.utcnow() + session_lifetime

    new_session = UserSession(
        user_id=user.id,
        token=session_token,
        expires_at=expires_at,
        created_at=datetime.utcnow()
    )
    db.add(new_session)
    db.commit()

    return UserLoginResponse(
        access_token=session_token,
        token_type="bearer",
        expires_in=int(session_lifetime.total_seconds()),
        user=UserInfoResponse(
            id=str(user.id),
            email=user.email,
            name=user.name or user.email,
            role=user_role,
            domain=user_domain,
            is_active=getattr(user, "is_active", True),
            status=getattr(user, "status", None) or ("active" if getattr(user, "is_active", True) else "blocked"),
            permissions=get_role_permissions(user_role),
            google_auth=_get_user_google_info(user),
            allowed_websites=user_allowed_sites
        )
    )


@router.post("/logout", response_model=UserLogoutResponse, tags=["Authentication"])
def user_logout(
    body: Optional[UserLogoutRequest] = None,
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Security(security_bearer),
    db: Session = Depends(get_db)
):
    """
    Terminates the active user session and revokes the token.
    Accepts token via Authorization Bearer header or JSON body.
    """
    token_to_revoke = None
    if bearer_creds and bearer_creds.credentials:
        token_to_revoke = bearer_creds.credentials.strip()
    elif body and body.token:
        token_to_revoke = body.token.strip()

    if token_to_revoke:
        session = db.query(UserSession).filter(
            UserSession.token == token_to_revoke,
            UserSession.revoked_at.is_(None)
        ).first()
        if session:
            session.revoked_at = datetime.utcnow()
            db.commit()

    return UserLogoutResponse(
        success=True,
        message="Logged out successfully."
    )


@router.get("/me", response_model=UserInfoResponse, tags=["Authentication"])
def get_current_user_profile(
    staff: AuthenticatedStaff = Depends(get_current_user_from_db),
    db: Session = Depends(get_db)
):
    """
    Returns the authenticated user's profile based on the active session token,
    including linked Google OAuth account details.
    """
    user = db.query(User).filter(User.email.ilike(staff.email)).first()
    g_info = _get_user_google_info(user) if user else GoogleAuthInfo()

    resolved_role = staff.role or "worker"
    resolved_domain = getattr(staff, "domain", None) or getattr(user, "domain", None) or get_current_env_domain()
    user_websites = staff.allowed_websites or resolve_user_allowed_websites(user, resolved_role)
    resolved_status = getattr(user, "status", None) or getattr(staff, "status", "active")
    return UserInfoResponse(
        id=staff.user_id,
        email=staff.email,
        name=staff.name,
        role=resolved_role,
        domain=resolved_domain,
        is_active=getattr(user, "is_active", True) if user else True,
        status=resolved_status,
        permissions=staff.permissions or get_role_permissions(resolved_role),
        google_auth=g_info,
        allowed_websites=user_websites
    )


@router.get("/admin/users", response_model=list[AdminUserDirectoryItem], tags=["Admin Directory"])
def list_admin_users_with_google_status(
    role: Optional[str] = Query(None, description="Filter by role: super_admin, admin, worker"),
    search: Optional[str] = Query(None, description="Search by name or email"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    # Named status_filter so it does not shadow fastapi.status (that turned the 403 below into a 500).
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: active, blocked, deactivated"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Session = Depends(get_db)
):
    """
    Directory endpoint for admins and super admins to inspect staff users.
    Supports filtering by role, search keyword, and active status.
    Workers are denied access (403).
    Admins can view admins and workers; Super Admins view all accounts.
    Strict Domain Isolation:
      Only staff accounts matching the active website domain (.env) are returned.
    """
    active_env_key = get_current_env_domain_key()
    staff_domain_key = staff.domain_key or get_user_domain_key(staff.email, staff.domain)
    # Super admins work across all websites (same rule as create/update/block/delete).
    if staff.role != "super_admin" and staff_domain_key and staff_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your account belongs to a different domain."
        )

    query = db.query(User)

    # Role scope enforcement: regular admin cannot view super_admin accounts
    if staff.role != "super_admin":
        query = query.filter(User.role.in_(["admin", "worker", "employee"]))

    # Query filters
    if role:
        role_clean = role.strip().lower()
        if role_clean in ["worker", "employee"]:
            query = query.filter(User.role.in_(["worker", "employee"]))
        else:
            query = query.filter(func.lower(User.role) == role_clean)

    if search:
        search_term = f"%{search.strip().lower()}%"
        query = query.filter(
            func.lower(User.email).like(search_term) |
            func.lower(User.name).like(search_term)
        )

    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    if status_filter:
        query = query.filter(func.lower(User.status) == status_filter.strip().lower())
    else:
        # Soft-deleted accounts are hidden from the normal directory (ask for status=deleted to list them).
        query = query.filter(or_(User.status.is_(None), func.lower(User.status) != "deleted"))

    all_users = query.order_by(User.created_at.asc()).all()

    # Filter by scope: Super Admins see all; regular Admins only see accounts whose websites are all within theirs.
    if staff.role == "super_admin":
        users = all_users
    else:
        own_sites = set(staff.allowed_websites or [])
        users = [
            u for u in all_users
            if set(resolve_user_allowed_websites(u, u.role)) <= own_sites
        ]

    now = datetime.utcnow()
    results = []
    for u in users:
        active_sess_count = db.query(UserSession).filter(
            UserSession.user_id == u.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now
        ).count()

        resolved_u_role = u.role or "worker"
        resolved_u_domain = u.domain or CANONICAL_DOMAINS.get(active_env_key, get_current_env_domain())
        allowed_sites = resolve_user_allowed_websites(u, resolved_u_role)
        results.append(AdminUserDirectoryItem(
            id=str(u.id),
            email=u.email,
            name=u.name or u.email,
            role=resolved_u_role,
            domain=resolved_u_domain,
            is_active=getattr(u, "is_active", True),
            status=getattr(u, "status", None) or ("active" if getattr(u, "is_active", True) else "blocked"),
            permissions=get_role_permissions(resolved_u_role),
            created_at=u.created_at,
            google_auth=_get_user_google_info(u),
            active_sessions_count=active_sess_count,
            allowed_websites=allowed_sites
        ))
    return results


@router.post("/admin/users", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Admin Directory"])
def create_admin_user(
    req: CreateUserRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Session = Depends(get_db)
):
    """
    Creates a new administrator or worker user account.
    Hierarchy:
      - super_admin can create super_admin, admin, or worker accounts.
      - admin can only create worker accounts.
    Website Access Delegation:
      - super_admin can grant access to: All websites, Multiple websites, or Single specific website.
      - admin can only grant access to websites that they themselves have access to.
    Syncs with both PostgreSQL and Cloud Firestore admin_authentication.
    """
    active_env_key = get_current_env_domain_key()
    staff_domain_key = staff.domain_key or get_user_domain_key(staff.email, staff.domain)
    if staff.role != "super_admin" and staff_domain_key and staff_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your account belongs to a different domain."
        )

    email_clean = req.email.strip().lower()
    if not email_clean or "@" not in email_clean:
        raise HTTPException(status_code=400, detail="Invalid email address format.")

    if len(req.password.strip()) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters long.")

    target_role = req.role.strip().lower()
    if target_role not in ["super_admin", "admin", "worker", "employee"]:
        raise HTTPException(
            status_code=400,
            detail="Role must be one of: 'worker', 'admin', 'super_admin'."
        )

    if target_role == "employee":
        target_role = "worker"

    if staff.role != "super_admin":
        if target_role in ["super_admin", "admin"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins can only create worker accounts. Super admin privileges required to create admins."
            )

    # Domain resolution and isolation check
    if req.domain and req.domain.strip():
        target_domain_key = normalize_tenant_domain_key(req.domain)
        target_domain = CANONICAL_DOMAINS.get(target_domain_key, req.domain.strip())
    else:
        email_key = normalize_tenant_domain_key(email_clean)
        if email_key in ("tajlandia", "varsovia", "thai"):
            target_domain_key = email_key
        else:
            target_domain_key = active_env_key
        target_domain = CANONICAL_DOMAINS.get(target_domain_key, get_current_env_domain())

    if staff.role != "super_admin" and target_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Cannot create user for domain '{target_domain}'. This server instance is restricted to '{get_current_env_domain()}' ({active_env_key})."
        )

    # Website access delegation
    assigned_websites = None
    if req.websites is not None:
        norm_req_websites = normalize_website_list(req.websites)
        if staff.role != "super_admin":
            unauthorized = [s for s in norm_req_websites if s not in staff.allowed_websites]
            if unauthorized:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: You cannot grant access to website(s) outside your permissions: {unauthorized}."
                )
        assigned_websites = norm_req_websites
    elif target_role == "super_admin":
        assigned_websites = list(ALL_CANONICAL_WEBSITES)
    else:
        assigned_websites = normalize_website_list([target_domain])

    existing = db.query(User).filter(func.lower(User.email) == email_clean).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"User with email '{email_clean}' already exists.")

    new_user = User(
        email=email_clean,
        name=req.name.strip() if req.name else email_clean.split("@")[0],
        role=target_role,
        domain=target_domain,
        allowed_websites=assigned_websites,
        password_hash=hash_password(req.password.strip()),
        is_active=req.is_active,
        status=req.status or "active",
        preferences={"allowed_websites": assigned_websites}
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Sync to Cloud Firestore admin_authentication
    try:
        from app.services.firestore_auth_service import add_or_update_admin_in_firestore
        add_or_update_admin_in_firestore(
            email=email_clean,
            password=req.password.strip(),
            role=target_role,
            name=new_user.name or email_clean,
            domain=target_domain,
            is_active=new_user.is_active,
            allowed_websites=assigned_websites
        )
    except Exception as fe:
        logger.warning(f"Error syncing created user '{email_clean}' to Firestore: {fe}")

    # Audit log
    try:
        record_audit_log(
            db=db,
            tool_name="admin:create_user",
            staff_user_id=staff.user_id,
            tool_arguments={"email": email_clean, "role": target_role, "name": new_user.name, "domain": target_domain, "websites": assigned_websites},
            proposed_payload={"action": "create_user", "email": email_clean, "role": target_role, "domain": target_domain, "websites": assigned_websites},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Created {target_role} {email_clean} on websites {assigned_websites}"
        )
    except Exception as ae:
        logger.warning(f"Audit log failed for user creation: {ae}")

    return AdminUserDirectoryItem(
        id=str(new_user.id),
        email=new_user.email,
        name=new_user.name,
        role=new_user.role,
        domain=new_user.domain,
        is_active=new_user.is_active,
        status=new_user.status or "active",
        permissions=get_role_permissions(new_user.role),
        created_at=new_user.created_at,
        google_auth=_get_user_google_info(new_user),
        active_sessions_count=0,
        allowed_websites=assigned_websites
    )


@router.get("/admin/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"])
def get_admin_user_details(
    user_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Session = Depends(get_db)
):
    """
    Retrieves details and status for a specific user account.
    Enforces strict domain isolation (cannot access user of other website).
    """
    user = _user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    active_env_key = get_current_env_domain_key()
    staff_domain_key = staff.domain_key or get_user_domain_key(staff.email, staff.domain)
    # Super admins work across all websites (same rule as create/update/block/delete).
    if staff.role != "super_admin" and staff_domain_key and staff_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your account belongs to a different domain."
        )

    target_user_domain_key = get_user_domain_key(user)
    if staff.role != "super_admin" and target_user_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found on this website domain."
        )

    if staff.role != "super_admin" and user.role == "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins cannot view super admin accounts.")
    if (user.status or "").lower() == "deleted" and staff.role != "super_admin":
        raise HTTPException(status_code=404, detail="User not found.")
    _require_manageable_by(staff, user)

    now = datetime.utcnow()
    active_sess_count = db.query(UserSession).filter(
        UserSession.user_id == user.id,
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > now
    ).count()

    resolved_u_role = user.role or "worker"
    resolved_u_domain = user.domain or CANONICAL_DOMAINS.get(active_env_key, get_current_env_domain())
    return AdminUserDirectoryItem(
        id=str(user.id),
        email=user.email,
        name=user.name or user.email,
        role=resolved_u_role,
        domain=resolved_u_domain,
        is_active=getattr(user, "is_active", True),
        status=getattr(user, "status", None) or ("active" if getattr(user, "is_active", True) else "blocked"),
        permissions=get_role_permissions(resolved_u_role),
        created_at=user.created_at,
        google_auth=_get_user_google_info(user),
        active_sessions_count=active_sess_count,
        allowed_websites=resolve_user_allowed_websites(user, resolved_u_role)
    )


@router.patch("/admin/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"])
@router.put("/admin/users/{user_id}", response_model=AdminUserDirectoryItem, tags=["Admin Directory"])
def update_admin_user(
    user_id: str,
    req: UpdateUserRequest,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Session = Depends(get_db)
):
    """
    Updates user details (name, password reset, role, active status, domain, allowed_websites).
    Hierarchy:
      - super_admin can update any user account, assign any websites, and promote/demote roles.
      - admin can only update worker accounts (cannot edit admins or super_admins, cannot promote to admin).
    Strict Domain & Website Isolation:
      - Super admin can assign any canonical websites (all, multiple, single).
      - Admin can only grant websites within their own allowed_websites.
    Syncs with PostgreSQL and Cloud Firestore.
    """
    user = _user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    active_env_key = get_current_env_domain_key()
    staff_domain_key = staff.domain_key or get_user_domain_key(staff.email, staff.domain)
    if staff.role != "super_admin" and staff_domain_key and staff_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your account belongs to a different domain."
        )

    # Domain isolation enforcement
    target_user_domain_key = get_user_domain_key(user)
    if staff.role != "super_admin" and target_user_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Cannot modify users from another website domain ({target_user_domain_key})."
        )

    target_user_role = (user.role or "worker").lower()
    _require_manageable_by(staff, user)

    if req.status is not None and req.status.strip().lower() not in VALID_USER_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(VALID_USER_STATUSES)}.")
    demoting = req.role is not None and req.role.strip().lower() != "super_admin"
    disabling = (req.status is not None and req.status.strip().lower() != "active") or req.is_active is False
    if demoting or disabling:
        _require_other_active_super_admin(db, user, "demote or disable")

    # Role hierarchy enforcement
    if staff.role != "super_admin":
        if target_user_role == "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins cannot modify super admin accounts."
            )
        if target_user_role == "admin" and str(staff.user_id) != str(user.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins cannot modify other admin accounts."
            )
        if req.role is not None:
            new_role = req.role.strip().lower()
            if new_role in ["admin", "super_admin"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only super administrators can promote accounts to admin or super_admin."
                )

    new_plain_password = None
    if req.name is not None:
        user.name = req.name.strip()
    if req.password is not None and req.password.strip():
        new_plain_password = req.password.strip()
        if len(new_plain_password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters long.")
        user.password_hash = hash_password(new_plain_password)
        # Revoke existing sessions so user logs in with new password
        db.query(UserSession).filter(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None)
        ).update({"revoked_at": datetime.utcnow()})
    if req.role is not None:
        target_role = req.role.strip().lower()
        if target_role not in ["worker", "admin", "super_admin", "employee"]:
            raise HTTPException(status_code=400, detail="Role must be 'worker', 'admin', or 'super_admin'.")
        if target_role == "employee":
            target_role = "worker"
        user.role = target_role
    if req.domain is not None and req.domain.strip():
        new_dom_key = normalize_tenant_domain_key(req.domain)
        if new_dom_key != active_env_key and staff.role != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins cannot transfer user accounts across different website domains."
            )
        user.domain = CANONICAL_DOMAINS.get(new_dom_key, req.domain.strip())
    if req.websites is not None:
        clean_assigned_websites = normalize_website_list(req.websites)
        if staff.role != "super_admin":
            unauthorized_sites = [w for w in clean_assigned_websites if w not in (staff.allowed_websites or [])]
            if unauthorized_sites:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot grant access to websites outside your authorized scope: {unauthorized_sites}"
                )
        user.allowed_websites = clean_assigned_websites
        prefs = dict(user.preferences or {})
        prefs["allowed_websites"] = clean_assigned_websites
        user.preferences = prefs
    if req.status is not None:
        target_status = req.status.strip().lower()
        user.status = target_status
        if target_status in ("blocked", "deactivated"):
            user.is_active = False
            db.query(UserSession).filter(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None)
            ).update({"revoked_at": datetime.utcnow()}, synchronize_session=False)
        elif target_status == "active":
            user.is_active = True
    if req.is_active is not None:
        user.is_active = req.is_active
        if not req.is_active:
            db.query(UserSession).filter(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None)
            ).update({"revoked_at": datetime.utcnow()}, synchronize_session=False)

    db.commit()
    db.refresh(user)

    # Sync to Cloud Firestore
    try:
        from app.services.firestore_auth_service import update_admin_in_firestore
        update_admin_in_firestore(
            email=user.email,
            name=user.name,
            role=user.role,
            domain=user.domain,
            is_active=user.is_active,
            password=new_plain_password,
            allowed_websites=user.allowed_websites,
            status=user.status
        )
    except Exception as fe:
        logger.warning(f"Error syncing updated user '{user.email}' to Firestore: {fe}")

    # Audit log
    try:
        record_audit_log(
            db=db,
            tool_name="admin:update_user",
            staff_user_id=staff.user_id,
            tool_arguments={"target_user_id": user_id, "email": user.email, "role": user.role, "domain": user.domain, "allowed_websites": user.allowed_websites, "status": user.status},
            proposed_payload={"action": "update_user", "target_email": user.email, "allowed_websites": user.allowed_websites, "status": user.status},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Updated user {user.email}"
        )
    except Exception as ae:
        logger.warning(f"Audit log failed for user update: {ae}")

    now = datetime.utcnow()
    active_sess_count = db.query(UserSession).filter(
        UserSession.user_id == user.id,
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > now
    ).count()

    resolved_u_role = user.role or "worker"
    resolved_u_domain = user.domain or CANONICAL_DOMAINS.get(active_env_key, get_current_env_domain())
    return AdminUserDirectoryItem(
        id=str(user.id),
        email=user.email,
        name=user.name or user.email,
        role=resolved_u_role,
        domain=resolved_u_domain,
        is_active=getattr(user, "is_active", True),
        status=getattr(user, "status", None) or ("active" if getattr(user, "is_active", True) else "blocked"),
        permissions=get_role_permissions(resolved_u_role),
        created_at=user.created_at,
        google_auth=_get_user_google_info(user),
        active_sessions_count=active_sess_count,
        allowed_websites=resolve_user_allowed_websites(user, resolved_u_role)
    )


@router.delete("/admin/users/{user_id}", tags=["Admin Directory"])
def delete_admin_user(
    user_id: str,
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Session = Depends(get_db)
):
    """
    Deletes a user account and cascades active sessions.
    Hierarchy:
      - super_admin can delete admin and worker accounts within this domain.
      - admin can delete worker accounts only within this domain.
      - No user can delete their own currently authenticated account.
      - Cross-domain deletion is strictly blocked (403).
    Removes account from PostgreSQL and Cloud Firestore admin_authentication.
    """
    user = _user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if str(staff.user_id) == str(user_id) or staff.email.lower() == user.email.lower():
        raise HTTPException(status_code=400, detail="You cannot delete your own account while logged in.")

    active_env_key = get_current_env_domain_key()
    staff_domain_key = staff.domain_key or get_user_domain_key(staff.email, staff.domain)
    if staff.role != "super_admin" and staff_domain_key and staff_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your account belongs to a different domain."
        )

    # Domain isolation enforcement
    target_user_domain_key = get_user_domain_key(user)
    if staff.role != "super_admin" and target_user_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Cannot delete users from another website domain ({target_user_domain_key})."
        )

    target_user_role = (user.role or "worker").lower()

    if staff.role != "super_admin":
        if target_user_role == "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins cannot delete super admin accounts."
            )
        if target_user_role == "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins cannot delete other admin accounts. Only super administrators can delete admins."
            )

    _require_manageable_by(staff, user)
    _require_other_active_super_admin(db, user, "delete")
    if (user.status or "").lower() == "deleted":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This account is already deleted.")

    deleted_email = user.email

    # Soft delete: the account row stays (audit trails keep pointing at a real user) but can never log in again.
    now = datetime.utcnow()
    db.query(UserSession).filter(
        UserSession.user_id == user.id,
        UserSession.revoked_at.is_(None),
    ).update({"revoked_at": now}, synchronize_session=False)
    user.status = "deleted"
    user.is_active = False
    prefs = dict(user.preferences or {})
    prefs["deleted_at"] = now.isoformat()
    prefs["deleted_by"] = staff.email
    user.preferences = prefs
    db.commit()

    # Deactivate (not delete) the Firestore admin_authentication login so it cannot authenticate either.
    try:
        from app.services.firestore_auth_service import update_admin_in_firestore
        update_admin_in_firestore(email=deleted_email, is_active=False, status="deleted")
    except Exception as fe:
        logger.warning(f"Error deactivating user '{deleted_email}' in Firestore: {fe}")

    # Audit log
    try:
        record_audit_log(
            db=db,
            tool_name="admin:delete_user",
            staff_user_id=staff.user_id,
            tool_arguments={"deleted_user_id": user_id, "deleted_email": deleted_email},
            proposed_payload={"action": "delete_user", "deleted_email": deleted_email},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Deleted user {deleted_email}"
        )
    except Exception as ae:
        logger.warning(f"Audit log failed for user deletion: {ae}")

    return {"success": True, "message": f"User '{deleted_email}' has been successfully deleted."}


@router.post("/admin/users/{user_id}/block", response_model=AdminUserDirectoryItem, tags=["Admin Directory"])
def block_admin_user(
    user_id: str,
    req: BlockUserRequest,
    staff: AuthenticatedStaff = Depends(require_super_admin),
    db: Session = Depends(get_db)
):
    """
    Super Admin API to block or unblock any administrator or worker user.
    When a user is blocked:
      - All active sessions are immediately revoked in PostgreSQL.
      - Account status is set to 'blocked' and is_active to False.
      - Status is synced to Cloud Firestore admin_authentication collection (isBlocked=True).
      - Blocked users cannot log in (403 Forbidden) and cannot call any admin APIs (chat, CRUD, etc.).
    A Super Admin cannot block their own account.
    """
    user = _user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if str(staff.user_id) == str(user_id) or staff.email.lower() == user.email.lower():
        raise HTTPException(
            status_code=400,
            detail="Super administrators cannot block their own account."
        )

    is_blocking = req.blocked
    if (user.status or "").lower() == "deleted":
        raise HTTPException(status_code=404, detail="User not found.")
    if is_blocking:
        _require_other_active_super_admin(db, user, "block")
        user.status = "blocked"
        user.is_active = False
        # Revoke all active sessions immediately
        now = datetime.utcnow()
        revoked_count = db.query(UserSession).filter(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None)
        ).update({"revoked_at": now}, synchronize_session=False)
        db.commit()
        db.refresh(user)
        logger.info(f"Blocked user {user.email} (id={user.id}). Revoked {revoked_count} active sessions.")
    else:
        user.status = "active"
        user.is_active = True
        db.commit()
        db.refresh(user)
        logger.info(f"Unblocked user {user.email} (id={user.id}).")

    # Sync to Cloud Firestore admin_authentication
    try:
        from app.services.firestore_auth_service import update_admin_in_firestore
        update_admin_in_firestore(
            email=user.email,
            is_active=user.is_active,
            status=user.status
        )
    except Exception as fe:
        logger.warning(f"Error syncing block status for user '{user.email}' to Firestore: {fe}")

    # Record Audit Log
    try:
        record_audit_log(
            db=db,
            tool_name="admin:block_user",
            staff_user_id=staff.user_id,
            tool_arguments={"target_user_id": user_id, "email": user.email, "blocked": is_blocking, "reason": req.reason},
            proposed_payload={"action": "block_user" if is_blocking else "unblock_user", "target_email": user.email},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"{'Blocked' if is_blocking else 'Unblocked'} user {user.email}. Revoked sessions."
        )
    except Exception as ae:
        logger.warning(f"Audit log failed for user block: {ae}")

    resolved_u_role = user.role or "worker"
    resolved_u_domain = user.domain or CANONICAL_DOMAINS.get(get_current_env_domain_key(), get_current_env_domain())
    return AdminUserDirectoryItem(
        id=str(user.id),
        email=user.email,
        name=user.name or user.email,
        role=resolved_u_role,
        domain=resolved_u_domain,
        is_active=getattr(user, "is_active", True),
        status=user.status or ("active" if user.is_active else "blocked"),
        permissions=get_role_permissions(resolved_u_role),
        created_at=user.created_at,
        google_auth=_get_user_google_info(user),
        active_sessions_count=0 if is_blocking else db.query(UserSession).filter(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.utcnow()
        ).count(),
        allowed_websites=resolve_user_allowed_websites(user, resolved_u_role)
    )



def _admin_secret_matches(supplied: Optional[str]) -> bool:
    """Constant-time check against the configured admin secrets. A default or weak internal secret never counts."""
    if not supplied:
        return False
    candidates = []
    if not is_weak_secret(settings.INTERNAL_API_SECRET):
        candidates.append(settings.INTERNAL_API_SECRET)
    if settings.ADMIN_PASSWORD and not is_weak_secret(settings.ADMIN_PASSWORD):
        candidates.append(settings.ADMIN_PASSWORD)
    supplied_bytes = supplied.encode("utf-8")
    return any(hmac.compare_digest(supplied_bytes, c.encode("utf-8")) for c in candidates)


@router.post("/register", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Authentication"])
def register_user(
    req: RegisterRequest,
    db: Session = Depends(get_db),
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Security(security_bearer)
):
    """
    Multi-user registration endpoint.
    Permitted if:
    - Called by an authenticated staff member (Bearer token), OR
    - Provided 'admin_secret' matches settings.INTERNAL_API_SECRET or settings.ADMIN_PASSWORD, OR
    - No users currently exist in the database (initial onboarding).
    """
    # Access rule: a super admin session, the configured admin secret, or first-run onboarding.
    if bearer_creds and bearer_creds.credentials:
        caller = get_current_user_from_db(db=db, bearer_creds=bearer_creds)
        if caller.role != "super_admin":
            raise HTTPException(status_code=403, detail="Only a super admin can register staff accounts.")
        actor = caller.email
    elif _admin_secret_matches(req.admin_secret):
        actor = "admin_secret"
    elif db.query(User).count() == 0:
        actor = "bootstrap"
    else:
        raise HTTPException(
            status_code=403,
            detail="Registration requires a super admin session or a valid admin secret.",
        )

    email_clean = req.email.strip().lower()
    if not email_clean or "@" not in email_clean:
        raise HTTPException(status_code=400, detail="Invalid email address format.")

    plain_pass = req.password.strip()
    if len(plain_pass) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters long.")

    # Determine requested / target role. An unknown role is rejected; it never falls back to super_admin.
    requested_role = (req.role or "worker").strip().lower().replace(" ", "_").replace("-", "_")
    if requested_role not in ["super_admin", "admin", "worker", "employee"]:
        raise HTTPException(status_code=400, detail="Role must be one of: 'worker', 'admin', 'super_admin'.")
    target_role = requested_role

    active_env_key = get_current_env_domain_key()
    if getattr(req, "domain", None) and req.domain.strip():
        reg_domain_key = normalize_tenant_domain_key(req.domain)
        resolved_domain = CANONICAL_DOMAINS.get(reg_domain_key, req.domain.strip())
    else:
        email_key = normalize_tenant_domain_key(email_clean)
        resolved_domain = CANONICAL_DOMAINS.get(email_key, get_current_env_domain())

    # Determine assigned websites (default to all canonical for super_admin, or resolved_domain for worker/admin)
    if req.websites is not None:
        assigned_websites = normalize_website_list(req.websites)
    elif target_role == "super_admin":
        assigned_websites = list(ALL_CANONICAL_WEBSITES)
    else:
        assigned_websites = [normalize_website_domain(resolved_domain)]

    existing = db.query(User).filter(func.lower(User.email) == email_clean).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"User with email '{email_clean}' already exists.")

    new_user = User(
        email=email_clean,
        name=req.name.strip() if req.name else email_clean.split("@")[0],
        role=target_role,
        domain=resolved_domain,
        password_hash=hash_password(plain_pass),
        is_active=True,
        allowed_websites=assigned_websites,
        preferences={"allowed_websites": assigned_websites}
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Sync to Cloud Firestore admin_authentication collection
    try:
        from app.services.firestore_auth_service import add_or_update_admin_in_firestore
        add_or_update_admin_in_firestore(
            email=email_clean,
            password=req.password.strip(),
            role=target_role,
            name=new_user.name or email_clean,
            domain=resolved_domain,
            is_active=True,
            allowed_websites=assigned_websites
        )
    except Exception as e:
        logger.warning(f"Error syncing new admin '{email_clean}' to Firestore: {e}")

    # Record audit log
    try:
        record_audit_log(
            db=db,
            tool_name="auth:register",
            staff_user_id=actor,
            tool_arguments={"email": email_clean, "role": target_role, "name": new_user.name, "domain": resolved_domain, "websites": assigned_websites},
            proposed_payload={"action": "register_admin", "email": email_clean, "role": target_role, "domain": resolved_domain, "websites": assigned_websites},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Registered admin {email_clean} with role {target_role} on websites {assigned_websites}"
        )
    except Exception as ae:
        logger.warning(f"Error auditing register: {ae}")

    return AdminUserDirectoryItem(
        id=str(new_user.id),
        email=new_user.email,
        name=new_user.name,
        role=new_user.role,
        domain=new_user.domain,
        is_active=new_user.is_active,
        created_at=new_user.created_at,
        google_auth=_get_user_google_info(new_user),
        active_sessions_count=0,
        allowed_websites=resolve_user_allowed_websites(new_user, new_user.role)
    )


@router.post("/forgot-password", response_model=ForgotPasswordResponse, tags=["Authentication"])
def forgot_password_endpoint(
    req: ForgotPasswordRequest,
    db: Session = Depends(get_db)
):
    """
    Starts a password reset. The reset token is emailed to the account owner and is NEVER returned in the
    response (returning it let anyone reset any account, including a super admin's, without logging in).
    The response is the same whether or not the account exists, so it cannot be used to discover accounts.
    The token is bound to the current password, so it works once and dies when the password changes.
    """
    from app.services.firestore_auth_service import get_admin_from_firestore, generate_password_reset_token

    email_clean = req.email.strip().lower()
    if not email_clean or "@" not in email_clean:
        raise HTTPException(status_code=400, detail="Invalid email format.")

    expires_in_secs = 1800  # 30 minutes
    generic = ForgotPasswordResponse(
        success=True,
        message="If an account exists for this email, a password reset link has been sent to it.",
        email=email_clean,
        reset_token=None,
        expires_in=expires_in_secs,
    )

    fs_admin = get_admin_from_firestore(email_clean)
    db_user = db.query(User).filter(func.lower(User.email) == email_clean).first()
    if not fs_admin and not db_user:
        return generic

    current_hash = _current_password_hash(email_clean, db_user, fs_admin)
    reset_token = generate_password_reset_token(
        email_clean, expires_in_seconds=expires_in_secs, current_password_hash=current_hash
    )
    sent = False
    try:
        from app.services.google_services import google_manager
        base = (getattr(settings, "BASE_WEBSITE_URL", "") or "").rstrip("/")
        body = (
            "A password reset was requested for your staff account.\n\n"
            "If this was you, use this reset token within 30 minutes (it works once):\n\n"
            f"{reset_token}\n\n"
            f"{base + '/admin?reset_token=' + reset_token if base else ''}\n\n"
            "If you did not request this, ignore this email; your password is unchanged."
        )
        result = google_manager.send_email(to=email_clean, subject="Password reset", body=body)
        sent = bool(result and result.get("success") and not result.get("is_mock"))
    except Exception as e:
        logger.warning("Password reset email could not be sent: %s", type(e).__name__)

    try:
        record_audit_log(
            db=db,
            tool_name="auth:forgot_password",
            staff_user_id=email_clean,
            tool_arguments={"email": email_clean},
            proposed_payload={"action": "forgot_password_request", "email": email_clean},
            confirmed_by_staff=True,
            status="SUCCESS" if sent else "FAILED",
            execution_result=("Password reset email sent" if sent else "Password reset email could not be delivered")
        )
    except Exception as e:
        logger.warning(f"Audit log failed for forgot-password: {e}")

    return generic


def _current_password_hash(email_clean: str, db_user: Optional[User], fs_admin: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The hash reset tokens are bound to: Postgres first (what login checks), else Firestore."""
    if db_user is not None and getattr(db_user, "password_hash", None):
        return db_user.password_hash
    if fs_admin is None:
        from app.services.firestore_auth_service import get_admin_from_firestore
        fs_admin = get_admin_from_firestore(email_clean)
    if fs_admin:
        return fs_admin.get("passwordHash") or fs_admin.get("password_hash")
    return None


@router.post("/reset-password", response_model=ResetPasswordResponse, tags=["Authentication"])
def reset_password_endpoint(
    req: ResetPasswordRequest,
    db: Session = Depends(get_db)
):
    """
    Resets the administrator password using a valid cryptographic reset token.
    Updates password in Cloud Firestore `admin_authentication` and PostgreSQL.
    Revokes any active user sessions to force a fresh login.
    """
    from app.services.firestore_auth_service import verify_password_reset_token, update_admin_password_in_firestore

    email_clean = req.email.strip().lower()
    new_pw = req.new_password.strip()

    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters long.")

    db_user = db.query(User).filter(func.lower(User.email) == email_clean).first()
    verified_email = verify_password_reset_token(
        req.reset_token,
        expected_email=email_clean,
        current_password_hash=_current_password_hash(email_clean, db_user),
    )
    if not verified_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid, expired or already used password reset token."
        )

    # 1. Update in Cloud Firestore admin_authentication
    fs_updated = update_admin_password_in_firestore(email_clean, new_pw)

    # 2. Update in PostgreSQL
    new_hash = hash_password(new_pw)
    if db_user:
        db_user.password_hash = new_hash
        # Revoke all active sessions
        now = datetime.utcnow()
        db.query(UserSession).filter(
            UserSession.user_id == db_user.id,
            UserSession.revoked_at.is_(None)
        ).update({"revoked_at": now})
        db.commit()
    elif not fs_updated:
        raise HTTPException(status_code=404, detail="User account not found.")

    try:
        record_audit_log(
            db=db,
            tool_name="auth:reset_password",
            staff_user_id=email_clean,
            tool_arguments={"email": email_clean},
            proposed_payload={"action": "reset_password", "email": email_clean},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Password successfully reset for {email_clean}"
        )
    except Exception as e:
        logger.warning(f"Audit log failed for reset-password: {e}")

    return ResetPasswordResponse(
        success=True,
        message="Password has been successfully reset. Please log in with your new credentials."
    )


@router.post("/change-password", response_model=ChangePasswordResponse, tags=["Authentication"])
def change_password_endpoint(
    req: ChangePasswordRequest,
    staff: AuthenticatedStaff = Depends(get_current_user_from_db),
    db: Session = Depends(get_db)
):
    """
    Changes the authenticated administrator's password.
    Requires current password verification.
    Updates password in Cloud Firestore `admin_authentication` and PostgreSQL.
    """
    from app.services.firestore_auth_service import verify_firestore_admin_credentials, update_admin_password_in_firestore

    old_pw = req.old_password.strip()
    new_pw = req.new_password.strip()

    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters long.")

    if old_pw == new_pw:
        raise HTTPException(status_code=400, detail="New password cannot be the same as the current password.")

    user = db.query(User).filter(User.email.ilike(staff.email)).first()
    verified = False

    try:
        fs_admin = verify_firestore_admin_credentials(staff.email, old_pw)
        if fs_admin:
            verified = True
    except Exception as e:
        logger.warning(f"Error checking Firestore credentials: {e}")

    if not verified and user and user.password_hash and verify_password(old_pw, user.password_hash):
        verified = True

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect."
        )

    # 1. Update in Cloud Firestore admin_authentication
    update_admin_password_in_firestore(staff.email, new_pw)

    # 2. Update in PostgreSQL, and sign out every other session (a stolen token must not outlive a password change)
    if user:
        user.password_hash = hash_password(new_pw)
        others = db.query(UserSession).filter(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
        )
        if staff.session_id:
            try:
                others = others.filter(UserSession.id != uuid.UUID(str(staff.session_id)))
            except ValueError:
                pass
        others.update({"revoked_at": datetime.utcnow()}, synchronize_session=False)
        db.commit()

    try:
        record_audit_log(
            db=db,
            tool_name="auth:change_password",
            staff_user_id=staff.email,
            tool_arguments={"email": staff.email},
            proposed_payload={"action": "change_password", "email": staff.email},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Password successfully changed for {staff.email}"
        )
    except Exception as e:
        logger.warning(f"Audit log failed for change-password: {e}")

    return ChangePasswordResponse(
        success=True,
        message="Password has been successfully changed."
    )




