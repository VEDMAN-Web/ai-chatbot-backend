"""Store admin-uploaded property images. Attaching them to a listing is a separate chat step."""

from __future__ import annotations

import os
import re
import uuid
from typing import Dict, List, Tuple
from urllib.parse import urlparse

from fastapi import HTTPException, status

from app.services.report_service import (
    InMemoryStorageProvider,
    LocalStorageProvider,
    S3StorageProvider,
    get_storage_provider,
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_FILES = 10
PUBLIC_IMAGE_PREFIX = "/api/v1/admin/property-images/files/"
_FOLDER = "property-images"

_NAME_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_MEDIA = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
_EXT_KIND = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".webp": "webp",
}
_REJECTED_EXT = {
    ".svg": "SVG files are not allowed. Upload a jpg, png, or webp image.",
    ".html": "HTML files are not allowed. Upload a jpg, png, or webp image.",
    ".htm": "HTML files are not allowed. Upload a jpg, png, or webp image.",
    ".pdf": "PDF files are not allowed. Upload a jpg, png, or webp image.",
    ".gif": "GIF files are not allowed. Upload a jpg, png, or webp image.",
}


def sniff_image_kind(content: bytes) -> str:
    """Return jpeg, png, or webp from magic bytes, or an empty string."""
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return ""


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


def validate_upload(filename: str, content: bytes) -> Tuple[str, str]:
    """Return (extension without dot, media type). Raises 400 for anything else."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _REJECTED_EXT:
        raise _bad(_REJECTED_EXT[ext])
    if ext not in _EXT_KIND:
        raise _bad("Only jpg, jpeg, png, and webp images are allowed.")
    if len(content) > MAX_IMAGE_BYTES:
        raise _bad("Each image must be 10 MB or smaller.")
    if not content:
        raise _bad("That file is empty. Upload a jpg, png, or webp image.")
    kind = sniff_image_kind(content)
    if kind != _EXT_KIND[ext]:
        raise _bad("That file's contents are not a jpg, png, or webp image.")
    stored_ext = "jpg" if ext == ".jpeg" else ext[1:]
    return stored_ext, _MEDIA[stored_ext]


def public_image_url(filename: str) -> str:
    return f"{PUBLIC_IMAGE_PREFIX}{filename}"


def is_owned_image_url(url: str) -> bool:
    """True only for a URL this upload endpoint produces."""
    raw = (url or "").strip()
    if not raw or any(ch in raw for ch in (" ", "\n", "\r", "\\")):
        return False
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return False
    if parsed.hostname and parsed.hostname.lower() not in _own_hosts():
        return False
    path = parsed.path or ""
    marker = "/property-images/files/"
    if marker not in path:
        return False
    name = path.split(marker, 1)[1]
    return bool(name) and "/" not in name and bool(_NAME_RE.match(name))


def _own_hosts() -> set:
    from app.core.config import settings

    hosts = {"localhost", "127.0.0.1", "testserver"}
    for value in (
        getattr(settings, "BASE_WEBSITE_URL", "") or "",
        getattr(settings, "SITE_ORIGIN", "") or "",
    ):
        host = urlparse(str(value)).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def safe_image_filename(filename: str) -> str:
    name = os.path.basename((filename or "").strip())
    if not _NAME_RE.match(name):
        raise _bad("Unknown image file.")
    return name


def _directory(provider) -> str:
    from app.core.config import settings

    base = getattr(provider, "base_dir", None) or settings.REPORT_STORAGE_DIR
    folder = os.path.join(base, _FOLDER)
    os.makedirs(folder, exist_ok=True)
    return folder


def save_property_image(content: bytes, extension: str, media_type: str) -> Dict[str, object]:
    """Save bytes with the report storage provider under property-images/. Never uses the client name."""
    filename = f"{uuid.uuid4()}.{extension}"
    provider = get_storage_provider()
    if isinstance(provider, S3StorageProvider):
        client = provider._get_s3_client()
        client.put_object(
            Bucket=provider.bucket_name,
            Key=f"{_FOLDER}/{filename}",
            Body=content,
            ContentType=media_type,
        )
    elif isinstance(provider, InMemoryStorageProvider):
        provider._files[f"{_FOLDER}/{filename}"] = content
    else:
        if not isinstance(provider, LocalStorageProvider):
            provider = LocalStorageProvider()
        path = os.path.join(_directory(provider), filename)
        with open(path, "wb") as handle:
            handle.write(content)
    return {
        "url": public_image_url(filename),
        "filename": filename,
        "size": len(content),
    }


def load_property_image(filename: str) -> Tuple[bytes, str]:
    """Read one uploaded image. Used by the public file URL."""
    safe = safe_image_filename(filename)
    media = _MEDIA[safe.rsplit(".", 1)[-1].lower()]
    provider = get_storage_provider()
    if isinstance(provider, InMemoryStorageProvider):
        key = f"{_FOLDER}/{safe}"
        if key not in provider._files:
            raise FileNotFoundError(safe)
        return provider._files[key], media
    if isinstance(provider, S3StorageProvider):
        client = provider._get_s3_client()
        obj = client.get_object(Bucket=provider.bucket_name, Key=f"{_FOLDER}/{safe}")
        return obj["Body"].read(), media
    path = os.path.join(_directory(provider), safe)
    if not os.path.isfile(path):
        raise FileNotFoundError(safe)
    with open(path, "rb") as handle:
        return handle.read(), media


def read_upload_limited(upload) -> bytes:
    raw = upload.file.read(MAX_IMAGE_BYTES + 1)
    if raw is None:
        raw = b""
    if len(raw) > MAX_IMAGE_BYTES:
        raise _bad("Each image must be 10 MB or smaller.")
    return raw


def store_uploads(files: List) -> List[Dict[str, object]]:
    if not files or len(files) < 1:
        raise _bad("Upload between 1 and 10 images.")
    if len(files) > MAX_IMAGE_FILES:
        raise _bad("Upload between 1 and 10 images.")
    checked: List[Tuple[bytes, str, str]] = []
    for upload in files:
        content = read_upload_limited(upload)
        extension, media = validate_upload(getattr(upload, "filename", "") or "", content)
        checked.append((content, extension, media))
    return [save_property_image(content, extension, media) for content, extension, media in checked]
