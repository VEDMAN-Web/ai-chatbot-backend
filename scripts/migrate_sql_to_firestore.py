"""
Stage F4: copy staff users and live sessions from SQL into Firestore.

    python -m scripts.migrate_sql_to_firestore              # dry run (default), prints a report
    python -m scripts.migrate_sql_to_firestore --apply      # write to Firestore
    python -m scripts.migrate_sql_to_firestore --apply --users-only

Rules:
  * Idempotent: running it twice changes nothing the second time.
  * Existing Firestore user documents are never overwritten. Only missing fields are filled in.
  * Passwords: only the bcrypt hash is copied. A Firestore hash wins over the SQL hash.
  * userId in Firestore = the old SQL uuid, so existing sessions and links keep working.
    If a Firestore doc already has a different userId, it is kept and the session is remapped.
  * Sessions: only unexpired, unrevoked ones. Stored as sha256(token), never the raw token.
  * Run the dry run first and check "conflicts" before --apply.
ONE-OFF TOOL. Postgres is no longer part of the app. This script reads the old `users` and `user_sessions`
tables with plain SQL and needs two packages that are NOT in requirements.txt:

    pip install sqlalchemy psycopg2-binary
    DATABASE_URL=postgresql://... python -m scripts.migrate_sql_to_firestore

Run it (dry run first, then --apply) BEFORE deploying a build without Postgres, or nobody can log in.
The database URL comes from the DATABASE_URL environment variable only. It is never stored in code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.repositories import _fs_utils as fs
from app.repositories.users_repo import (
    SESSIONS_COLLECTION,
    USERS_COLLECTION,
    SessionRecord,
    UserRecord,
    hash_token,
)

# Fields filled in on an existing Firestore doc only when it is missing/empty there.
_FILL_IF_MISSING = (
    "name", "role", "domain", "siteOrigin", "allowed_websites", "websites",
    "googleEmail", "googleRefreshToken", "preferences", "createdAt", "passwordHash",
)


def _find_existing(client: Any, email: str) -> Optional[Dict[str, Any]]:
    """Return {'doc_id': ..., 'data': ...} for an existing admin_authentication doc, or None."""
    slug = email.replace("@", "_").replace(".", "_")
    for doc_id in (email, slug):
        data = fs.get_document(client, USERS_COLLECTION, doc_id)
        if data is not None:
            return {"doc_id": doc_id, "data": data}
    try:
        for snap in client.collection(USERS_COLLECTION).where("email", "==", email).limit(1).stream():
            data = fs.doc_to_dict(snap)
            return {"doc_id": str(data.get("id") or email), "data": data}
    except Exception:
        pass
    return None


def _sql_user_record(row: Any) -> UserRecord:
    rec = UserRecord({
        "email": (row.email or "").strip().lower(),
        "userId": str(row.id),
        "name": row.name,
        "role": row.role,
        "domain": row.domain,
        "allowed_websites": list(row.allowed_websites or []),
        "passwordHash": row.password_hash,
        "isActive": bool(row.is_active),
        "status": row.status,
        "googleEmail": row.google_email,
        "googleRefreshToken": row.google_refresh_token,
        "preferences": dict(row.preferences or {}),
        "createdAt": row.created_at,
    })
    return rec


def migrate_users(users: List[Any], client: Any, *, apply: bool = False) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "sql_users": 0, "created": [], "filled": [], "unchanged": [],
        "conflicts": [], "skipped": [], "id_map": {},
    }
    for row in sorted(users, key=lambda r: (r.email or "")):
        report["sql_users"] += 1
        email = (row.email or "").strip().lower()
        if not email:
            report["skipped"].append(str(row.id))
            continue
        sql_rec = _sql_user_record(row)
        existing = _find_existing(client, email)

        if existing is None:
            report["created"].append(email)
            report["id_map"][str(row.id)] = str(row.id)
            if apply:
                fs.set_document(client, USERS_COLLECTION, email, sql_rec.to_firestore_dict(), merge=False)
            continue

        data = existing["data"]
        fs_user_id = data.get("userId") or data.get("user_id")
        effective_id = str(fs_user_id or row.id)
        report["id_map"][str(row.id)] = effective_id
        if fs_user_id and str(fs_user_id) != str(row.id):
            report["conflicts"].append({
                "email": email, "sql_user_id": str(row.id), "firestore_user_id": str(fs_user_id),
                "action": "kept Firestore userId; sessions remapped",
            })

        wanted = sql_rec.to_firestore_dict()
        patch: Dict[str, Any] = {}
        if not fs_user_id:
            patch["userId"] = str(row.id)
        for key in _FILL_IF_MISSING:
            if data.get(key) in (None, "", [], {}) and wanted.get(key) not in (None, "", [], {}):
                patch[key] = wanted[key]
        # Never carry a plaintext password forward.
        if patch:
            report["filled"].append({"email": email, "fields": sorted(patch)})
            if apply:
                fs.set_document(client, USERS_COLLECTION, existing["doc_id"], patch, merge=True)
        else:
            report["unchanged"].append(email)
    return report


def migrate_sessions(sessions: List[Any], client: Any, id_map: Dict[str, str], *, apply: bool = False) -> Dict[str, Any]:
    now = datetime.utcnow()
    report: Dict[str, Any] = {"sql_sessions": 0, "copied": 0, "already_present": 0, "dropped_expired_or_revoked": 0, "orphaned": 0}
    for row in sessions:
        report["sql_sessions"] += 1
        if row.revoked_at is not None or row.expires_at is None or row.expires_at <= now:
            report["dropped_expired_or_revoked"] += 1
            continue
        user_id = id_map.get(str(row.user_id))
        if not user_id:
            report["orphaned"] += 1
            continue
        doc_id = hash_token(row.token)
        if fs.get_document(client, SESSIONS_COLLECTION, doc_id) is not None:
            report["already_present"] += 1
            continue
        report["copied"] += 1
        if apply:
            rec = SessionRecord({
                "id": doc_id, "userId": user_id, "tokenHash": doc_id,
                "expiresAt": row.expires_at, "createdAt": row.created_at,
            })
            fs.set_document(client, SESSIONS_COLLECTION, doc_id, rec.to_firestore_dict(), merge=False)
    return report


def parity(users: List[Any], client: Any) -> Dict[str, Any]:
    """Compare SQL and Firestore after a run. ok=True means every SQL user exists in Firestore with a userId."""
    missing: List[str] = []
    no_hash: List[str] = []
    for row in users:
        email = (row.email or "").strip().lower()
        existing = _find_existing(client, email)
        if existing is None:
            missing.append(email)
            continue
        data = existing["data"]
        if not (data.get("userId") or data.get("user_id")):
            missing.append(email)
        if not (data.get("passwordHash") or data.get("password_hash")):
            no_hash.append(email)
    return {"ok": not missing, "missing_in_firestore": missing, "users_without_password_hash": no_hash}


def load_from_postgres(url: str) -> Dict[str, List[Any]]:
    """Read users and sessions with plain SQL (needs sqlalchemy + psycopg2, installed only for this one-off)."""
    from sqlalchemy import create_engine, text

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            users = [SimpleNamespace(**dict(r._mapping)) for r in conn.execute(text(
                "SELECT id, email, name, role, domain, allowed_websites, password_hash, is_active, status, "
                "google_email, google_refresh_token, preferences, created_at FROM users"
            ))]
            sessions = [SimpleNamespace(**dict(r._mapping)) for r in conn.execute(text(
                "SELECT id, user_id, token, expires_at, revoked_at, created_at FROM user_sessions"
            ))]
    finally:
        engine.dispose()
    return {"users": users, "sessions": sessions}


def run(users: List[Any], sessions: List[Any], client: Any, *, apply: bool = False, users_only: bool = False) -> Dict[str, Any]:
    migrated = migrate_users(users, client, apply=apply)
    out: Dict[str, Any] = {"mode": "APPLY" if apply else "DRY-RUN", "users": {k: v for k, v in migrated.items() if k != "id_map"}}
    if not users_only:
        out["sessions"] = migrate_sessions(sessions, client, migrated["id_map"], apply=apply)
    if apply:
        out["parity"] = parity(users, client)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write to Firestore (default is a dry run).")
    parser.add_argument("--users-only", action="store_true", help="Skip sessions.")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("Set the DATABASE_URL environment variable to the old Postgres URL.", file=sys.stderr)
        return 2
    client = fs.admin_fs_client()
    if client is None:
        print("Firestore client unavailable. Check ADMIN_FIRESTORE_* / credentials.", file=sys.stderr)
        return 2
    loaded = load_from_postgres(url)
    report = run(loaded["users"], loaded["sessions"], client, apply=args.apply, users_only=args.users_only)
    print(json.dumps(report, indent=2, default=str))
    if report["users"]["conflicts"]:
        print("\nWARNING: userId conflicts found. Review before relying on old links.", file=sys.stderr)
    if args.apply and not report["parity"]["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
