import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.db.models import User
from app.core.tenant import get_current_env_domain
from app.services.firestore_service import get_admin_firestore_client as get_firestore_client

logger = logging.getLogger(__name__)

ADMIN_AUTH_COLLECTION = "admin_authentication"

try:  # removes a legacy plain-text "password" field when a document is updated
    from google.cloud.firestore_v1 import DELETE_FIELD as _DROP_PLAINTEXT
except Exception:  # pragma: no cover - library not installed (tests)
    _DROP_PLAINTEXT = None


def get_admin_from_firestore(email: str) -> Optional[Dict[str, Any]]:
    """
    Looks up an administrative user directly in Cloud Firestore `admin_authentication` collection.
    Checks document ID directly (e.g. 'user@domain.com') and queries by 'email' field.
    """
    client = get_firestore_client()
    if not client:
        logger.warning("Firestore client not available to fetch admin.")
        return None

    email_clean = (email or "").strip().lower()
    if not email_clean:
        return None

    try:
        col = client.collection(ADMIN_AUTH_COLLECTION)

        # 1. Direct document ID lookup with normalized email
        doc = col.document(email_clean).get()
        if doc.exists:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            return data

        # 2. Query where email == email_clean
        from google.cloud.firestore_v1.base_query import FieldFilter
        try:
            docs = list(col.where(filter=FieldFilter("email", "==", email_clean)).limit(1).stream())
        except Exception:
            docs = list(col.where("email", "==", email_clean).limit(1).stream())

        if docs:
            data = docs[0].to_dict() or {}
            data["id"] = docs[0].id
            return data

        # 3. Fallback: Check if document ID has replaced characters (e.g., '@' or '.' replaced with '_')
        slug_id = email_clean.replace("@", "_").replace(".", "_")
        doc_slug = col.document(slug_id).get()
        if doc_slug.exists:
            data = doc_slug.to_dict() or {}
            data["id"] = doc_slug.id
            return data

        return None
    except Exception as e:
        logger.error(f"Error fetching admin '{email_clean}' from Firestore collection '{ADMIN_AUTH_COLLECTION}': {e}")
        return None


