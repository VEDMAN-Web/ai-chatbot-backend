"""CORS is an explicit allow-list, and API docs are hidden in production."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_unlisted_origin_has_no_allow_header(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.CORS_ALLOWED_ORIGINS", "https://allowed.example")
    monkeypatch.setattr("app.core.config.settings.ENV", "production")
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_listed_origin_is_reflected(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.CORS_ALLOWED_ORIGINS", "https://allowed.example")
    monkeypatch.setattr("app.core.config.settings.ENV", "production")
    resp = client.get("/health", headers={"Origin": "https://allowed.example"})
    assert resp.headers.get("access-control-allow-origin") == "https://allowed.example"
    assert resp.headers.get("access-control-allow-credentials") == "true"


def test_docs_hidden_in_production(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.ENV", "production")
    monkeypatch.setattr("app.core.config.settings.ENABLE_API_DOCS", False)
    resp = client.get("/docs")
    assert resp.status_code == 404


def test_docs_available_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.ENV", "production")
    monkeypatch.setattr("app.core.config.settings.ENABLE_API_DOCS", True)
    resp = client.get("/docs")
    assert resp.status_code == 200
