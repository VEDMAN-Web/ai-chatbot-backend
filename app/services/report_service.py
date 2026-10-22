import os
import io
import csv
import json
import hmac
import hashlib
import base64
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from abc import ABC, abstractmethod
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.db.models import Property, PropertySpec, Lead
from app.core.config import settings

logger = logging.getLogger(__name__)

ALLOWED_REPORT_EXTENSIONS = {".pdf", ".xlsx", ".docx", ".csv"}
REPORT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
}


class StorageError(Exception):
    """Raised when a configured remote store fails (no silent local fallback)."""


class ReportNotFoundError(Exception):
    """Report metadata or bytes missing."""


class ReportExpiredError(Exception):
    """Report past its retention window."""


class SignedLinkError(Exception):
    """Invalid or expired signed download token."""

    def __init__(self, message: str, *, expired: bool = False):
        super().__init__(message)
        self.expired = expired


# 1. Abstract Storage Interface
class StorageProvider(ABC):
    mode: str = "local"

    @abstractmethod
    def save_file(self, filename: str, content: bytes, content_type: str = "application/pdf", **kwargs) -> str:
        """Saves file bytes and returns a retrieval URL or local path."""
        pass

    @abstractmethod
    def exists(self, filename: str) -> bool:
        pass

    @abstractmethod
    def get_bytes(self, filename: str) -> bytes:
        pass

    @abstractmethod
    def delete(self, filename: str) -> bool:
        pass

    def get_presigned_url(self, filename: str, expires_in: int = 600) -> Optional[str]:
        return None

    def local_path(self, filename: str) -> Optional[str]:
        return None


class LocalStorageProvider(StorageProvider):
    mode = "local"

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or settings.REPORT_STORAGE_DIR
        os.makedirs(self.base_dir, exist_ok=True)

    def cleanup_old_reports(self, max_files: int = 5, max_age_hours: int = 24) -> int:
        """
        Automatically prunes old generated report files to prevent disk clutter.
        Keeps at most `max_files` latest reports, and removes any files older than `max_age_hours`.
        Returns number of deleted files.
        """
        if not os.path.exists(self.base_dir):
            return 0
        now = datetime.now().timestamp()
        deleted_count = 0
        try:
            files = []
            for fname in os.listdir(self.base_dir):
                if fname.endswith((".xlsx", ".pdf", ".docx", ".csv")) and (
                    fname.startswith("tajlandia_report_") or fname.startswith("tajlandia_lead_report_")
                ):
                    full_path = os.path.join(self.base_dir, fname)
                    if os.path.isfile(full_path):
                        mtime = os.path.getmtime(full_path)
                        files.append((full_path, mtime, fname))

            # Sort newest first
            files.sort(key=lambda x: x[1], reverse=True)

            for idx, (path, mtime, fname) in enumerate(files):
                age_hours = (now - mtime) / 3600.0
                if idx >= max_files or age_hours > max_age_hours:
                    try:
                        os.remove(path)
                        _delete_report_metadata(fname)
                        deleted_count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return deleted_count

    def save_file(self, filename: str, content: bytes, content_type: str = "application/pdf", **kwargs) -> str:
        safe = validate_report_filename(filename)
        file_path = os.path.join(self.base_dir, safe)
        with open(file_path, "wb") as f:
            f.write(content)
        # Automatically prune old report files (short-term disk hygiene)
        self.cleanup_old_reports(max_files=5, max_age_hours=24)
        if "property" in safe.lower() or "brochure" in safe.lower():
            return f"/api/v1/chat/brochure/file/{safe}"
        return f"/api/v1/admin/reports/{safe}"

    def exists(self, filename: str) -> bool:
        try:
            safe = validate_report_filename(filename)
        except ValueError:
            return False
        path = os.path.join(self.base_dir, safe)
        return os.path.isfile(path)

    def get_bytes(self, filename: str) -> bytes:
        safe = validate_report_filename(filename)
        path = os.path.join(self.base_dir, safe)
        if not os.path.isfile(path):
            raise ReportNotFoundError(f"Report not found: {safe}")
        with open(path, "rb") as f:
            return f.read()

    def delete(self, filename: str) -> bool:
        try:
            safe = validate_report_filename(filename)
            path = os.path.join(self.base_dir, safe)
            if os.path.isfile(path):
                os.remove(path)
                return True
        except Exception:
            pass
        return False

    def local_path(self, filename: str) -> Optional[str]:
        try:
            safe = validate_report_filename(filename)
        except ValueError:
            return None
        path = os.path.join(self.base_dir, safe)
        return path if os.path.isfile(path) else None


