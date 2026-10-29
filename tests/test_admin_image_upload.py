"""Property image uploads stay on the local storage provider. No AWS calls."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin import router as admin_router
from app.core.auth import AuthenticatedStaff, require_admin_or_employee
from app.core.rate_limit import reset_rate_limiter
from app.core.deps import FirestoreSession, get_db
from app.services.admin_image_commands import _NoRedirect, lookup_image_content_type
from app.services.report_service import LocalStorageProvider

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 16
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
HTML = b"<html><body>no</body></html>"


def _staff():
    return AuthenticatedStaff(
        user_id="admin@tajlandia.com",
        email="admin@tajlandia.com",
        role="admin",
        allowed_websites=["tajlandia.com"],
        selected_websites=["tajlandia.com"],
    )


def _client(monkeypatch, *, authorize=True):
    reset_rate_limiter()
    folder = tempfile.mkdtemp()
    monkeypatch.setattr(
        "app.services.admin_image_storage.get_storage_provider",
        lambda: LocalStorageProvider(base_dir=folder),
    )
    app = FastAPI()
    app.include_router(admin_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: FirestoreSession()
    if authorize:
        app.dependency_overrides[require_admin_or_employee] = lambda: _staff()
    return TestClient(app), folder


def _upload(client, files):
    return client.post("/api/v1/admin/property-images/upload", files=files)


def test_upload_accepts_jpg_png_and_webp(monkeypatch):
    client, folder = _client(monkeypatch)
    response = _upload(client, [
        ("files", ("pool.jpg", JPEG, "image/jpeg")),
        ("files", ("plan.png", PNG, "image/png")),
        ("files", ("view.webp", WEBP, "image/webp")),
    ])
    assert response.status_code == 200, response.text
    saved = response.json()["files"]
    assert len(saved) == 3
    for item, content, ext in zip(saved, (JPEG, PNG, WEBP), ("jpg", "png", "webp")):
        assert item["filename"].endswith("." + ext)
        assert item["filename"].split(".")[0] != "pool"
        assert "pool.jpg" not in item["url"]
        assert item["url"].endswith(item["filename"])
        assert item["size"] == len(content)
        path = Path(folder) / "property-images" / item["filename"]
        assert path.read_bytes() == content
        fetched = client.get(item["url"])
        assert fetched.status_code == 200
        assert fetched.content == content


def test_upload_rejects_unsafe_and_oversized_files(monkeypatch):
    client, folder = _client(monkeypatch)
    svg = _upload(client, [("files", ("logo.svg", SVG, "image/svg+xml"))])
    assert svg.status_code == 400
    assert "svg" in svg.json()["detail"].lower()
    html = _upload(client, [("files", ("page.html", HTML, "text/html"))])
    assert html.status_code == 400
    assert "html" in html.json()["detail"].lower()
    renamed = _upload(client, [("files", ("notes.jpg", b"this is not a jpeg", "image/jpeg"))])
    assert renamed.status_code == 400
    assert "not a jpg" in renamed.json()["detail"].lower()
    huge = _upload(client, [("files", ("big.jpg", JPEG + (b"\x00" * (10 * 1024 * 1024)), "image/jpeg"))])
    assert huge.status_code == 400
    assert "10 MB" in huge.json()["detail"]
    too_many = [("files", (f"{index}.jpg", JPEG, "image/jpeg")) for index in range(11)]
    assert _upload(client, too_many).status_code == 400
    assert "1 and 10" in _upload(client, too_many).json()["detail"]
    stored = Path(folder) / "property-images"
    assert not stored.exists() or list(stored.glob("*")) == []


def test_upload_requires_staff_login(monkeypatch):
    client, _folder = _client(monkeypatch, authorize=False)
    response = _upload(client, [("files", ("pool.jpg", JPEG, "image/jpeg"))])
    assert response.status_code == 401


def test_head_check_uses_a_five_second_timeout_and_no_redirect(monkeypatch):
    seen = {}

    class _Response:
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _open(self, request, timeout=None, *args, **kwargs):
        seen["timeout"] = timeout
        seen["method"] = request.get_method()
        return _Response()

    monkeypatch.setattr("urllib.request.OpenerDirector.open", _open)
    assert lookup_image_content_type("https://cdn.example.com/photo") == "image/jpeg"
    assert seen["timeout"] == 5
    assert seen["method"] == "HEAD"
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/a") is None