def verify_firestore_admin_credentials(email: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Verifies an admin's password against live Firestore `admin_authentication` data.
    Supports both bcrypt hashed passwords (passwordHash/password_hash) and
    plaintext passwords (password) for accounts configured via Firebase Console.
    """
    admin_doc = get_admin_from_firestore(email)
    if not admin_doc:
        return None

    # Check active status
    is_active = admin_doc.get("isActive")
    if is_active is None:
        is_active = admin_doc.get("is_active", True)
    if not is_active:
        logger.warning(f"Firestore admin '{email}' account is deactivated.")
        return None

    stored_hash = admin_doc.get("passwordHash") or admin_doc.get("password_hash")

    # Only a bcrypt hash is accepted. A stored hash is never accepted as the password itself, and
    # plain-text passwords are upgraded through users_repo.upgrade_legacy_password at login.
    is_verified = bool(stored_hash) and verify_password(password, stored_hash)

    if is_verified:
        return {
            "email": admin_doc.get("email") or email.strip().lower(),
            "name": admin_doc.get("name") or "Admin Staff",
            "role": admin_doc.get("role") or "super_admin",
            "is_active": True,
            "id": admin_doc.get("id") or email.strip().lower(),
            "password_hash": stored_hash or hash_password(password)
        }

    return None


def add_or_update_admin_in_firestore(
    email: str,
    password: str,
    role: str = "super_admin",
    name: str = "Admin Staff",
    domain: Optional[str] = None,
    is_active: bool = True,
    allowed_websites: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Adds or updates an admin user in Cloud Firestore `admin_authentication` collection.
    Stores both hashed and structured credentials for live authentication.
    """
    client = get_firestore_client()
    if not client:
        raise RuntimeError("Firestore client is not initialized or unavailable.")

    email_clean = (email or "").strip().lower()
    if not email_clean:
        raise ValueError("Admin email cannot be empty.")

    now_iso = datetime.now(timezone.utc).isoformat()
    hashed = hash_password(password)
    target_domain = domain or get_current_env_domain()

    payload = {
        "email": email_clean,
        "passwordHash": hashed,
        "role": role,
        "name": name,
        "domain": target_domain,
        "siteOrigin": target_domain,
        "isActive": is_active,
        "updatedAt": now_iso
    }
    if allowed_websites:
        payload["allowed_websites"] = list(allowed_websites)
        payload["websites"] = list(allowed_websites)

    doc_ref = client.collection(ADMIN_AUTH_COLLECTION).document(email_clean)
    existing_doc = doc_ref.get()
    if not existing_doc.exists:
        payload["createdAt"] = now_iso
        payload["status"] = "active"

    doc_ref.set(payload, merge=True)
    logger.info(f"Successfully configured admin '{email_clean}' for domain '{target_domain}' in Firestore collection '{ADMIN_AUTH_COLLECTION}'.")
    return payload


def sync_firestore_admin_to_local_db(
    db: Session,
    firestore_admin: Dict[str, Any],
    plain_password: Optional[str] = None
) -> User:
    """
    Synchronizes an authenticated Firestore admin user to the local PostgreSQL User table,
    ensuring foreign key referential integrity for UserSession tokens.
    """
    email_clean = firestore_admin["email"].strip().lower()
    user = db.query(User).filter(User.email.ilike(email_clean)).first()

    pw_hash = firestore_admin.get("password_hash")
    if not pw_hash and plain_password:
        pw_hash = hash_password(plain_password)

    fs_domain = (
        firestore_admin.get("domain")
        or firestore_admin.get("siteOrigin")
        or firestore_admin.get("site_origin")
        or get_current_env_domain()
    )

    fs_websites = firestore_admin.get("allowed_websites") or firestore_admin.get("websites")
    fs_status = firestore_admin.get("status") or ("blocked" if not firestore_admin.get("is_active", True) else "active")
    fs_active = False if fs_status == "blocked" else bool(firestore_admin.get("is_active", True))

    if not user:
        user = User(
            email=email_clean,
            name=firestore_admin.get("name") or "Admin Staff",
            role=firestore_admin.get("role") or "super_admin",
            domain=fs_domain,
            password_hash=pw_hash,
            is_active=fs_active,
            status=fs_status
        )
        if fs_websites:
            user.allowed_websites = list(fs_websites)
            user.preferences = {"allowed_websites": list(fs_websites), "status": fs_status}
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"Created local database user cache for Firestore admin '{email_clean}' (domain: {fs_domain}, status: {fs_status}).")
    else:
        # Update role, domain, password_hash, status, or allowed_websites if changed
        if pw_hash and user.password_hash != pw_hash:
            user.password_hash = pw_hash
        user.role = firestore_admin.get("role") or user.role or "super_admin"
        user.is_active = fs_active
        user.status = fs_status
        if fs_domain and (not getattr(user, "domain", None) or user.domain != fs_domain):
            user.domain = fs_domain
        if fs_websites:
            user.allowed_websites = list(fs_websites)
            prefs = dict(user.preferences or {})
            prefs["allowed_websites"] = list(fs_websites)
            prefs["status"] = fs_status
            user.preferences = prefs
        db.commit()
        db.refresh(user)

    return user


def update_admin_password_in_firestore(email: str, new_password: str) -> bool:
    """
    Updates the admin's password and passwordHash in Cloud Firestore `admin_authentication` collection.
    """
    client = get_firestore_client()
    if not client:
        logger.warning("Firestore client not available to update password.")
        return False

    email_clean = (email or "").strip().lower()
    if not email_clean or not new_password:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    hashed = hash_password(new_password)

    try:
        col = client.collection(ADMIN_AUTH_COLLECTION)
        doc_ref = col.document(email_clean)
        doc = doc_ref.get()

        if doc.exists:
            doc_ref.update({
                "passwordHash": hashed,
                "updatedAt": now_iso,
                **({"password": _DROP_PLAINTEXT} if _DROP_PLAINTEXT is not None else {}),
            })
            logger.info(f"Updated password for admin '{email_clean}' in Firestore doc '{email_clean}'.")
            return True

        # Check if stored by query or slug
        from google.cloud.firestore_v1.base_query import FieldFilter
        try:
            docs = list(col.where(filter=FieldFilter("email", "==", email_clean)).limit(1).stream())
        except Exception:
            docs = list(col.where("email", "==", email_clean).limit(1).stream())

        if docs:
            target_ref = col.document(docs[0].id)
            target_ref.update({
                "passwordHash": hashed,
                "updatedAt": now_iso,
                **({"password": _DROP_PLAINTEXT} if _DROP_PLAINTEXT is not None else {}),
            })
            logger.info(f"Updated password for admin '{email_clean}' in Firestore doc '{docs[0].id}'.")
            return True

        # Fallback: create or merge
        doc_ref.set({
            "email": email_clean,
            "passwordHash": hashed,
            "updatedAt": now_iso,
            "isActive": True,
            "role": "super_admin"
        }, merge=True)
        logger.info(f"Created/merged password for admin '{email_clean}' in Firestore doc '{email_clean}'.")
        return True

    except Exception as e:
        logger.error(f"Error updating Firestore password for '{email_clean}': {e}")
        return False


