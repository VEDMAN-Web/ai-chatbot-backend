"""
Role permissions on chat-driven property changes, and what happens to Drive files that cannot be read.

The REST API already refused workers; these tests pin the same rule on the admin chat tools (when a change is
proposed and again when it is confirmed) and on the Drive ingest path (missing, scanned, empty, corrupt files).
"""
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core import permissions
from app.core.auth import AuthenticatedStaff
from app.db.models import PendingAction
from app.db.session import SessionLocal
from app.services import drive_service
from app.services.action_service import confirm_and_execute_action
from app.services.admin_agent import _execute_admin_tool_uncached

WRITE_TOOLS = sorted(permissions.TOOL_PERMISSIONS)


def _staff(role: str, email: str = None) -> AuthenticatedStaff:
    return AuthenticatedStaff(
        user_id=str(uuid.uuid4()),
        email=email or f"{role}-{uuid.uuid4().hex[:6]}@tajlandia.com",
        role=role,
        allowed_websites=["tajlandia.com"],
        selected_websites=["tajlandia.com"],
    )


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# --- the matrix itself -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.parametrize("role", ["worker", "employee", "public", "", None, "owner", 12345, object()])
def test_tool_denied_for_non_admin_roles(role, tool):
    assert permissions.tool_denial(role, tool)


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.parametrize("role", ["admin", "super_admin", " Admin "])
def test_tool_allowed_for_admin_roles(role, tool):
    assert permissions.tool_denial(role, tool) is None


@pytest.mark.parametrize("tool", ["search_properties_full", "generate_report", "get_buyer_leads", "fetch_drive_file", "send_email"])
def test_read_and_report_tools_stay_open_to_workers(tool):
    assert permissions.tool_denial("worker", tool) is None


def test_role_cannot_be_raised_by_tool_arguments(db):
    """A worker who passes 'role': 'admin' in the tool arguments is still a worker."""
    result, needs_confirmation, pending_id = _execute_admin_tool_uncached(
        db, "create_or_update_listing",
        {"property_id": "X-1", "role": "super_admin", "staff_role": "admin", "fields": {"price_thb": 1}},
        _staff("worker"), str(uuid.uuid4()), "w@tajlandia.com",
    )
    assert result["status"] == "FORBIDDEN"
    assert needs_confirmation is False and pending_id is None


# --- proposing through the chat tools ------------------------------------------------------------------------------

@pytest.mark.parametrize("tool", [
    "create_or_update_listing", "ingest_property_from_drive", "ingest_property_from_url",
    "execute_batch_property_action", "undo_batch_property_operation",
])
def test_worker_cannot_propose_property_changes_in_chat(db, tool):
    staff = _staff("worker")
    before = db.query(PendingAction).filter(PendingAction.staff_user_id == staff.email).count()
    result, needs_confirmation, pending_id = _execute_admin_tool_uncached(
        db, tool,
        {"property_id": "X-1", "file_id": "anything", "url": "https://example.com/p",
         "operation_type": "delete", "property_ids": ["X-1"], "operation_id": "op"},
        staff, str(uuid.uuid4()), staff.email,
    )
    assert result["status"] == "FORBIDDEN"
    assert needs_confirmation is False and pending_id is None
    assert db.query(PendingAction).filter(PendingAction.staff_user_id == staff.email).count() == before


# --- confirming ---------------------------------------------------------------------------------------------------

