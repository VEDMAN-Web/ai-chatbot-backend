"""Startup is Firestore-only: no schema sync, migration or seeding step runs before uvicorn."""
import importlib

import pytest


@pytest.fixture
def run_mod():
    return importlib.import_module("run")


def test_run_module_has_no_sql_startup_hooks(run_mod):
    for name in ("init_database", "seed_data", "sql_startup_enabled", "alembic_on_start_enabled"):
        assert not hasattr(run_mod, name), name


def test_main_only_starts_uvicorn(run_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(run_mod.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("PORT", "8123")
    run_mod.main()
    assert len(calls) == 1
    assert calls[0][0][0] == "app.main:app"
    assert calls[0][1]["port"] == 8123


def test_bad_port_falls_back_to_8000(run_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(run_mod.uvicorn, "run", lambda *a, **k: calls.append(k))
    monkeypatch.setenv("PORT", "not-a-number")
    run_mod.main()
    assert calls[0]["port"] == 8000
