"""Stage F4: SQL -> Firestore migration of staff users and live sessions."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, User, UserSession
from app.repositories.users_repo import SESSIONS_COLLECTION, USERS_COLLECTION, hash_token
from scripts import migrate_sql_to_firestore as mig
from tests.fake_firestore import FakeFirestore


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[User.__table__, UserSession.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def fs():
    return FakeFirestore()


def _user(db, email, **kw):
    row = User(
        id=kw.pop("id", uuid.uuid4()),
        email=email,
        name=kw.pop("name", email.split("@")[0]),
        role=kw.pop("role", "admin"),
        domain="tajlandia.com",
        allowed_websites=["tajlandia.com"],
        password_hash=kw.pop("password_hash", "$2b$12$hash-" + email),
        is_active=True,
        status="active",
        preferences={},
        created_at=datetime(2025, 1, 2, 3, 4, 5),
        **kw,
    )
    db.add(row)
    db.commit()
    return row


def _session(db, user, token, *, hours=24, revoked=False):
    row = UserSession(
        user_id=user.id,
        token=token,
        expires_at=datetime.utcnow() + timedelta(hours=hours),
        revoked_at=datetime.utcnow() if revoked else None,
    )
    db.add(row)
    db.commit()
    return row


def test_dry_run_writes_nothing(db, fs):
    _user(db, "a@x.com")
    report = mig.run(db, fs, apply=False)
    assert report["mode"] == "DRY-RUN"
    assert report["users"]["created"] == ["a@x.com"]
    assert mig._find_existing(fs, "a@x.com") is None


def test_apply_creates_user_with_sql_uuid_and_hash(db, fs):
    row = _user(db, "A@X.com")
    report = mig.run(db, fs, apply=True)
    found = mig._find_existing(fs, "a@x.com")
    assert found is not None
    assert found["data"]["userId"] == str(row.id)
    assert found["data"]["passwordHash"] == row.password_hash
    assert "password" not in found["data"]
    assert report["parity"]["ok"] is True


def test_apply_is_idempotent(db, fs):
    _user(db, "a@x.com")
    mig.run(db, fs, apply=True)
    again = mig.run(db, fs, apply=True)
    assert again["users"]["created"] == []
    assert again["users"]["unchanged"] == ["a@x.com"]
    assert again["sessions"]["copied"] == 0


def test_existing_firestore_doc_is_not_overwritten(db, fs):
    row = _user(db, "a@x.com", name="SQL Name", role="worker")
    fs.collection(USERS_COLLECTION).document("a@x.com").set(
        {"email": "a@x.com", "name": "Console Name", "role": "super_admin", "passwordHash": "fs-hash"}
    )
    report = mig.run(db, fs, apply=True)
    data = mig._find_existing(fs, "a@x.com")["data"]
    assert data["name"] == "Console Name"
    assert data["role"] == "super_admin"
    assert data["passwordHash"] == "fs-hash"
    assert data["userId"] == str(row.id)  # missing id filled in
    assert report["users"]["filled"][0]["email"] == "a@x.com"


def test_legacy_plaintext_password_is_not_copied_or_removed(db, fs):
    _user(db, "a@x.com", password_hash=None)
    fs.collection(USERS_COLLECTION).document("a@x.com").set({"email": "a@x.com", "password": "plain"})
    mig.run(db, fs, apply=True)
    data = mig._find_existing(fs, "a@x.com")["data"]
    assert data["password"] == "plain"  # left for upgrade-on-login, never copied elsewhere
    assert "passwordHash" not in data or not data["passwordHash"]


def test_userid_conflict_is_reported_and_sessions_remapped(db, fs):
    row = _user(db, "a@x.com")
    fs.collection(USERS_COLLECTION).document("a@x.com").set({"email": "a@x.com", "userId": "fs-id-1"})
    _session(db, row, "tok-1")
    report = mig.run(db, fs, apply=True)
    assert report["users"]["conflicts"][0]["firestore_user_id"] == "fs-id-1"
    assert mig._find_existing(fs, "a@x.com")["data"]["userId"] == "fs-id-1"
    sess = fs.collection(SESSIONS_COLLECTION).document(hash_token("tok-1")).get().to_dict()
    assert sess["userId"] == "fs-id-1"


def test_only_live_sessions_are_copied_and_hashed(db, fs):
    row = _user(db, "a@x.com")
    _session(db, row, "live-token")
    _session(db, row, "expired-token", hours=-1)
    _session(db, row, "revoked-token", revoked=True)
    report = mig.run(db, fs, apply=True)
    assert report["sessions"]["copied"] == 1
    assert report["sessions"]["dropped_expired_or_revoked"] == 2
    snap = fs.collection(SESSIONS_COLLECTION).document(hash_token("live-token")).get()
    doc = snap.to_dict()
    assert doc["userId"] == str(row.id)
    assert "live-token" not in str(doc)
    assert fs.collection(SESSIONS_COLLECTION).document(hash_token("expired-token")).get().to_dict() in (None, {})


def test_migrated_session_authenticates_via_repo(db, fs, monkeypatch):
    from app.core.config import settings
    from app.repositories import users_repo
    from tests.fake_firestore import patch_firestore

    row = _user(db, "a@x.com")
    _session(db, row, "live-token")
    mig.run(db, fs, apply=True)
    patch_firestore(monkeypatch, fs)
    monkeypatch.setattr(settings, "STORE_USERS", "firestore")
    sess = users_repo.get_session_by_token(None, "live-token")
    assert sess is not None and sess.user_id == str(row.id)
    assert users_repo.get_by_id(None, str(row.id)).email == "a@x.com"


def test_users_only_skips_sessions(db, fs):
    row = _user(db, "a@x.com")
    _session(db, row, "t")
    report = mig.run(db, fs, apply=True, users_only=True)
    assert "sessions" not in report