class S3StorageProvider(StorageProvider):
    mode = "s3"

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        region_name: str = "us-east-1",
        profile_name: Optional[str] = None,
        folder: str = "brochures"
    ):
        self.endpoint_url = endpoint_url or os.getenv("HF_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "https://s3.hf.co/itisha249"
        self.bucket_name = bucket_name or os.getenv("HF_S3_BUCKET") or os.getenv("AWS_S3_BUCKET") or "tajlandia"
        self.region_name = region_name or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
        self.profile_name = profile_name or os.getenv("AWS_PROFILE") or "hf"
        self.folder = folder

    def _object_key(self, filename: str) -> str:
        safe = validate_report_filename(filename)
        return f"{self.folder}/{safe}" if self.folder else safe

    def _get_s3_client(self):
        import boto3
        from botocore.config import Config

        boto_config = Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4"
        )

        access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("HF_ACCESS_KEY")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("HF_SECRET_KEY")

        if access_key and secret_key:
            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=self.region_name
            )
        else:
            try:
                session = boto3.Session(profile_name=self.profile_name, region_name=self.region_name)
            except Exception:
                session = boto3.Session(region_name=self.region_name)

        return session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
            config=boto_config
        )

    def save_file(self, filename: str, content: bytes, content_type: str = "application/pdf", **kwargs) -> str:
        safe = validate_report_filename(filename)
        key = self._object_key(safe)
        try:
            s3 = self._get_s3_client()
            s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type
            )
        except Exception as e:
            logger.error(
                "S3 upload failed for %s: %s. Not falling back to local storage.",
                safe,
                e,
                exc_info=True,
            )
            raise StorageError(
                "Report generated but could not be stored permanently"
            ) from e
        # Always expose the authenticated admin path; serving streams/redirects from S3
        if "property" in safe.lower() or "brochure" in safe.lower():
            return f"/api/v1/chat/brochure/file/{safe}"
        return f"/api/v1/admin/reports/{safe}"

    def exists(self, filename: str) -> bool:
        try:
            s3 = self._get_s3_client()
            s3.head_object(Bucket=self.bucket_name, Key=self._object_key(filename))
            return True
        except Exception:
            return False

    def get_bytes(self, filename: str) -> bytes:
        try:
            s3 = self._get_s3_client()
            obj = s3.get_object(Bucket=self.bucket_name, Key=self._object_key(filename))
            return obj["Body"].read()
        except Exception as e:
            raise ReportNotFoundError(f"Report not found in S3: {filename}") from e

    def delete(self, filename: str) -> bool:
        try:
            s3 = self._get_s3_client()
            s3.delete_object(Bucket=self.bucket_name, Key=self._object_key(filename))
            return True
        except Exception:
            return False

    def get_presigned_url(self, filename: str, expires_in: int = 600) -> Optional[str]:
        try:
            s3 = self._get_s3_client()
            return s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": self._object_key(filename)},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            logger.warning("Could not generate S3 presigned URL for %s: %s", filename, e)
            return None


# Shared backing store so metadata→provider recreation still finds test files
_MEMORY_REPORT_STORE: Dict[str, bytes] = {}


class InMemoryStorageProvider(StorageProvider):
    """Test / fake provider — same interface as local/S3."""

    mode = "memory"

    def __init__(self, store: Optional[Dict[str, bytes]] = None):
        self._files = store if store is not None else _MEMORY_REPORT_STORE

    def save_file(self, filename: str, content: bytes, content_type: str = "application/pdf", **kwargs) -> str:
        safe = validate_report_filename(filename)
        self._files[safe] = content
        return f"/api/v1/admin/reports/{safe}"

    def exists(self, filename: str) -> bool:
        try:
            return validate_report_filename(filename) in self._files
        except ValueError:
            return False

    def get_bytes(self, filename: str) -> bytes:
        safe = validate_report_filename(filename)
        if safe not in self._files:
            raise ReportNotFoundError(f"Report not found: {safe}")
        return self._files[safe]

    def delete(self, filename: str) -> bool:
        try:
            safe = validate_report_filename(filename)
            return self._files.pop(safe, None) is not None
        except ValueError:
            return False


def is_s3_configured() -> bool:
    return bool(
        os.getenv("HF_S3_ENDPOINT_URL")
        or os.getenv("S3_ENDPOINT_URL")
        or os.getenv("AWS_S3_BUCKET")
        or os.getenv("HF_S3_BUCKET")
        or os.getenv("AWS_ACCESS_KEY_ID")
        or os.getenv("HF_ACCESS_KEY")
    )


def is_persistent_volume_configured() -> bool:
    return bool(getattr(settings, "REPORT_PERSISTENT_VOLUME", False))


def get_report_storage_mode() -> str:
    """Return ephemeral | s3 | persistent for config-status."""
    if is_s3_configured():
        return "s3"
    if is_persistent_volume_configured():
        return "persistent"
    return "ephemeral"


def get_storage_provider() -> StorageProvider:
    """S3 is primary when configured; otherwise local disk (may be ephemeral)."""
    if is_s3_configured():
        return S3StorageProvider()
    return LocalStorageProvider()


# Default storage provider instance (resolved lazily via get_storage_provider in generate_report)
default_storage_provider = get_storage_provider()


