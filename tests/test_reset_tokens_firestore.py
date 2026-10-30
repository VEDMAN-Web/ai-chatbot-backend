"""Password reset state (epoch + single use) lives in Firestore so every instance agrees."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import firestore_auth_service as svc
from tests.fake_firestore import FakeFirestore, docs_in_collection, patch_firestore


@pytest.fixture
def fs(monkeypatch):
    fake = patch_firestore(monkeypatch, FakeFirestore())
    svc._used_reset_jti.clear()
    svc._password_epoch.clear()
    return fake


def test_token_roundtrip_and_single_use_in_firestore(fs):
    token = svc.generate_password_reset_token("a@x.com")
    assert svc.verify_password_reset_token(token, expected_email="a@x.com") == "a@x.com"  # check only
    assert svc.verify_password_reset_token(token, expected_email="a@x.com", consume=True) == "a@x.com"
    assert svc.verify_password_reset_token(token, expected_email="a@x.com", consume=True) is None
    assert svc.verify_password_reset_token(token, expected_email="a@x.com") is None
    assert svc._used_reset_jti == set()  # nothing kept in process memory
    assert any(d for d in docs_in_collection(fs, svc.RESET_COLLECTION))


def test_second_instance_sees_the_same_state(fs, monkeypatch):
    token = svc.generate_password_reset_token("a@x.com")
    assert svc.verify_password_reset_token(token, consume=True) == "a@x.com"
    svc._used_reset_jti.clear()  # simulate another process: no shared memory
    svc._password_epoch.clear()
    assert svc.verify_password_reset_token(token, consume=True) is None


def test_password_change_invalidates_outstanding_tokens_everywhere(fs):
    token = svc.generate_password_reset_token("a@x.com")
    svc.invalidate_password_reset_tokens("a@x.com")
    svc._password_epoch.clear()  # another instance
    assert svc.verify_password_reset_token(token, consume=True) is None
    fresh = svc.generate_password_reset_token("a@x.com")
    assert svc.verify_password_reset_token(fresh, consume=True) == "a@x.com"


def test_tampered_and_wrong_email_rejected(fs):
    token = svc.generate_password_reset_token("a@x.com")
    assert svc.verify_password_reset_token(token, expected_email="b@x.com") is None
    assert svc.verify_password_reset_token(token[:-2] + "00") is None


def test_production_without_firestore_fails_closed(monkeypatch):
    monkeypatch.setattr(svc, "_reset_store", lambda: None)
    monkeypatch.setattr("app.core.config.is_production_env", lambda: True)
    with pytest.raises(RuntimeError):
        svc.generate_password_reset_token("a@x.com")
    # verification never succeeds on a store error
    monkeypatch.setattr("app.core.config.is_production_env", lambda: False)
    token = svc.generate_password_reset_token("a@x.com")
    monkeypatch.setattr("app.core.config.is_production_env", lambda: True)
    assert svc.verify_password_reset_token(token, consume=True) is None


def test_dev_without_firestore_still_uses_memory(monkeypatch):
    monkeypatch.setattr(svc, "_reset_store", lambda: None)
    svc._used_reset_jti.clear()
    svc._password_epoch.clear()
    token = svc.generate_password_reset_token("a@x.com")
    assert svc.verify_password_reset_token(token, consume=True) == "a@x.com"
    assert svc.verify_password_reset_token(token, consume=True) is None
