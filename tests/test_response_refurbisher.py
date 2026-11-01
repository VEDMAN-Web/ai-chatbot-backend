import pytest
from app.services.response_refurbisher import refurbish_response


def test_refurbish_drive_ingestion_started_en():
    raw_text = (
        "**Google Drive folder validated:** test\n"
        "- Files discovered: **4**\n"
        "- By format: PDF: 4\n"
        "- Batch size: 10 → 1 batch(es)\n"
        "- OCR for scanned PDFs/images: on\n\n"
        "Processing job `dij_6a6a99a330de41ac` has started. Nothing will be written to the database — every "
        "extracted property becomes a draft for your review. Ask **import status** for progress, "
        "**show import drafts** to review, then **approve import** when ready."
    )
    refurbished = refurbish_response(raw_text, role="admin", language="en")
    
    # Assert human-friendly and safe
    assert "**Google Drive folder validated:** test" in refurbished
    assert "Files discovered: **4**" in refurbished
    assert "Safety Guarantee:" in refurbished
    assert "Nothing will be written to your live database yet" in refurbished
    assert "What would you like to do next?" in refurbished
    assert "import status" in refurbished
    assert "show import drafts" in refurbished
    assert "approve import" in refurbished
    # Ensure raw developer logs are not dominating
    assert "OCR for scanned PDFs/images: on" not in refurbished
    assert "10 → 1 batch(es)" not in refurbished


def test_refurbish_drive_ingestion_started_pl():
    raw_text = (
        "**Google Drive folder validated:** Projekty Tajlandia\n"
        "- Files discovered: **12**\n"
        "- By format: PDF: 10, DOCX: 2\n"
        "- Batch size: 10 → 2 batch(es)\n"
        "- OCR for scanned PDFs/images: on\n\n"
        "Processing job `dij_123` has started."
    )
    refurbished = refurbish_response(raw_text, role="admin", language="pl")
    
    assert "**Google Drive folder validated:** Projekty Tajlandia" in refurbished
    assert "Files discovered: **12**" in refurbished
    assert "Gwarancja bezpieczeństwa:" in refurbished
    assert "szkic (draft)" in refurbished
    assert "import status" in refurbished
    assert "show import drafts" in refurbished
    assert "approve import" in refurbished


def test_refurbish_technical_database_errors():
    raw_err = "psycopg2.OperationalError: connection to server at '127.0.0.1' failed: Connection refused"
    refurbished_en = refurbish_response(raw_err, role="admin", language="en")
    assert "psycopg2.OperationalError" not in refurbished_en
    assert "temporary system issue" in refurbished_en

    refurbished_pl = refurbish_response(raw_err, role="admin", language="pl")
    assert "psycopg2.OperationalError" not in refurbished_pl
    assert "chwilowy problem techniczny" in refurbished_pl


def test_refurbish_machine_artifacts():
    raw_contact = "[Contact Form] John Doe john@example.com +48123456789"
    cleaned = refurbish_response(raw_contact, role="user", language="en")
    assert "[Contact Form]" not in cleaned
    assert "John Doe" in cleaned