def validate_report_filename(filename: str) -> str:
    """Reject path traversal; allow only basename with pdf/xlsx/docx/csv."""
    if not filename or not isinstance(filename, str):
        raise ValueError("Invalid report filename")
    raw = filename.strip()
    if not raw or raw in (".", ".."):
        raise ValueError("Invalid report filename")
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("Path traversal is not allowed")
    safe = os.path.basename(raw)
    if safe != raw or not safe:
        raise ValueError("Path traversal is not allowed")
    ext = os.path.splitext(safe)[1].lower()
    if ext not in ALLOWED_REPORT_EXTENSIONS:
        raise ValueError(f"Unsupported report extension: {ext}")
    if not re.match(r"^[A-Za-z0-9_\-\.]+$", safe):
        raise ValueError("Invalid report filename characters")
    return safe


def _signing_secret() -> bytes:
    secret = (
        (getattr(settings, "SECRET_KEY", None) or "").strip()
        or (getattr(settings, "INTERNAL_API_SECRET", None) or "").strip()
        or "tajlandia-report-download-dev"
    )
    return secret.encode("utf-8")


def create_signed_download_token(filename: str, ttl_sec: Optional[int] = None) -> Tuple[str, int]:
    """
    HMAC token: base64url(filename:expiry:signature). No secrets embedded in the URL path.
    Returns (token, expires_at_unix).
    """
    safe = validate_report_filename(filename)
    ttl = int(ttl_sec if ttl_sec is not None else getattr(settings, "REPORT_SIGNED_LINK_TTL_SEC", 600))
    exp = int(time.time()) + max(30, ttl)
    msg = f"{safe}:{exp}"
    sig = hmac.new(_signing_secret(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{safe}:{exp}:{sig}"
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    return token, exp


def verify_signed_download_token(token: str) -> str:
    """Validate token; return safe filename. Raises SignedLinkError on failure."""
    if not token or not isinstance(token, str):
        raise SignedLinkError("Invalid download token")
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except Exception as e:
        raise SignedLinkError("Invalid download token") from e
    parts = raw.split(":")
    if len(parts) != 3:
        raise SignedLinkError("Invalid download token")
    filename, exp_s, sig = parts
    try:
        safe = validate_report_filename(filename)
        exp = int(exp_s)
    except (ValueError, TypeError) as e:
        raise SignedLinkError("Invalid download token") from e
    msg = f"{safe}:{exp}"
    expected = hmac.new(_signing_secret(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignedLinkError("Invalid or tampered download token")
    if int(time.time()) > exp:
        raise SignedLinkError("Download link has expired", expired=True)
    return safe


def _metadata_dir() -> str:
    base = getattr(settings, "REPORT_STORAGE_DIR", "data/reports") or "data/reports"
    meta_dir = os.path.join(base, ".meta")
    os.makedirs(meta_dir, exist_ok=True)
    return meta_dir


def _metadata_path(filename: str) -> str:
    safe = validate_report_filename(filename)
    return os.path.join(_metadata_dir(), f"{safe}.json")


def save_report_metadata(
    filename: str,
    *,
    storage: str,
    created_by: Optional[str] = None,
    retention_days: Optional[int] = None,
) -> Dict[str, Any]:
    safe = validate_report_filename(filename)
    days = int(retention_days if retention_days is not None else getattr(settings, "REPORT_RETENTION_DAYS", 7))
    now = datetime.now(timezone.utc)
    record = {
        "filename": safe,
        "storage": storage,
        "created_at": now.isoformat(),
        "created_by": created_by,
        "expires_at": (now + timedelta(days=days)).isoformat(),
    }
    try:
        with open(_metadata_path(safe), "w", encoding="utf-8") as f:
            json.dump(record, f)
    except Exception as e:
        logger.warning("Could not persist report metadata for %s: %s", safe, e)
    return record


def get_report_metadata(filename: str) -> Optional[Dict[str, Any]]:
    try:
        safe = validate_report_filename(filename)
        path = _metadata_path(safe)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _delete_report_metadata(filename: str) -> None:
    try:
        path = _metadata_path(filename)
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def _provider_for_storage_mode(mode: Optional[str]) -> StorageProvider:
    if mode == "s3":
        return S3StorageProvider()
    if mode == "memory":
        return InMemoryStorageProvider()
    return LocalStorageProvider()


def report_exists(filename: str, storage: Optional[StorageProvider] = None) -> bool:
    try:
        safe = validate_report_filename(filename)
    except ValueError:
        return False
    meta = get_report_metadata(safe)
    provider = storage
    if provider is None:
        if meta and meta.get("storage"):
            provider = _provider_for_storage_mode(meta.get("storage"))
        else:
            provider = get_storage_provider()
    return bool(provider.exists(safe))


def load_report_bytes(filename: str, storage: Optional[StorageProvider] = None) -> Tuple[bytes, str, Dict[str, Any]]:
    """
    Load report bytes from the same provider that saved them.
    Returns (bytes, media_type, metadata).
    Raises ReportExpiredError / ReportNotFoundError / ValueError.
    """
    safe = validate_report_filename(filename)
    meta = get_report_metadata(safe) or {}
    expires_at = meta.get("expires_at")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                raise ReportExpiredError("This report has expired")
        except ReportExpiredError:
            raise
        except Exception:
            pass

    provider = storage
    if provider is None:
        mode = meta.get("storage")
        if mode:
            provider = _provider_for_storage_mode(mode)
        else:
            provider = get_storage_provider()

    if not provider.exists(safe):
        raise ReportNotFoundError("This report is no longer available - regenerate it")

    content = provider.get_bytes(safe)
    ext = os.path.splitext(safe)[1].lower()
    media_type = REPORT_MEDIA_TYPES.get(ext, "application/octet-stream")
    return content, media_type, meta


def purge_old_reports(retention_days: Optional[int] = None) -> int:
    """
    Delete reports (and metadata) older than retention_days. Never raises.
    """
    deleted = 0
    try:
        days = int(retention_days if retention_days is not None else getattr(settings, "REPORT_RETENTION_DAYS", 7))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        meta_dir = _metadata_dir()
        if not os.path.isdir(meta_dir):
            return 0
        for name in os.listdir(meta_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(meta_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                fname = meta.get("filename") or name[:-5]
                created = meta.get("created_at") or meta.get("expires_at")
                exp = meta.get("expires_at")
                should_delete = False
                if exp:
                    try:
                        exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        should_delete = datetime.now(timezone.utc) > exp_dt
                    except Exception:
                        should_delete = False
                if not should_delete and created:
                    try:
                        created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        should_delete = created_dt < cutoff
                    except Exception:
                        pass
                if not should_delete:
                    continue
                provider = _provider_for_storage_mode(meta.get("storage"))
                try:
                    provider.delete(fname)
                except Exception:
                    pass
                try:
                    os.remove(path)
                except Exception:
                    pass
                deleted += 1
            except Exception:
                continue
    except Exception as e:
        logger.warning("purge_old_reports error (returning %s): %s", deleted, e)
    return deleted


_ADMIN_REPORT_URL_RE = re.compile(
    r"(?:https?://[^\s)\]\"']+)?/api/v1/admin/reports/[A-Za-z0-9_\-\.]+",
    re.IGNORECASE,
)
_ADMIN_REPORT_MD_LINK_RE = re.compile(
    r"\[[^\]]*\]\(\s*(?:https?://[^)\s]+)?/api/v1/admin/reports/[^)\s]+\s*\)",
    re.IGNORECASE,
)


def strip_admin_report_urls_from_text(text: str) -> str:
    """Remove raw /api/v1/admin/reports/... links from chat text (download card is enough)."""
    if not text:
        return text
    out = _ADMIN_REPORT_MD_LINK_RE.sub("", text)
    out = _ADMIN_REPORT_URL_RE.sub("", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def format_report_ready_message(
    *,
    filename: str,
    format_type: str,
    language: str = "en",
    storage_ok: bool = True,
    storage_error: Optional[str] = None,
) -> str:
    if not storage_ok:
        if language == "pl":
            return (
                "Raport został wygenerowany, ale nie udało się zapisać go trwale. "
                "Spróbuj ponownie za chwilę."
            )
        return (
            storage_error
            or "Report generated but could not be stored permanently. Please try again shortly."
        )
    if language == "pl":
        return (
            f"✅ Wygenerowano raport {format_type.upper()} **{filename}**. "
            "Użyj karty pobierania poniżej."
        )
    return (
        f"✅ Generated {format_type.upper()} report **{filename}**. "
        "Use the download card below."
    )


# 2. Report Builders
def _build_excel_report(properties_data: List[Dict[str, Any]], title: str) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Property Report"

    # Header styling
    ws.append([title])
    ws.merge_cells("A1:H1")
    title_cell = ws["A1"]
    title_cell.font = Font(size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")

    headers = [
        "Property ID", "Property Name", "Location", "Price Display", 
        "Bedrooms", "Bathrooms", "Status", "Developer"
    ]
    ws.append(headers)
    for col_num, _ in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_num)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")

    for p in properties_data:
        ws.append([
            p.get("property_id", ""),
            p.get("property_name", ""),
            p.get("location", ""),
            p.get("price_display", ""),
            p.get("bedrooms", ""),
            p.get("bathrooms", ""),
            p.get("status", ""),
            p.get("developer_name", "")
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_pdf_report(properties_data: List[Dict[str, Any]], title: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    # Title
    story.append(Paragraph(f"<b>{title}</b>", styles["Title"]))
    story.append(Spacer(1, 12))

    # Metadata
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Generated: {date_str} | Total Properties: {len(properties_data)}", styles["Normal"]))
    story.append(Spacer(1, 16))

    # Table data
    table_data = [["ID", "Property Name", "Location", "Price", "Status"]]
    for p in properties_data:
        table_data.append([
            p.get("property_id", ""),
            p.get("property_name", "")[:28],
            p.get("location", "")[:20],
            p.get("price_display", "")[:20],
            p.get("status", "")[:18]
        ])

    table = Table(table_data, colWidths=[80, 160, 110, 100, 90])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
    ]))
    story.append(table)

    doc.build(story)
    return buf.getvalue()


def _build_docx_report(properties_data: List[Dict[str, Any]], title: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    doc.add_paragraph(f"Total Properties Listed: {len(properties_data)}")

    table = doc.add_table(rows=1, cols=5)
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = "Property ID"
    hdr_cells[1].text = "Name"
    hdr_cells[2].text = "Location"
    hdr_cells[3].text = "Price"
    hdr_cells[4].text = "Status"

    for p in properties_data:
        row_cells = table.add_row().cells
        row_cells[0].text = str(p.get("property_id", ""))
        row_cells[1].text = str(p.get("property_name", ""))
        row_cells[2].text = str(p.get("location", ""))
        row_cells[3].text = str(p.get("price_display", ""))
        row_cells[4].text = str(p.get("status", ""))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_csv_report(properties_data: List[Dict[str, Any]]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["property_id", "property_name", "location", "price_display", "bedrooms", "bathrooms", "status", "developer_name"])
    for p in properties_data:
        writer.writerow([
            p.get("property_id", ""),
            p.get("property_name", ""),
            p.get("location", ""),
            p.get("price_display", ""),
            p.get("bedrooms", ""),
            p.get("bathrooms", ""),
            p.get("status", ""),
            p.get("developer_name", "")
        ])
    return output.getvalue().encode("utf-8")


LEADS_REPORT_CAP = 200
ALL_TIME_LEADS_SOFT_CAP_DAYS = 180


def is_possible_test_lead(
    email: Optional[str] = None,
    name: Optional[str] = None,
    notes: Optional[str] = None,
) -> bool:
    """Flag placeholder/test entries — do not drop them or call them 'demo' alone."""
    blob = " ".join(
        str(x).lower() for x in (email or "", name or "", notes or "") if x
    )
    if not blob.strip():
        return False
    markers = (
        "example.com",
        "test@",
        "@test.",
        "qa@",
        " qa ",
        "qa test",
        "test user",
        "test lead",
        "dummy",
        "fake@",
        "noreply@",
    )
    name_l = (name or "").strip().lower()
    if name_l in ("test", "qa", "dummy", "sample"):
        return True
    return any(m in blob for m in markers)


def serialize_lead_row(l: Any) -> Dict[str, Any]:
    """Normalize a Lead ORM row (or duck-typed object) for chat/report payloads."""
    notes_val = "Web chat inquiry"
    signals = getattr(l, "buying_signals", None) or []
    if isinstance(signals, list):
        for sig in signals:
            if isinstance(sig, str) and sig.startswith("Note: "):
                notes_val = sig.replace("Note: ", "")
                break
        else:
            if signals and isinstance(signals[0], str):
                notes_val = signals[0]

    budget_s = "Not specified"
    if getattr(l, "budget_max", None) is not None:
        budget_s = f"{float(l.budget_max):,} THB"
    elif getattr(l, "budget_min", None) is not None:
        budget_s = f"from {float(l.budget_min):,} THB"
    else:
        if any(isinstance(s, str) and "flexible" in s.lower() for s in signals):
            budget_s = "Flexible"

    created_s = ""
    if getattr(l, "created_at", None):
        try:
            created_s = l.created_at.strftime("%Y-%m-%d")
        except Exception:
            created_s = str(l.created_at)[:10]

    email = getattr(l, "contact_email", None) or "Not specified"
    name = getattr(l, "contact_name", None) or "Inquiring Visitor"
    test_flag = is_possible_test_lead(email=email, name=name, notes=notes_val)
    row = {
        "lead_id": str(getattr(l, "id", "")),
        "contact_name": name,
        "contact_email": email,
        "contact_phone": getattr(l, "contact_phone", None) or "Not specified",
        "preferred_location": getattr(l, "preferred_location", None) or "Not specified",
        "budget": budget_s,
        "lead_score": getattr(l, "lead_score", None) or 0.0,
        "status": getattr(l, "status", None) or "new",
        "notes": notes_val,
        "buying_signals": signals if isinstance(signals, list) else [],
        "created_at": created_s or "Not specified",
        "possible_test_entry": test_flag,
    }
    if test_flag:
        row["test_entry_tag"] = "possible test entry"
    return row


# 2b. Lead Report Builders
def _build_lead_pdf_report(leads_data: List[Dict[str, Any]], title: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'LeadTitle',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0F172A")
    )
    meta_style = ParagraphStyle(
        'LeadMeta',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#64748B")
    )
    cell_bold = ParagraphStyle(
        'LeadCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.white
    )
    cell_regular = ParagraphStyle(
        'LeadCellReg',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#334155")
    )

    story.append(Paragraph(f"<b>{title}</b>", title_style))
    story.append(Spacer(1, 6))

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    period_note = ""
    if leads_data and leads_data[0].get("_period_subtitle"):
        period_note = f" | {leads_data[0]['_period_subtitle']}"
    story.append(Paragraph(
        f"Generated: {date_str} | Buyer Leads: {len(leads_data)}{period_note} | Confidential Agency Internal",
        meta_style,
    ))
    story.append(Spacer(1, 14))

    for idx, lead in enumerate(leads_data):
        score_val = lead.get('lead_score', 'N/A')
        status_val = str(lead.get('status', 'new')).upper()
        test_tag = " &nbsp;|&nbsp; [possible test entry]" if lead.get("possible_test_entry") else ""
        header_text = (
            f"LEAD #{idx+1}: {lead.get('contact_name', 'Client')} &nbsp;|&nbsp; "
            f"Status: {status_val} &nbsp;|&nbsp; Score: {score_val}/100{test_tag}"
        )

        table_rows = [
            [Paragraph(f"<b>{header_text}</b>", cell_bold), ""],
            [Paragraph(f"<b>Email:</b> {lead.get('contact_email', 'N/A')}", cell_regular),
             Paragraph(f"<b>Phone:</b> {lead.get('contact_phone', 'N/A')}", cell_regular)],
            [Paragraph(f"<b>Preferred Location:</b> {lead.get('preferred_location', 'Thailand')}", cell_regular),
             Paragraph(f"<b>Target Budget:</b> {lead.get('budget', 'Flexible')}", cell_regular)],
            [Paragraph(f"<b>Client Notes:</b> {lead.get('notes', 'Web inquiry')}", cell_regular), ""]
        ]
        signals = lead.get('buying_signals', [])
        if signals:
            sig_text = " • ".join(str(s) for s in signals)
            table_rows.append([Paragraph(f"<b>Buying Signals:</b> {sig_text}", cell_regular), ""])

        t = Table(table_rows, colWidths=[270, 270])
        t_style = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('SPAN', (0, 0), (1, 0)),
            ('SPAN', (0, 3), (1, 3)),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]
        if signals:
            t_style.append(('SPAN', (0, 4), (1, 4)))
        t.setStyle(TableStyle(t_style))
        story.append(t)
        story.append(Spacer(1, 14))

    doc.build(story)
    return buf.getvalue()


def _build_lead_excel_report(leads_data: List[Dict[str, Any]], title: str) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Buyer Leads"

    ws.append([title])
    ws.merge_cells("A1:H1")
    title_cell = ws["A1"]
    title_cell.font = Font(size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")

    headers = [
        "Lead ID", "Contact Name", "Email", "Phone", "Location", "Budget",
        "Lead Score", "Status", "Notes", "Possible Test Entry",
    ]
    ws.append(headers)
    for col_num, _ in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_num)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")

    for l in leads_data:
        ws.append([
            l.get("lead_id", ""),
            l.get("contact_name", ""),
            l.get("contact_email", ""),
            l.get("contact_phone", ""),
            l.get("preferred_location", ""),
            l.get("budget", ""),
            l.get("lead_score", ""),
            l.get("status", ""),
            l.get("notes", ""),
            "possible test entry" if l.get("possible_test_entry") else "",
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_lead_docx_report(leads_data: List[Dict[str, Any]], title: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    doc.add_paragraph(f"Total Qualified Buyer Leads: {len(leads_data)}")

    table = doc.add_table(rows=1, cols=5)
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = "Lead ID"
    hdr_cells[1].text = "Contact Name"
    hdr_cells[2].text = "Location / Budget"
    hdr_cells[3].text = "Score / Status"
    hdr_cells[4].text = "Notes"

    for l in leads_data:
        row_cells = table.add_row().cells
        row_cells[0].text = str(l.get("lead_id", ""))
        row_cells[1].text = f"{l.get('contact_name', '')} ({l.get('contact_email', '')})"
        row_cells[2].text = f"{l.get('preferred_location', '')} / {l.get('budget', '')}"
        row_cells[3].text = f"{l.get('lead_score', '')} pts ({l.get('status', '')})"
        row_cells[4].text = str(l.get("notes", ""))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_lead_csv_report(leads_data: List[Dict[str, Any]]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "lead_id", "contact_name", "email", "phone", "preferred_location",
        "budget", "lead_score", "status", "notes", "possible_test_entry",
    ])
    for l in leads_data:
        writer.writerow([
            l.get("lead_id", ""),
            l.get("contact_name", ""),
            l.get("contact_email", ""),
            l.get("contact_phone", ""),
            l.get("preferred_location", ""),
            l.get("budget", ""),
            l.get("lead_score", ""),
            l.get("status", ""),
            l.get("notes", ""),
            "possible test entry" if l.get("possible_test_entry") else "",
        ])
    return output.getvalue().encode("utf-8")


def _finalize_report_save(
    storage: StorageProvider,
    filename: str,
    content_bytes: bytes,
    content_type: str,
    *,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persist bytes, write metadata, verify existence. Never silently falls back
    when S3 is configured.
    """
    storage_mode = getattr(storage, "mode", None) or (
        "s3" if isinstance(storage, S3StorageProvider) else "local"
    )
    if storage_mode == "local":
        storage_mode = "s3" if is_s3_configured() and isinstance(storage, S3StorageProvider) else (
            "memory" if isinstance(storage, InMemoryStorageProvider) else "local"
        )

    try:
        file_url = storage.save_file(filename, content_bytes, content_type=content_type)
    except StorageError as e:
        return {
            "filename": filename,
            "file_url": None,
            "storage_ok": False,
            "storage": storage_mode,
            "message": str(e) or "Report generated but could not be stored permanently",
        }

    meta_storage = "s3" if storage_mode == "s3" else (
        "memory" if storage_mode == "memory" else "local"
    )
    save_report_metadata(filename, storage=meta_storage, created_by=created_by)

    if not storage.exists(filename):
        logger.error("Report %s saved but verification failed on provider %s", filename, meta_storage)
        return {
            "filename": filename,
            "file_url": None,
            "storage_ok": False,
            "storage": meta_storage,
            "message": "Report generated but could not be stored permanently",
        }

    # Always prefer authenticated admin path for the download card
    admin_url = f"/api/v1/admin/reports/{filename}"
    if file_url and file_url.startswith("/api/v1/chat/"):
        admin_url = file_url

    return {
        "filename": filename,
        "file_url": admin_url,
        "storage_ok": True,
        "storage": meta_storage,
        "message": format_report_ready_message(
            filename=filename, format_type=os.path.splitext(filename)[1].lstrip(".") or "pdf"
        ),
    }


# 3. Main Generation Entrypoint
def generate_report(
    db: Session,
    format_type: str,
    property_ids: Optional[List[str]] = None,
    lead_ids: Optional[List[str]] = None,
    report_type: Optional[str] = None,
    title: Optional[str] = None,
    storage: Optional[StorageProvider] = None,
    websites: Optional[List[str]] = None,
    created_by: Optional[str] = None,
    days: Optional[int] = None,
    period_label: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Builds property intelligence or buyer lead intelligence reports in PDF, XLSX, DOCX, or CSV.
    Stores file via abstract StorageProvider and returns download URL.

    Leads: no lead_ids and no period → all stored leads (cap LEADS_REPORT_CAP).
    days=0/None with no lead_ids → all-time. Empty period → offer wider window, no empty PDF.
    """
    storage = storage or get_storage_provider()
    fmt = format_type.strip().lower()
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    is_lead_report = (
        report_type == "leads"
        or (lead_ids is not None and len(lead_ids) > 0)
        or (title and "lead" in title.lower())
    )

    if is_lead_report:
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        all_time = days is None or int(days) <= 0
        period_days = None if all_time else int(days)
        period_subtitle = period_label or (
            "all stored leads (newest first)"
            if all_time
            else f"last {period_days} days through {now.strftime('%Y-%m-%d')}"
        )

        query = db.query(Lead)
        leads: List[Any] = []
        capped = False
        if lead_ids:
            clean_lids = [str(lid).strip() for lid in lead_ids if lid]
            leads = query.filter(
                (Lead.contact_name.in_(clean_lids))
                | (Lead.id.in_(clean_lids))
            ).order_by(Lead.created_at.desc()).all()
            period_subtitle = f"selected leads ({len(leads)})"
        else:
            if not all_time and period_days:
                window_start = now - timedelta(days=period_days)
                try:
                    query = query.filter(Lead.created_at >= window_start.replace(tzinfo=None))
                except Exception:
                    query = query.filter(Lead.created_at >= window_start)
            total_matching = query.count()
            leads = (
                query.order_by(Lead.created_at.desc(), Lead.lead_score.desc())
                .limit(LEADS_REPORT_CAP)
                .all()
            )
            capped = total_matching > LEADS_REPORT_CAP
            if capped:
                period_subtitle = f"{period_subtitle}; showing newest {LEADS_REPORT_CAP} of {total_matching}"

            # Empty for a requested period → offer wider window, do not emit empty PDF
            if not leads and not all_time and period_days:
                wider = 30 if period_days < 30 else (90 if period_days < 90 else 180)
                w_start = now - timedelta(days=wider)
                try:
                    wider_q = db.query(Lead).filter(
                        Lead.created_at >= w_start.replace(tzinfo=None)
                    )
                except Exception:
                    wider_q = db.query(Lead).filter(Lead.created_at >= w_start)
                wider_count = wider_q.count()
                most_recent = db.query(Lead).order_by(Lead.created_at.desc()).first()
                recent_d = None
                if most_recent and getattr(most_recent, "created_at", None):
                    try:
                        recent_d = most_recent.created_at.strftime("%Y-%m-%d")
                    except Exception:
                        recent_d = str(most_recent.created_at)[:10]
                offer_msg = (
                    f"No leads in the last {period_days} days"
                    + (f". The most recent lead was on {recent_d}" if recent_d else "")
                    + (
                        f". Last {wider} days: {wider_count} leads. Want the last {wider} days?"
                        if wider_count
                        else ". No leads found in a wider window either."
                    )
                )
                return {
                    "format": fmt,
                    "filename": None,
                    "file_url": None,
                    "storage_ok": False,
                    "empty_period": True,
                    "total_leads": 0,
                    "period_days": period_days,
                    "period_label": period_subtitle,
                    "wider_offer": {"days": wider, "count": wider_count},
                    "most_recent_lead_date": recent_d,
                    "message": offer_msg,
                    "generated_at": now.isoformat(),
                }

        report_title = title or f"Tajlandia AI - Buyer Lead Intelligence ({period_subtitle})"
        leads_data = []
        for l in leads:
            row = serialize_lead_row(l)
            row["_period_subtitle"] = period_subtitle
            leads_data.append(row)

        if not leads_data and lead_ids:
            return {
                "format": fmt,
                "filename": None,
                "file_url": None,
                "storage_ok": False,
                "empty_period": True,
                "total_leads": 0,
                "message": "No matching leads found for the requested names/ids.",
                "generated_at": now.isoformat(),
            }

        filename = f"tajlandia_lead_report_{timestamp_str}.{fmt}"
        if fmt == "xlsx":
            content_bytes = _build_lead_excel_report(leads_data, report_title)
            ctype = REPORT_MEDIA_TYPES[".xlsx"]
        elif fmt == "pdf":
            content_bytes = _build_lead_pdf_report(leads_data, report_title)
            ctype = REPORT_MEDIA_TYPES[".pdf"]
        elif fmt == "docx":
            content_bytes = _build_lead_docx_report(leads_data, report_title)
            ctype = REPORT_MEDIA_TYPES[".docx"]
        elif fmt == "csv":
            content_bytes = _build_lead_csv_report(leads_data)
            ctype = REPORT_MEDIA_TYPES[".csv"]
        else:
            raise ValueError(f"Unsupported report format: {fmt}. Supported formats: pdf, xlsx, docx, csv.")

        saved = _finalize_report_save(
            storage, filename, content_bytes, ctype, created_by=created_by
        )
        note = ""
        if capped:
            note = f" (capped at {LEADS_REPORT_CAP} newest leads)"
        ready_msg = saved.get("message") or format_report_ready_message(
            filename=filename, format_type=fmt, storage_ok=bool(saved.get("storage_ok"))
        )
        if saved.get("storage_ok"):
            ready_msg = (
                f"✅ Generated {fmt.upper()} leads report ({len(leads_data)} leads — {period_subtitle})"
                f"{note}. Use the download card below."
            )
        return {
            "format": fmt,
            "filename": filename,
            "file_url": saved.get("file_url"),
            "storage_ok": bool(saved.get("storage_ok")),
            "storage": saved.get("storage"),
            "message": ready_msg,
            "title": report_title,
            "total_leads": len(leads_data),
            "period_label": period_subtitle,
            "capped": capped,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    # Property Report Generation
    report_title = title or "Tajlandia AI Property Intelligence Inventory Report"
    from app.services.property_search import build_property_query, PropertyFilterParams

    # build_property_query expects a Session (not an existing Query)
    params = PropertyFilterParams(websites=websites or None)
    query = build_property_query(db, params)
    if property_ids:
        clean_ids = [str(pid).strip() for pid in property_ids if pid]
        # Match both exact IDs and case variants used in Firestore/SQL
        upper_ids = [c.upper() for c in clean_ids]
        query = query.filter(
            or_(
                Property.property_id.in_(clean_ids),
                Property.property_id.in_(upper_ids),
            )
        )
    properties = query.all()

    properties_data = []
    for prop in properties:
        loc = f"{prop.district or ''}, {prop.province or ''}".strip(", ")
        properties_data.append({
            "property_id": prop.property_id,
            "property_name": prop.property_name,
            "location": loc or "Thailand",
            "price_display": prop.price_display or "Price on Request",
            "bedrooms": prop.bedrooms_display or str(prop.bedrooms_min or "N/A"),
            "bathrooms": prop.bathrooms or "N/A",
            "status": prop.status or "Available",
            "developer_name": prop.developer_name or "Tajlandia Partner"
        })

    filename = f"tajlandia_report_{timestamp_str}.{fmt}"
    if fmt == "xlsx":
        content_bytes = _build_excel_report(properties_data, report_title)
        ctype = REPORT_MEDIA_TYPES[".xlsx"]
    elif fmt == "pdf":
        content_bytes = _build_pdf_report(properties_data, report_title)
        ctype = REPORT_MEDIA_TYPES[".pdf"]
    elif fmt == "docx":
        content_bytes = _build_docx_report(properties_data, report_title)
        ctype = REPORT_MEDIA_TYPES[".docx"]
    elif fmt == "csv":
        content_bytes = _build_csv_report(properties_data)
        ctype = REPORT_MEDIA_TYPES[".csv"]
    else:
        raise ValueError(f"Unsupported report format: {fmt}. Supported formats: pdf, xlsx, docx, csv.")

    saved = _finalize_report_save(
        storage, filename, content_bytes, ctype, created_by=created_by
    )

    return {
        "format": fmt,
        "filename": filename,
        "file_url": saved.get("file_url"),
        "storage_ok": bool(saved.get("storage_ok")),
        "storage": saved.get("storage"),
        "message": saved.get("message") or format_report_ready_message(
            filename=filename, format_type=fmt, storage_ok=bool(saved.get("storage_ok"))
        ),
        "title": report_title,
        "total_properties": len(properties_data),
        "generated_at": datetime.now(timezone.utc).isoformat()
    }
