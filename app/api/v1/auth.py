import os
import html
import hmac
import hashlib
import json
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Security, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, ConfigDict, model_validator

from app.core.deps import get_db
from app.repositories import users_repo
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


def _store_call(fn):
    """Run a user-store call. An unavailable store is a 503, never a silent success."""
    try:
        return fn()
    except HTTPException:
        raise
    except RuntimeError as exc:
        logger.error("User store unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication store is temporarily unavailable. Please try again shortly.",
        ) from exc


_WEAK_SECRETS = {
    "", "changeme", "change-me", "secret", "password", "admin", "tajlandia-internal-secret-token",
    "tajlandia-oauth-state-secret", "tajlandia-reset-secret", "your-secret-here",
}


def _is_weak_secret(value: Optional[str]) -> bool:
    text = (value or "").strip()
    return len(text) < 12 or text.lower() in _WEAK_SECRETS


def _admin_secret_matches(supplied: Optional[str]) -> bool:
    """Constant-time check against the configured admin secrets. A default or weak internal secret never counts."""
    if not supplied:
        return False
    candidates = []
    if not _is_weak_secret(settings.INTERNAL_API_SECRET):
        candidates.append(settings.INTERNAL_API_SECRET)
    if settings.ADMIN_PASSWORD and not _is_weak_secret(settings.ADMIN_PASSWORD):
        candidates.append(settings.ADMIN_PASSWORD)
    supplied_bytes = supplied.encode("utf-8")
    return any(hmac.compare_digest(supplied_bytes, c.encode("utf-8")) for c in candidates)




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
    db: Any = Depends(get_db)
):
    """
    Receives authorization code from Google, exchanges it for access & refresh tokens,
    and stores them isolated under the specific staff member's record in Firestore.
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
    user = _store_call(lambda: users_repo.get_by_email(db, target_email))
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
    _store_call(lambda: users_repo.save_user(db, user))

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
    db: Any = Depends(get_db)
):
    """
    Returns the Google Workspace connection status for the authenticated user.
    """
    user = _store_call(lambda: users_repo.get_by_email(db, staff.email))
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
    db: Any = Depends(get_db)
):
    """
    Revokes and unlinks the connected Google account for the authenticated staff member.
    """
    user = _store_call(lambda: users_repo.get_by_email(db, staff.email))
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
    _store_call(lambda: users_repo.save_user(db, user))

    return {
        "status": "disconnected",
        "user_email": staff.email,
        "message": "Google Workspace credentials have been successfully disconnected from your account."
    }


# =========================================================================
# User Login, Logout & Any Management Endpoints
# =========================================================================

class GoogleAuthInfo(BaseModel):
    connected: bool = False
    google_email: Optional[str] = None
    connected_at: Optional[str] = None
    services: list[str] = Field(default_factory=list)


def _get_user_google_info(user: Optional[Any]) -> GoogleAuthInfo:
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


_RESET_GENERIC_MESSAGE = "If an account exists for this email, a reset link has been sent."
_RESET_GENERIC_ERROR = "Invalid or expired password reset token."


class ForgotPasswordResponse(BaseModel):
    success: bool = True
    message: str
    reset_token: Optional[str] = None


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
    request: Request,
    db: Any = Depends(get_db)
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
    from app.core.rate_limit import enforce_rate_limit
    enforce_rate_limit(
        request,
        scope="login",
        identities=[email_clean],
        limit=int(settings.RATE_LIMIT_LOGIN_ATTEMPTS),
        window_seconds=int(settings.RATE_LIMIT_LOGIN_WINDOW_SEC),
        message="Too many login attempts. Try again later.",
    )

    def _audit_login_failure(reason: str) -> None:
        try:
            record_audit_log(
                db=db,
                tool_name="auth:login_failed",
                staff_user_id=email_clean,
                tool_arguments={"email": email_clean, "reason": reason},
                proposed_payload={"action": "login_failed", "email": email_clean},
                confirmed_by_staff=False,
                status="FAILED",
                execution_result=reason,
            )
        except Exception as ae:
            logger.warning("Audit log failed for failed login: %s", ae)

    # 1. Look up the account in the user store (Firestore admin_authentication by default)
    user = _store_call(lambda: users_repo.get_by_email(db, email_clean))
    if not user:
        _audit_login_failure("invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # 2. Blocked or deactivated accounts never log in (their sessions are revoked)
    user_status = (getattr(user, "status", None) or "active").lower()
    if user_status == "blocked" or not getattr(user, "is_active", True):
        _store_call(lambda: users_repo.revoke_user_sessions(db, user.id))
        _audit_login_failure("account_blocked")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is blocked. All active sessions have been terminated. Access denied."
        )

    # 3. Verify the password. A plain-text password typed in the Firebase console is accepted once
    #    and immediately replaced by a bcrypt hash.
    password_ok = bool(user.password_hash) and verify_password(req.password, user.password_hash)
    if not password_ok:
        password_ok = bool(_store_call(lambda: users_repo.upgrade_legacy_password(db, user, req.password)))
    if not password_ok:
        _audit_login_failure("invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"}
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
            # Verify permission to access target domain
            if is_privileged_admin or canon_req in user_allowed_sites:
                user_domain = canon_req
                # Persist selected default website for this account if changed
                if getattr(user, "domain", None) != canon_req:
                    user.domain = canon_req
                    _store_call(lambda: users_repo.save_user(db, user))
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

    _store_call(lambda: users_repo.create_session(db, user.id, session_token, expires_at))

    try:
        record_audit_log(
            db=db,
            tool_name="auth:login",
            staff_user_id=user.email,
            tool_arguments={"email": user.email, "role": user_role, "domain": user_domain},
            proposed_payload={"action": "login", "email": user.email},
            confirmed_by_staff=True,
            status="SUCCESS",
            execution_result=f"Login for {user.email}",
        )
    except Exception as ae:
        logger.warning("Audit log failed for login: %s", ae)

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
    db: Any = Depends(get_db)
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

    logout_email = None
    if token_to_revoke:
        session = _store_call(lambda: users_repo.get_session_by_token(db, token_to_revoke))
        if session is not None and session.revoked_at is not None:
            session = None
        if session:
            try:
                owner = users_repo.get_by_id(db, session.user_id)
                logout_email = getattr(owner, "email", None)
            except Exception:
                logout_email = None
            _store_call(lambda: users_repo.revoke_session(db, session))
            try:
                record_audit_log(
                    db=db,
                    tool_name="auth:logout",
                    staff_user_id=logout_email or "unknown",
                    tool_arguments={"email": logout_email},
                    proposed_payload={"action": "logout"},
                    confirmed_by_staff=True,
                    status="SUCCESS",
                    execution_result=f"Logout for {logout_email or 'session'}",
                )
            except Exception as ae:
                logger.warning("Audit log failed for logout: %s", ae)

    return UserLogoutResponse(
        success=True,
        message="Logged out successfully."
    )


@router.get("/me", response_model=UserInfoResponse, tags=["Authentication"])
def get_current_user_profile(
    staff: AuthenticatedStaff = Depends(get_current_user_from_db),
    db: Any = Depends(get_db)
):
    """
    Returns the authenticated user's profile based on the active session token,
    including linked Google OAuth account details.
    """
    user = _store_call(lambda: users_repo.get_by_email(db, staff.email))
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
    status: Optional[str] = Query(None, description="Filter by status: active, blocked, deactivated"),
    staff: AuthenticatedStaff = Depends(require_admin_or_super_admin),
    db: Any = Depends(get_db)
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
    if staff_domain_key and staff_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your account belongs to a different domain."
        )

    # Role scope enforcement: regular admin cannot view super_admin accounts
    roles_filter = None
    if staff.role != "super_admin":
        roles_filter = ["admin", "worker", "employee"]
    if role:
        role_clean = role.strip().lower()
        wanted = ["worker", "employee"] if role_clean in ["worker", "employee"] else [role_clean]
        roles_filter = wanted if roles_filter is None else [r for r in wanted if r in roles_filter]

    all_users = _store_call(lambda: users_repo.list_users(
        db, roles=roles_filter, search=search, is_active=is_active, status=status
    ))

    # Filter by domain: Super Admins can inspect all domains; regular Admins are restricted to their allowed websites or active env
    if staff.role == "super_admin":
        users = all_users
    else:
        users = [
            u for u in all_users 
            if get_user_domain_key(u) == active_env_key or 
            any(normalize_tenant_domain_key(w) == get_user_domain_key(u) for w in (staff.allowed_websites or []))
        ]

    now = datetime.utcnow()
    results = []
    for u in users:
        active_sess_count = _store_call(lambda: users_repo.count_active_sessions(db, u.id))

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
    db: Any = Depends(get_db)
):
    """
    Creates a new administrator or worker user account.
    Hierarchy:
      - super_admin can create super_admin, admin, or worker accounts.
      - admin can only create worker accounts.
    Website Access Delegation:
      - super_admin can grant access to: All websites, Multiple websites, or Single specific website.
      - admin can only grant access to websites that they themselves have access to.
    Syncs with both Cloud Firestore admin_authentication.
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

    try:
        new_user = _store_call(lambda: users_repo.create_user(
            db,
            email=email_clean,
            name=req.name.strip() if req.name else email_clean.split("@")[0],
            role=target_role,
            domain=target_domain,
            allowed_websites=assigned_websites,
            password_hash=hash_password(req.password.strip()),
            is_active=req.is_active,
            status=req.status or "active",
            preferences={"allowed_websites": assigned_websites},
        ))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"User with email '{email_clean}' already exists.")

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
    db: Any = Depends(get_db)
):
    """
    Retrieves details and status for a specific user account.
    Enforces strict domain isolation (cannot access user of other website).
    """
    user = _store_call(lambda: users_repo.get_by_id(db, user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    active_env_key = get_current_env_domain_key()
    staff_domain_key = staff.domain_key or get_user_domain_key(staff.email, staff.domain)
    if staff_domain_key and staff_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Your account belongs to a different domain."
        )

    target_user_domain_key = get_user_domain_key(user)
    if target_user_domain_key != active_env_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found on this website domain."
        )

    if staff.role != "super_admin" and user.role == "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins cannot view super admin accounts.")

    active_sess_count = _store_call(lambda: users_repo.count_active_sessions(db, user.id))

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
    db: Any = Depends(get_db)
):
    """
    Updates user details (name, password reset, role, active status, domain, allowed_websites).
    Hierarchy:
      - super_admin can update any user account, assign any websites, and promote/demote roles.
      - admin can only update worker accounts (cannot edit admins or super_admins, cannot promote to admin).
    Strict Domain & Website Isolation:
      - Super admin can assign any canonical websites (all, multiple, single).
      - Admin can only grant websites within their own allowed_websites.
    Syncs with Cloud Firestore.
    """
    user = _store_call(lambda: users_repo.get_by_id(db, user_id))
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
        _store_call(lambda: users_repo.revoke_user_sessions(db, user.id))
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
        if target_status == "blocked":
            user.is_active = False
            _store_call(lambda: users_repo.revoke_user_sessions(db, user.id))
        elif target_status == "active":
            user.is_active = True
    if req.is_active is not None:
        user.is_active = req.is_active
        if not req.is_active:
            _store_call(lambda: users_repo.revoke_user_sessions(db, user.id))

    _store_call(lambda: users_repo.save_user(db, user))


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

    active_sess_count = _store_call(lambda: users_repo.count_active_sessions(db, user.id))

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
    db: Any = Depends(get_db)
):
    """
    Deletes a user account and cascades active sessions.
    Hierarchy:
      - super_admin can delete admin and worker accounts within this domain.
      - admin can delete worker accounts only within this domain.
      - No user can delete their own currently authenticated account.
      - Cross-domain deletion is strictly blocked (403).
    Removes account from Cloud Firestore admin_authentication.
    """
    user = _store_call(lambda: users_repo.get_by_id(db, user_id))
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

    deleted_email = user.email

    # Cascade sessions and remove the account
    _store_call(lambda: users_repo.delete_user(db, user))


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
    db: Any = Depends(get_db)
):
    """
    Super Admin API to block or unblock any administrator or worker user.
    When a user is blocked:
      - All active sessions are immediately revoked in Firestore.
      - Account status is set to 'blocked' and is_active to False.
      - Status is synced to Cloud Firestore admin_authentication collection (isBlocked=True).
      - Blocked users cannot log in (403 Forbidden) and cannot call any admin APIs (chat, CRUD, etc.).
    A Super Admin cannot block their own account.
    """
    user = _store_call(lambda: users_repo.get_by_id(db, user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if str(staff.user_id) == str(user_id) or staff.email.lower() == user.email.lower():
        raise HTTPException(
            status_code=400,
            detail="Super administrators cannot block their own account."
        )

    is_blocking = req.blocked
    if is_blocking:
        user.status = "blocked"
        user.is_active = False
        # Revoke all active sessions immediately
        _store_call(lambda: users_repo.save_user(db, user))
        revoked_count = _store_call(lambda: users_repo.revoke_user_sessions(db, user.id))
        logger.info(f"Blocked user {user.email} (id={user.id}). Revoked {revoked_count} active sessions.")
    else:
        user.status = "active"
        user.is_active = True
        _store_call(lambda: users_repo.save_user(db, user))
        logger.info(f"Unblocked user {user.email} (id={user.id}).")


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
        active_sessions_count=0 if is_blocking else _store_call(lambda: users_repo.count_active_sessions(db, user.id)),
        allowed_websites=resolve_user_allowed_websites(user, resolved_u_role)
    )



@router.post("/register", response_model=AdminUserDirectoryItem, status_code=status.HTTP_201_CREATED, tags=["Authentication"])
def register_user(
    req: RegisterRequest,
    db: Any = Depends(get_db),
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
        caller = get_current_user_from_db(db=db, bearer_creds=bearer_creds, staff_email_hdr=None, staff_token_hdr=None)
        if caller.role != "super_admin":
            raise HTTPException(status_code=403, detail="Only a super admin can register staff accounts.")
        actor = caller.email
    elif _admin_secret_matches(req.admin_secret):
        actor = "admin_secret"
    elif not _store_call(lambda: users_repo.any_user_exists(db)):
        actor = "bootstrap"  # first-run onboarding: the very first account may be created without a session
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

    # Registering never overwrites an existing account (that would be an account takeover).
    try:
        new_user = _store_call(lambda: users_repo.create_user(
            db,
            email=email_clean,
            name=req.name.strip() if req.name else email_clean.split("@")[0],
            role=target_role,
            domain=resolved_domain,
            password_hash=hash_password(plain_pass),
            is_active=True,
            allowed_websites=assigned_websites,
            preferences={"allowed_websites": assigned_websites},
        ))
    except ValueError:
        raise HTTPException(status_code=409, detail=f"User with email '{email_clean}' already exists.")

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


def _deliver_password_reset(email: str, token: str) -> None:
    """Email the token when SMTP is configured. Never log it in production."""
    from app.core.config import is_production_env
    from app.services.firestore_auth_service import remember_reset_token_for_tests

    remember_reset_token_for_tests(email, token)
    host = (settings.SMTP_HOST or "").strip()
    user = (settings.SMTP_USER or "").strip()
    if host and user:
        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(
                f"Use this link token to reset your password. It expires in 30 minutes and works once.\n\n{token}\n"
            )
            msg["Subject"] = "Password reset"
            msg["From"] = user
            msg["To"] = email
            with smtplib.SMTP(host, int(settings.SMTP_PORT or 587), timeout=10) as server:
                if settings.SMTP_PASSWORD:
                    server.starttls()
                    server.login(user, settings.SMTP_PASSWORD)
                server.send_message(msg)
            return
        except Exception as exc:
            logger.warning("Password reset email could not be sent: %s", exc)
    if not is_production_env():
        logger.debug("Password reset token for %s: %s", email, token)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    response_model_exclude_none=True,
    tags=["Authentication"],
)
def forgot_password_endpoint(
    req: ForgotPasswordRequest,
    request: Request,
    db: Any = Depends(get_db)
):
    """
    Always returns the same body. The reset token is emailed, never returned in production.
    """
    from app.core.config import is_production_env
    from app.core.rate_limit import enforce_rate_limit
    from app.services.firestore_auth_service import generate_password_reset_token

    email_clean = req.email.strip().lower()
    if not email_clean or "@" not in email_clean:
        raise HTTPException(status_code=400, detail="Invalid email format.")

    enforce_rate_limit(
        request,
        scope="forgot_password",
        identities=[email_clean],
        limit=int(settings.RATE_LIMIT_RESET_ATTEMPTS),
        window_seconds=int(settings.RATE_LIMIT_RESET_WINDOW_SEC),
        message="Too many password reset requests. Try again later.",
    )

    account_exists = False
    try:
        account_exists = bool(users_repo.get_by_email(db, email_clean))
    except Exception as exc:
        logger.warning("Forgot-password lookup failed: %s", exc)

    # Same cryptographic work either way so the response timing stays close.
    try:
        reset_token = generate_password_reset_token(email_clean, expires_in_seconds=1800)
    except Exception as exc:
        logger.warning("Forgot-password token could not be created: %s", exc)
        return ForgotPasswordResponse(success=True, message=_RESET_GENERIC_MESSAGE)
    if account_exists:
        _deliver_password_reset(email_clean, reset_token)
        try:
            record_audit_log(
                db=db,
                tool_name="auth:forgot_password",
                staff_user_id=email_clean,
                tool_arguments={"email": email_clean},
                proposed_payload={"action": "forgot_password_request", "email": email_clean},
                confirmed_by_staff=True,
                status="SUCCESS",
                execution_result=f"Password reset requested for {email_clean}"
            )
        except Exception as e:
            logger.warning(f"Audit log failed for forgot-password: {e}")

    expose = bool(settings.EXPOSE_RESET_TOKEN_IN_RESPONSE) and not is_production_env() and account_exists
    return ForgotPasswordResponse(
        success=True,
        message=_RESET_GENERIC_MESSAGE,
        reset_token=reset_token if expose else None,
    )


@router.post("/reset-password", response_model=ResetPasswordResponse, tags=["Authentication"])
def reset_password_endpoint(
    req: ResetPasswordRequest,
    request: Request,
    db: Any = Depends(get_db)
):
    """
    Resets the administrator password using a valid cryptographic reset token.
    Updates password in Cloud Firestore `admin_authentication`.
    Revokes any active user sessions to force a fresh login.
    """
    from app.core.rate_limit import enforce_rate_limit
    from app.services.firestore_auth_service import (
        invalidate_password_reset_tokens,
        verify_password_reset_token,
    )

    email_clean = req.email.strip().lower()
    new_pw = req.new_password.strip()
    enforce_rate_limit(
        request,
        scope="reset_password",
        identities=[email_clean],
        limit=int(settings.RATE_LIMIT_RESET_ATTEMPTS),
        window_seconds=int(settings.RATE_LIMIT_RESET_WINDOW_SEC),
        message="Too many password reset requests. Try again later.",
    )

    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters long.")

    verified_email = verify_password_reset_token(req.reset_token, expected_email=email_clean, consume=True)
    if not verified_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_RESET_GENERIC_ERROR,
        )

    # 1. Update in Cloud Firestore admin_authentication
    target = _store_call(lambda: users_repo.get_by_email(db, email_clean))
    if not target:
        raise HTTPException(status_code=404, detail="User account not found.")
    target.password_hash = hash_password(new_pw)
    target.legacy_plain_password = None
    _store_call(lambda: users_repo.save_user(db, target))
    _store_call(lambda: users_repo.revoke_user_sessions(db, target.id))
    invalidate_password_reset_tokens(email_clean)

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
    db: Any = Depends(get_db)
):
    """
    Changes the authenticated administrator's password.
    Requires current password verification.
    Updates password in Cloud Firestore `admin_authentication`.
    """

    old_pw = req.old_password.strip()
    new_pw = req.new_password.strip()

    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters long.")

    if old_pw == new_pw:
        raise HTTPException(status_code=400, detail="New password cannot be the same as the current password.")

    user = _store_call(lambda: users_repo.get_by_email(db, staff.email))
    verified = bool(
        user and user.password_hash and verify_password(old_pw, user.password_hash)
    ) or bool(user and _store_call(lambda: users_repo.upgrade_legacy_password(db, user, old_pw)))
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect."
        )

    user.password_hash = hash_password(new_pw)
    user.legacy_plain_password = None
    _store_call(lambda: users_repo.save_user(db, user))
    _store_call(lambda: users_repo.revoke_user_sessions(db, user.id))

    from app.services.firestore_auth_service import invalidate_password_reset_tokens
    invalidate_password_reset_tokens(staff.email)

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