import threading as _reset_threading

_reset_lock = _reset_threading.Lock()
_password_epoch: dict = {}
_used_reset_jti: set = set()
_test_reset_tokens: dict = {}
_RESET_TOKEN_MAX_SECONDS = 1800


def invalidate_password_reset_tokens(email: str) -> None:
    """Outstanding reset tokens for this email stop working after a password change."""
    email_clean = (email or "").strip().lower()
    if not email_clean:
        return
    with _reset_lock:
        _password_epoch[email_clean] = int(_password_epoch.get(email_clean, 0)) + 1


def get_test_reset_token(email: str) -> Optional[str]:
    """Test hook. Always empty in production, including when the expose flag is set."""
    from app.core.config import is_production_env

    if is_production_env():
        return None
    return _test_reset_tokens.get((email or "").strip().lower())


def remember_reset_token_for_tests(email: str, token: str) -> None:
    from app.core.config import is_production_env

    email_clean = (email or "").strip().lower()
    if not email_clean:
        return
    with _reset_lock:
        if is_production_env():
            _test_reset_tokens.pop(email_clean, None)
            return
        _test_reset_tokens[email_clean] = token


def _reset_secret() -> str:
    from app.core.config import settings

    return settings.INTERNAL_API_SECRET or settings.SECRET_KEY or "tajlandia-reset-secret"


