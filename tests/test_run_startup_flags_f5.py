"""Stage F5: Postgres/alembic startup can be switched off for Firestore-only deploys."""
import importlib

import pytest


@pytest.fixture
def run_mod():
    return importlib.import_module("run")


def test_defaults_keep_current_behaviour(run_mod, monkeypatch):
    monkeypatch.delenv("SQL_STARTUP", raising=False)
    monkeypatch.delenv("RUN_ALEMBIC_ON_START", raising=False)
    assert run_mod.sql_startup_enabled() is True
    assert run_mod.alembic_on_start_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "off", "No", " FALSE "])
def test_flags_can_be_turned_off(run_mod, monkeypatch, value):
    monkeypatch.setenv("SQL_STARTUP", value)
    monkeypatch.setenv("RUN_ALEMBIC_ON_START", value)
    assert run_mod.sql_startup_enabled() is False
    assert run_mod.alembic_on_start_enabled() is False


def test_main_skips_sql_entirely_when_disabled(run_mod, monkeypatch):
    monkeypatch.setenv("SQL_STARTUP", "false")
    calls = []
    monkeypatch.setattr(run_mod, "init_database", lambda: calls.append("init") or True)
    monkeypatch.setattr(run_mod, "seed_data", lambda: calls.append("seed"))
    monkeypatch.setattr(run_mod.uvicorn, "run", lambda *a, **k: calls.append("uvicorn"))
    run_mod.main()
    assert calls == ["uvicorn"]
