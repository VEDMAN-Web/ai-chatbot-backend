"""
Staff users + login sessions repository (Stage F3) — Postgres / Firestore / both.

Callers must use this module; never query User / UserSession or the Firestore client directly.

Firestore layout
  admin_authentication/{email}   one document per staff user (document id = lower-case email)
  admin_sessions/{sha256(token)} one document per login session (the raw token is never stored)

Records returned by this module expose the same attribute names as the SQLAlchemy rows
(id, email, name, role, domain, allowed_websites, password_hash, is_active, status,
google_email, google_refresh_token, preferences, created_at) so existing code keeps working.
Change attributes, then call save_user(db, user).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.repositories import _fs_utils as fs
from app.repositories.store_mode import (
    reads_firestore_first,
    reads_postgres,
    require_firestore_client,
    store_mode,
    writes_firestore,
    writes_postgres,
)

logger = logging.getLogger(__name__)

USERS_COLLECTION = "admin_authentication"
SESSIONS_COLLECTION = "admin_sessions"
_ID_NAMESPACE = uuid.UUID("6f1c2f0e-7b0a-4d7e-9a53-0d0b7c1f6a11")


def _mode() -> str:
    return store_mode("STORE_USERS", default="firestore")


def _client():
    return fs.admin_fs_client()


def _naive_utc(value: Any) -> Optional[datetime]:
    dt = fs.parse_dt(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return fs.to_iso(value)


def _as_uuid(value: Any) -> Any:
    """SQL rows use UUID columns; ids travel as strings elsewhere. Convert when it is a UUID."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except Exception:
        return value


def stable_user_id(email: str) -> str:
    """Deterministic id for users that have no id yet (console-created documents)."""
    return str(uuid.uuid5(_ID_NAMESPACE, (email or "").strip().lower()))


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

class UserRecord:
    """Duck-typed stand-in for the SQLAlchemy User row."""

    def __init__(self, data: Dict[str, Any]):
        d = dict(data or {})
        email = str(d.get("email") or d.get("id") or "").strip().lower()
        self.email = email
        self.id = str(d.get("userId") or d.get("user_id") or stable_user_id(email))
        self.name = d.get("name")
        self.role = d.get("role") or "worker"
        self.domain = d.get("domain") or d.get("siteOrigin") or d.get("site_origin")
        sites = d.get("allowed_websites") or d.get("websites") or []
        self.allowed_websites = list(sites) if isinstance(sites, (list, tuple)) else []
        self.password_hash = d.get("passwordHash") or d.get("password_hash")
        # Legacy plaintext password typed into the Firebase console. Only used once, to upgrade to a hash.
        self.legacy_plain_password = d.get("password")
        is_active = d.get("isActive")
        if is_active is None:
            is_active = d.get("is_active", True)
        self.status = d.get("status")
        if d.get("isBlocked") is True:
            self.status = "blocked"
        self.is_active = bool(is_active)
        if (self.status or "").lower() == "blocked":
            self.is_active = False
        if not self.status:
            self.status = "active" if self.is_active else "blocked"
        self.google_email = d.get("googleEmail") or d.get("google_email")
        self.google_refresh_token = d.get("googleRefreshToken") or d.get("google_refresh_token")
        prefs = d.get("preferences")
        self.preferences = copy.deepcopy(prefs) if isinstance(prefs, dict) else {}
        self.created_at = _naive_utc(d.get("createdAt") or d.get("created_at")) or datetime.utcnow()
        self._has_user_id = bool(d.get("userId") or d.get("user_id"))

    def to_firestore_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "userId": str(self.id),
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "domain": self.domain,
            "siteOrigin": self.domain,
            "allowed_websites": list(self.allowed_websites or []),
            "websites": list(self.allowed_websites or []),
            "passwordHash": self.password_hash,
            "isActive": bool(self.is_active),
            "isBlocked": (self.status or "").lower() == "blocked",
            "status": self.status or ("active" if self.is_active else "blocked"),
            "googleEmail": self.google_email,
            "googleRefreshToken": self.google_refresh_token,
            "preferences": copy.deepcopy(self.preferences or {}),
            "createdAt": _iso(self.created_at),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        return payload