def generate_password_reset_token(email: str, expires_in_seconds: int = 1800) -> str:
    """
    Signed single-use reset token. Lifetime is capped at 30 minutes.
    """
    import hmac
    import hashlib
    import secrets
    import time
    import urllib.parse

    lifetime = min(int(expires_in_seconds or _RESET_TOKEN_MAX_SECONDS), _RESET_TOKEN_MAX_SECONDS)
    lifetime = max(1, lifetime)
    email_clean = (email or "").strip().lower()
    exp = int(time.time()) + lifetime
    with _reset_lock:
        epoch = int(_password_epoch.get(email_clean, 0))
    jti = secrets.token_hex(16)
    payload = f"reset:{email_clean}:{exp}:{epoch}:{jti}"
    signature = hmac.new(_reset_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.quote(f"{payload}:{signature}")


def verify_password_reset_token(
    token: str,
    expected_email: Optional[str] = None,
    *,
    consume: bool = False,
) -> Optional[str]:
    """
    Check signature, expiry, password epoch, and single use.
    Expired, used, and tampered tokens all return None.
    """
    import hmac
    import hashlib
    import time
    import urllib.parse

    if not token:
        return None

    try:
        decoded = urllib.parse.unquote(token.strip())
        parts = decoded.split(":")
        if len(parts) != 6 or parts[0] != "reset":
            return None

        _, token_email, exp_str, epoch_str, jti, sig = parts
        exp = int(exp_str)
        epoch = int(epoch_str)
        if time.time() > exp:
            return None

        payload = f"reset:{token_email}:{exp_str}:{epoch_str}:{jti}"
        expected_sig = hmac.new(_reset_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None

        email_clean = token_email.lower()
        if expected_email and expected_email.strip().lower() != email_clean:
            return None

        with _reset_lock:
            if epoch != int(_password_epoch.get(email_clean, 0)):
                return None
            if jti in _used_reset_jti:
                return None
            if consume:
                _used_reset_jti.add(jti)
        return email_clean
    except Exception as e:
        logger.warning(f"Error parsing reset token: {e}")
        return None


def delete_admin_from_firestore(email: str) -> bool:
    """
    Deletes an administrative user from Cloud Firestore `admin_authentication` collection.
    Checks direct document ID, query by email, and slug ID.
    """
    client = get_firestore_client()
    if not client:
        logger.warning("Firestore client not available to delete admin.")
        return False

    email_clean = (email or "").strip().lower()
    if not email_clean:
        return False

    try:
        col = client.collection(ADMIN_AUTH_COLLECTION)
        deleted = False

        # 1. Direct document ID
        doc_ref = col.document(email_clean)
        if doc_ref.get().exists:
            doc_ref.delete()
            deleted = True
            logger.info(f"Deleted admin '{email_clean}' from Firestore doc '{email_clean}'.")

        # 2. Query where email == email_clean
        from google.cloud.firestore_v1.base_query import FieldFilter
        try:
            docs = list(col.where(filter=FieldFilter("email", "==", email_clean)).stream())
        except Exception:
            docs = list(col.where("email", "==", email_clean).stream())

        for d in docs:
            col.document(d.id).delete()
            deleted = True
            logger.info(f"Deleted admin '{email_clean}' from Firestore doc '{d.id}'.")

        # 3. Slug id
        slug_id = email_clean.replace("@", "_").replace(".", "_")
        slug_ref = col.document(slug_id)
        if slug_ref.get().exists:
            slug_ref.delete()
            deleted = True
            logger.info(f"Deleted admin '{email_clean}' from Firestore doc '{slug_id}'.")

        return deleted
    except Exception as e:
        logger.error(f"Error deleting admin '{email_clean}' from Firestore: {e}")
        return False


def update_admin_in_firestore(
    email: str,
    name: Optional[str] = None,
    role: Optional[str] = None,
    domain: Optional[str] = None,
    is_active: Optional[bool] = None,
    password: Optional[str] = None,
    allowed_websites: Optional[List[str]] = None,
    status: Optional[str] = None
) -> bool:
    """
    Updates fields for an admin in Cloud Firestore `admin_authentication` collection.
    """
    client = get_firestore_client()
    if not client:
        logger.warning("Firestore client not available to update admin.")
        return False

    email_clean = (email or "").strip().lower()
    if not email_clean:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    updates: Dict[str, Any] = {"updatedAt": now_iso}
    if name is not None:
        updates["name"] = name
    if role is not None:
        updates["role"] = role
    if domain is not None:
        updates["domain"] = domain
        updates["siteOrigin"] = domain
    if status is not None:
        updates["status"] = status
        if status.lower() == "blocked":
            updates["isActive"] = False
            updates["isBlocked"] = True
        elif status.lower() == "active":
            updates["isBlocked"] = False
    if is_active is not None:
        updates["isActive"] = is_active
        if not is_active and "status" not in updates:
            updates["isBlocked"] = True
    if password is not None and password.strip():
        if _DROP_PLAINTEXT is not None:
            updates["password"] = _DROP_PLAINTEXT
        updates["passwordHash"] = hash_password(password.strip())
    if allowed_websites is not None:
        updates["allowed_websites"] = list(allowed_websites)
        updates["websites"] = list(allowed_websites)

    try:
        col = client.collection(ADMIN_AUTH_COLLECTION)
        doc_ref = col.document(email_clean)
        if doc_ref.get().exists:
            doc_ref.update(updates)
            logger.info(f"Updated Firestore admin '{email_clean}' directly.")
            return True

        from google.cloud.firestore_v1.base_query import FieldFilter
        try:
            docs = list(col.where(filter=FieldFilter("email", "==", email_clean)).limit(1).stream())
        except Exception:
            docs = list(col.where("email", "==", email_clean).limit(1).stream())

        if docs:
            col.document(docs[0].id).update(updates)
            logger.info(f"Updated Firestore admin '{email_clean}' via email query.")
            return True

        # Fallback set with merge
        doc_ref.set(updates, merge=True)
        return True
    except Exception as e:
        logger.error(f"Error updating Firestore admin '{email_clean}': {e}")
        return False


