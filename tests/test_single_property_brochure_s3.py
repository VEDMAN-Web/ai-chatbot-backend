import io
import pypdf
import pytest
from app.db.session import SessionLocal
from app.db.models import Property
from app.services.brochure_service import generate_single_property_brochure_pdf, generate_and_upload_brochure
from app.services.report_service import S3StorageProvider
from app.services.public_agent import public_agent
from app.services.conversation_service import get_or_create_conversation


def test_s3_storage_provider_defaults():
    provider = S3StorageProvider()
    assert "s3.hf.co/itisha249" in provider.endpoint_url
    assert provider.bucket_name == "tajlandia"
    assert provider.region_name == "us-east-1"
    assert provider.profile_name == "hf"


def test_generate_single_property_brochure_pdf():
    db = SessionLocal()
    try:
        prop = db.query(Property).first()
        if not prop:
            pytest.skip("No property in database to generate brochure for.")

        res = generate_single_property_brochure_pdf(
            db=db,
            property_id=prop.property_id,
            site_origin="tajlandia.com",
            language="en"
        )
        assert res["success"] is True
        assert res["pages"] == 8
        assert res["property_id"] == prop.property_id
        assert len(res["pdf_bytes"]) > 10000

        reader = pypdf.PdfReader(io.BytesIO(res["pdf_bytes"]))
        assert len(reader.pages) == 8
        assert res.get("file_url") is not None
    finally:
        db.close()


def test_public_chat_brochure_returns_link_not_direct_download():
    db = SessionLocal()
    try:
        prop = db.query(Property).first()
        if not prop:
            pytest.skip("No property in database to test public chat brochure.")

        conv = get_or_create_conversation(db, session_id="test_chat_brochure_link_001", site_origin="tajlandia.com")
        resp = public_agent.run_conversation(
            db=db,
            conversation=conv,
            user_message=f"Download brochure for {prop.property_name}",
            requested_language="en"
        )

        assert resp["type"] == "brochure"
        assert resp["lead_captured"] is True
        assert resp.get("pdf_url") is not None
        assert resp.get("download_url") is not None
        # Must contain clickable markdown link to the PDF with simple hyphen
        assert "[Download PDF]" in resp["text"]
        assert " - " in resp["text"]
    finally:
        db.close()