class SessionRecord:
    """Duck-typed stand-in for the SQLAlchemy UserSession row."""

    def __init__(self, data: Dict[str, Any]):
        d = dict(data or {})
        self.id = str(d.get("id") or "")
        self.user_id = str(d.get("userId") or d.get("user_id") or "")
        self.token = d.get("token")  # never stored in Firestore; only set on creation
        self.token_hash = d.get("tokenHash") or d.get("token_hash") or self.id
        self.expires_at = _naive_utc(d.get("expiresAt") or d.get("expires_at")) or datetime.utcnow()
        self.revoked_at = _naive_utc(d.get("revokedAt") or d.get("revoked_at"))
        self.created_at = _naive_utc(d.get("createdAt") or d.get("created_at")) or datetime.utcnow()

    def to_firestore_dict(self) -> Dict[str, Any]:
        return {
            "userId": self.user_id,
            "tokenHash": self.token_hash,
            "expiresAt": _iso(self.expires_at),
            "revokedAt": _iso(self.revoked_at),
            "createdAt": _iso(self.created_at),
        }


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def _sql_models():
    from app.db.models import User, UserSession

    return User, UserSession


def _pg_get_by_email(db: Any, email: str):
    from sqlalchemy import func

    User, _ = _sql_models()
    return db.query(User).filter(func.lower(User.email) == email).first()


def _pg_get_by_id(db: Any, user_id: str):
    User, _ = _sql_models()
    try:
        return db.query(User).filter(User.id == _as_uuid(user_id)).first()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _pg_mirror_user(db: Any, rec: UserRecord) -> None:
    """Best-effort upsert of a Firestore user into SQL (only used when STORE_USERS=both)."""
    try:
        User, _ = _sql_models()
        row = _pg_get_by_email(db, rec.email)
        if row is None:
            row = User(email=rec.email)
            db.add(row)
        row.name = rec.name
        row.role = rec.role
        row.domain = rec.domain
        row.allowed_websites = list(rec.allowed_websites or [])
        row.password_hash = rec.password_hash
        row.is_active = rec.is_active
        row.status = rec.status
        row.google_email = rec.google_email
        row.google_refresh_token = rec.google_refresh_token
        row.preferences = copy.deepcopy(rec.preferences or {})
        db.commit()
    except Exception as e:
        logger.warning("users_repo: SQL mirror failed for %s: %s", rec.email, e)
        try:
            db.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _fs_client_required():
    return require_firestore_client(_client(), store_name="STORE_USERS")


def _fs_get_by_email(email: str) -> Optional[UserRecord]:
    client = _client() if _mode() == "both" else _fs_client_required()
    if client is None:
        return None
    data = fs.get_document(client, USERS_COLLECTION, email)
    if data is None:
        slug = email.replace("@", "_").replace(".", "_")
        data = fs.get_document(client, USERS_COLLECTION, slug)
    if data is None:
        try:
            for snap in client.collection(USERS_COLLECTION).where("email", "==", email).limit(1).stream():
                data = fs.doc_to_dict(snap)
                break
        except Exception as e:
            logger.debug("users_repo: email query failed: %s", e)
    if data is None:
        return None
    data.setdefault("email", email)
    rec = UserRecord(data)
    if not rec._has_user_id:
        # One-time write so the id used in sessions and URLs never changes.
        try:
            fs.set_document(client, USERS_COLLECTION, str(data.get("id") or email), {"userId": rec.id}, merge=True)
            rec._has_user_id = True
        except Exception as e:
            logger.debug("users_repo: could not persist userId for %s: %s", email, e)
    rec._doc_id = str(data.get("id") or email)
    return rec