def test_worker_cannot_confirm_a_property_change_even_for_their_own_pending_action(db):
    """A pending action can exist from before a role change; confirming re-checks the role."""
    staff = _staff("worker")
    action = PendingAction(
        confirmation_token=f"act_{uuid.uuid4().hex}",
        staff_user_id=staff.email,
        tool_name="create_or_update_listing",
        action_payload={"property_id": "X-1", "fields": {"price_thb": 1}},
        status="PENDING",
        conversation_id=str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    db.add(action)
    db.commit()
    try:
        with pytest.raises(HTTPException) as exc:
            confirm_and_execute_action(db, str(action.id), staff)
        assert exc.value.status_code == 403
        assert "not allowed to change property data" in exc.value.detail
        db.refresh(action)
        assert action.status == "PENDING"   # nothing ran
    finally:
        db.delete(action)
        db.commit()


# --- Drive: what a file can turn into ------------------------------------------------------------------------------

def _pdf(pages):
    """A PDF with one entry per page: text, or None for a page that is only a drawn rectangle (like a scan)."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for text in pages:
        if text is None:
            c.rect(50, 50, 400, 600, fill=1)
        else:
            c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_with_text_is_ok():
    res = drive_service.inspect_pdf_bytes(_pdf(["Villa Aurora, 3 bedrooms, 12,000,000 THB"]))
    assert res["status"] == drive_service.EXTRACT_OK
    assert "Villa Aurora" in res["text"]


def test_image_only_pdf_needs_ocr():
    res = drive_service.inspect_pdf_bytes(_pdf([None, None]))
    assert res["status"] == drive_service.EXTRACT_NEEDS_OCR
    assert res["text"] == "" and res["pages"] == 2 and res["text_pages"] == 0


def test_mixed_pdf_is_partial_and_keeps_the_readable_text():
    res = drive_service.inspect_pdf_bytes(_pdf(["Condo Lotus, 2 bedrooms", None]))
    assert res["status"] == drive_service.EXTRACT_PARTIAL
    assert "Condo Lotus" in res["text"] and res["text_pages"] == 1


def test_corrupt_pdf_is_an_error_not_text():
    res = drive_service.inspect_pdf_bytes(b"%PDF-1.4 this is not a real pdf")
    assert res["status"] == drive_service.EXTRACT_ERROR
    assert res["text"] == ""


def test_encrypted_pdf_is_an_error():
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(_pdf(["secret listing"])))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("pw")
    buf = io.BytesIO()
    writer.write(buf)
    res = drive_service.inspect_pdf_bytes(buf.getvalue())
    assert res["status"] == drive_service.EXTRACT_ERROR


def test_legacy_text_extractor_is_unchanged_for_good_and_bad_pdfs():
    assert "Hello" in drive_service.extract_text_from_pdf_bytes(_pdf(["Hello listing"]))
    assert drive_service.extract_text_from_pdf_bytes(b"junk").startswith("[PDF parsing error")


@pytest.fixture
def drive_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_service, "DRIVE_DOCS_DIR", str(tmp_path))
    monkeypatch.setattr(drive_service, "fetch_from_google_drive_api", lambda *a, **k: None)
    return tmp_path


def test_fetch_scanned_pdf_reports_ocr_required(drive_dir):
    (drive_dir / "scan.pdf").write_bytes(_pdf([None]))
    res = drive_service.fetch_drive_file("scan.pdf")
    assert res["extraction_status"] == drive_service.EXTRACT_NEEDS_OCR
    assert res["usable"] is False
    assert "OCR is required" in res["content"]


def test_fetch_empty_text_file(drive_dir):
    (drive_dir / "blank.txt").write_text("   \n")
    res = drive_service.fetch_drive_file("blank.txt")
    assert res["extraction_status"] == drive_service.EXTRACT_EMPTY and res["usable"] is False


def test_fetch_missing_file(drive_dir):
    res = drive_service.fetch_drive_file("does-not-exist-12345")
    assert res["extraction_status"] == drive_service.EXTRACT_NOT_FOUND and res["usable"] is False


def test_fetch_readable_file_is_usable_and_isolated(drive_dir):
    (drive_dir / "listing.txt").write_text("Villa Aurora, 3 bedrooms, 12,000,000 THB")
    res = drive_service.fetch_drive_file("listing.txt")
    assert res["extraction_status"] == drive_service.EXTRACT_OK and res["usable"] is True
    assert res["is_untrusted"] is True and "<UNTRUSTED_DOCUMENT_DATA" in res["isolated_block"]


def test_fetch_corrupt_pdf_is_not_usable(drive_dir):
    (drive_dir / "broken.pdf").write_bytes(b"%PDF-1.4 broken")
    res = drive_service.fetch_drive_file("broken.pdf")
    assert res["extraction_status"] == drive_service.EXTRACT_ERROR and res["usable"] is False


def test_oversized_api_file_is_not_usable(monkeypatch):
    monkeypatch.setattr(
        drive_service, "fetch_from_google_drive_api",
        lambda *a, **k: {"file_id": "f", "file_name": "huge.pdf", "content": "", "error": "too_large"},
    )
    res = drive_service.fetch_drive_file("f")
    assert res["extraction_status"] == drive_service.EXTRACT_TOO_LARGE and res["usable"] is False


@pytest.mark.parametrize("make", [
    lambda d: (d / "scan.pdf").write_bytes(_pdf([None])),
    lambda d: (d / "scan.pdf").write_bytes(b"%PDF-1.4 broken"),
    lambda d: (d / "scan.pdf").write_text(""),
    lambda d: None,   # no such file
])
def test_ingest_refuses_unreadable_drive_files_and_stages_nothing(db, drive_dir, make):
    make(drive_dir)
    staff = _staff("admin")
    before = db.query(PendingAction).filter(PendingAction.staff_user_id == staff.email).count()
    result, needs_confirmation, pending_id = _execute_admin_tool_uncached(
        db, "ingest_property_from_drive", {"file_id": "scan.pdf"}, staff, str(uuid.uuid4()), staff.email,
    )
    assert result["status"] == "DRIVE_FILE_UNUSABLE"
    assert needs_confirmation is False and pending_id is None
    assert db.query(PendingAction).filter(PendingAction.staff_user_id == staff.email).count() == before
