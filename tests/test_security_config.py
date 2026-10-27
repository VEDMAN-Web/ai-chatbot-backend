"""Secret and audit defaults."""
import pytest

from app.core import config
from app.core.config import DEFAULT_INTERNAL_API_SECRET, Settings, is_weak_secret


@pytest.mark.parametrize("value", [None, "", "   ", "secret", "changeme", DEFAULT_INTERNAL_API_SECRET.upper(), "short-value"])
def test_weak_secrets_are_detected(value):
    assert is_weak_secret(value) is True


def test_long_random_secret_is_not_weak():
    assert is_weak_secret("Zk3p9Qw8Lr2Xv7Tn5Bm1Hc4Jd6Fg0Ys") is False


def _production(monkeypatch, secret):
    monkeypatch.setattr(config, "is_test_environment", lambda: False)
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(config.settings, "INTERNAL_API_SECRET", secret)


@pytest.mark.parametrize("secret", [DEFAULT_INTERNAL_API_SECRET, "", "too-short"])
def test_production_refuses_to_start_with_a_weak_secret(monkeypatch, secret):
    _production(monkeypatch, secret)
    with pytest.raises(RuntimeError, match="INTERNAL_API_SECRET"):
        config.assert_secrets_configured_or_exit()


def test_production_starts_with_a_strong_secret(monkeypatch):
    _production(monkeypatch, "Zk3p9Qw8Lr2Xv7Tn5Bm1Hc4Jd6Fg0Ys")
    config.assert_secrets_configured_or_exit()


def test_other_environments_only_warn(monkeypatch):
    monkeypatch.setattr(config, "is_test_environment", lambda: False)
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(config.settings, "INTERNAL_API_SECRET", DEFAULT_INTERNAL_API_SECRET)
    config.assert_secrets_configured_or_exit()


def test_test_environment_is_never_blocked(monkeypatch):
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(config.settings, "INTERNAL_API_SECRET", DEFAULT_INTERNAL_API_SECRET)
    config.assert_secrets_configured_or_exit()  # pytest counts as a test environment


def test_audit_logging_is_on_by_default():
    assert Settings.model_fields["ENABLE_AUDIT_LOGGING"].default is True