def _fs_doc_id(rec: UserRecord) -> str:
    return getattr(rec, "_doc_id", None) or rec.email


def _fs_all_users() -> List[UserRecord]:
    client = _client() if _mode() == "both" else _fs_client_required()
    out: List[UserRecord] = []
    for snap in fs.stream_collection(client, USERS_COLLECTION):
        data = fs.doc_to_dict(snap)
        if not (data.get("email") or data.get("id")):
            continue
        data.setdefault("email", data.get("id"))
        rec = UserRecord(data)
        rec._doc_id = str(data.get("id") or rec.email)
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_by_email(db: Any, email: str):
    email = (email or "").strip().lower()
    if not email:
        return None
    mode = _mode()
    if reads_firestore_first(mode):
        rec = _fs_get_by_email(email)
        if rec is not None:
            return rec
    if reads_postgres(mode) and db is not None:
        return _pg_get_by_email(db, email)
    return None


def get_by_id(db: Any, user_id: Any):
    uid = str(user_id or "").strip()
    if not uid:
        return None
    mode = _mode()
    if reads_firestore_first(mode):
        if "@" in uid:
            rec = _fs_get_by_email(uid.lower())
            if rec is not None:
                return rec
        for rec in _fs_all_users():
            if str(rec.id) == uid:
                return rec
    if reads_postgres(mode) and db is not None:
        return _pg_get_by_id(db, uid)
    return None


def list_users(
    db: Any,
    *,
    roles: Optional[List[str]] = None,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    status: Optional[str] = None,
) -> List[Any]:
    """All matching users, oldest first."""
    mode = _mode()
    users: List[Any] = []
    if reads_firestore_first(mode):
        users = _fs_all_users()
    if not users and reads_postgres(mode) and db is not None:
        User, _ = _sql_models()
        users = db.query(User).order_by(User.created_at.asc()).all()

    wanted_roles = {r.strip().lower() for r in roles} if roles else None
    term = (search or "").strip().lower()
    wanted_status = (status or "").strip().lower()

    def keep(u: Any) -> bool:
        if wanted_roles is not None and (u.role or "").strip().lower() not in wanted_roles:
            return False
        if term and term not in (u.email or "").lower() and term not in (u.name or "").lower():
            return False
        if is_active is not None and bool(u.is_active) != bool(is_active):
            return False
        if wanted_status and (u.status or "").strip().lower() != wanted_status:
            return False
        return True

    users = [u for u in users if keep(u)]
    users.sort(key=lambda u: getattr(u, "created_at", None) or datetime.min)
    return users


def any_user_exists(db: Any) -> bool:
    return bool(list_users(db))


def first_active_super_admin(db: Any):
    for u in list_users(db, roles=["super_admin"], is_active=True):
        if (u.status or "").lower() != "blocked":
            return u
    return None


def create_user(db: Any, **fields: Any):
    """Create a user. Raises ValueError when the email already exists."""
    email = str(fields.get("email") or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    if get_by_email(db, email) is not None:
        raise ValueError(f"User with email '{email}' already exists.")
    mode = _mode()
    rec = UserRecord({
        "email": email,
        "name": fields.get("name"),
        "role": fields.get("role") or "worker",
        "domain": fields.get("domain"),
        "allowed_websites": fields.get("allowed_websites") or [],
        "passwordHash": fields.get("password_hash"),
        "isActive": fields.get("is_active", True),
        "status": fields.get("status") or "active",
        "googleEmail": fields.get("google_email"),
        "googleRefreshToken": fields.get("google_refresh_token"),
        "preferences": fields.get("preferences") or {},
        "userId": str(uuid.uuid4()),
        "createdAt": datetime.utcnow(),
    })
    if writes_firestore(mode):
        client = _fs_client_required() if mode == "firestore" else _client()
        if client is not None:
            fs.set_document(client, USERS_COLLECTION, email, rec.to_firestore_dict(), merge=False)
            rec._doc_id = email
    if writes_postgres(mode) and db is not None:
        if mode == "postgres":
            User, _ = _sql_models()
            row = User(
                email=email,
                name=rec.name,
                role=rec.role,
                domain=rec.domain,
                allowed_websites=list(rec.allowed_websites or []),
                password_hash=rec.password_hash,
                is_active=rec.is_active,
                status=rec.status,
                google_email=rec.google_email,
                google_refresh_token=rec.google_refresh_token,
                preferences=copy.deepcopy(rec.preferences or {}),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row
        _pg_mirror_user(db, rec)
    return rec


def save_user(db: Any, user: Any) -> Any:
    """Persist attribute changes made on a record returned by this module."""
    if isinstance(user, UserRecord):
        mode = _mode()
        if writes_firestore(mode):
            client = _fs_client_required() if mode == "firestore" else _client()
            if client is not None:
                payload = user.to_firestore_dict()
                fs.set_document(client, USERS_COLLECTION, _fs_doc_id(user), payload, merge=True)
        if writes_postgres(mode) and mode == "both" and db is not None:
            _pg_mirror_user(db, user)
        return user
    # SQLAlchemy row (STORE_USERS=postgres)
    try:
        db.commit()
        db.refresh(user)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    return user


def upgrade_legacy_password(db: Any, user: Any, plain_password: str) -> bool:
    """
    Accounts typed into the Firebase console may hold a plain-text password. Accept it once,
    replace it by a bcrypt hash and delete the plain-text field. Returns True when it matched.
    """
    legacy = getattr(user, "legacy_plain_password", None)
    if not legacy or str(legacy) != str(plain_password):
        return False
    from app.core.security import hash_password

    user.password_hash = hash_password(plain_password)
    user.legacy_plain_password = None
    save_user(db, user)
    try:
        client = _client()
        if client is not None:
            from google.cloud import firestore as _gcf  # type: ignore

            client.collection(USERS_COLLECTION).document(_fs_doc_id(user)).update({"password": _gcf.DELETE_FIELD})
    except Exception:
        try:
            client = _client()
            if client is not None:
                ref = client.collection(USERS_COLLECTION).document(_fs_doc_id(user))
                data = (ref.get().to_dict() or {})
                data.pop("password", None)
                ref.set(data)
        except Exception as e:
            logger.warning("users_repo: could not remove plain-text password for %s: %s", user.email, e)
    return True


def delete_user(db: Any, user: Any) -> None:
    """Delete the user and all of their sessions."""
    mode = _mode()
    uid = str(user.id)
    email = (user.email or "").strip().lower()
    delete_user_sessions(db, uid)
    if writes_firestore(mode):
        client = _client()
        if client is not None:
            fs.delete_document(client, USERS_COLLECTION, _fs_doc_id(user) if isinstance(user, UserRecord) else email)
    if writes_postgres(mode) and db is not None:
        try:
            row = user if not isinstance(user, UserRecord) else _pg_get_by_email(db, email)
            if row is not None:
                db.delete(row)
                db.commit()
        except Exception as e:
            logger.warning("users_repo: SQL delete failed for %s: %s", email, e)
            try:
                db.rollback()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(db: Any, user_id: Any, token: str, expires_at: datetime):
    mode = _mode()
    now = datetime.utcnow()
    rec = SessionRecord({
        "id": hash_token(token),
        "userId": str(user_id),
        "tokenHash": hash_token(token),
        "expiresAt": expires_at,
        "createdAt": now,
    })
    rec.token = token
    if writes_firestore(mode):
        client = _fs_client_required() if mode == "firestore" else _client()
        if client is not None:
            fs.set_document(client, SESSIONS_COLLECTION, rec.id, rec.to_firestore_dict(), merge=False)
    if writes_postgres(mode) and db is not None and mode == "postgres":
        _, UserSession = _sql_models()
        row = UserSession(user_id=_as_uuid(user_id), token=token, expires_at=expires_at, created_at=now)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    return rec


def get_session_by_token(db: Any, token: str):
    token = (token or "").strip()
    if not token:
        return None
    mode = _mode()
    if reads_firestore_first(mode):
        client = _client() if mode == "both" else _fs_client_required()
        data = fs.get_document(client, SESSIONS_COLLECTION, hash_token(token)) if client is not None else None
        if data is not None:
            rec = SessionRecord(data)
            rec.token = token
            return rec
    if reads_postgres(mode) and db is not None:
        _, UserSession = _sql_models()
        return db.query(UserSession).filter(UserSession.token == token).first()
    return None


def revoke_session(db: Any, session: Any) -> None:
    if session is None or session.revoked_at is not None:
        return
    session.revoked_at = datetime.utcnow()
    if isinstance(session, SessionRecord):
        client = _client()
        if client is not None:
            fs.set_document(client, SESSIONS_COLLECTION, session.id, {"revokedAt": _iso(session.revoked_at)}, merge=True)
    else:
        db.commit()


def _fs_sessions_for_user(user_id: str) -> List[SessionRecord]:
    client = _client() if _mode() == "both" else _fs_client_required()
    if client is None:
        return []
    out: List[SessionRecord] = []
    try:
        snaps = list(client.collection(SESSIONS_COLLECTION).where("userId", "==", str(user_id)).stream())
    except Exception as e:
        logger.warning("users_repo: session query failed: %s", e)
        snaps = []
    for snap in snaps:
        out.append(SessionRecord(fs.doc_to_dict(snap)))
    return out


def revoke_user_sessions(db: Any, user_id: Any) -> int:
    """Revoke every active session of a user. Returns how many were revoked."""
    mode = _mode()
    uid = str(user_id)
    count = 0
    now = datetime.utcnow()
    if writes_firestore(mode) or reads_firestore_first(mode):
        client = _client()
        if client is not None:
            for sess in _fs_sessions_for_user(uid):
                if sess.revoked_at is None:
                    fs.set_document(client, SESSIONS_COLLECTION, sess.id, {"revokedAt": _iso(now)}, merge=True)
                    count += 1
    if reads_postgres(mode) and db is not None:
        try:
            _, UserSession = _sql_models()
            count += db.query(UserSession).filter(
                UserSession.user_id == _as_uuid(user_id),
                UserSession.revoked_at.is_(None),
            ).update({"revoked_at": now}, synchronize_session=False)
            db.commit()
        except Exception as e:
            logger.warning("users_repo: SQL revoke failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
    return int(count)


def count_active_sessions(db: Any, user_id: Any) -> int:
    mode = _mode()
    uid = str(user_id)
    now = datetime.utcnow()
    if reads_firestore_first(mode):
        client = _client()
        if mode == "firestore":
            _fs_client_required()
        if client is not None:
            return sum(
                1 for s in _fs_sessions_for_user(uid)
                if s.revoked_at is None and s.expires_at > now
            )
    if reads_postgres(mode) and db is not None:
        _, UserSession = _sql_models()
        return db.query(UserSession).filter(
            UserSession.user_id == _as_uuid(user_id),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        ).count()
    return 0


def delete_user_sessions(db: Any, user_id: Any) -> None:
    mode = _mode()
    uid = str(user_id)
    if writes_firestore(mode):
        client = _client()
        if client is not None:
            for sess in _fs_sessions_for_user(uid):
                fs.delete_document(client, SESSIONS_COLLECTION, sess.id)
    if writes_postgres(mode) and db is not None:
        try:
            _, UserSession = _sql_models()
            db.query(UserSession).filter(UserSession.user_id == _as_uuid(user_id)).delete()
            db.commit()
        except Exception as e:
            logger.warning("users_repo: SQL session delete failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
