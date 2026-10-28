"""
Brochure PDF generation + public chat brochure link.
Uses mocks when DB/S3 are unavailable so CI is not infra-blocked.
"""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.brochure_service import generate_single_property_brochure_pdf
from app.services.public_agent import PublicAgentService
from app.services.report_service import S3StorageProvider


def test_s3_storage_provider_defaults():
    provider = S3StorageProvider()
    assert "s3.hf.co/itisha249" in provider.endpoint_url
    assert provider.bucket_name == "tajlandia"
    assert provider.region_name == "us-east-1"
    assert provider.profile_name == "hf"


def test_generate_single_property_brochure_pdf():
    """Live DB path when available; otherwise skip (infra)."""
    try:
        from app.db.session import SessionLocal
        from app.db.models import Property
    except Exception:
        pytest.skip("DB session unavailable")

    db = SessionLocal()
    try:
        try:
            prop = db.query(Property).first()
        except Exception:
            pytest.skip("SQL properties table unavailable (Firestore-only store)")
        if not prop:
            pytest.skip("No property in database to generate brochure for.")

        try:
            res = generate_single_property_brochure_pdf(
                db=db,
                property_id=prop.property_id,
                site_origin="tajlandia.com",
                language="en",
            )
        except ValueError:
            pytest.skip("Leftover SQL row not present in the Firestore property index (infra)")
        assert res["success"] is True
        assert res["pages"] == 8
        assert res["property_id"] == prop.property_id
        assert len(res["pdf_bytes"]) > 10000

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(res["pdf_bytes"]))
        assert len(reader.pages) == 8
        assert res.get("file_url") is not None
    finally:
        db.close()


def test_public_chat_brochure_returns_link_not_direct_download(monkeypatch):
    """Public path: brochure type with Download PDF link; never claims email was sent."""
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None

    prop = SimpleNamespace(
        property_id="isla-kata-sea-view-villa",
        property_name="Isla Kata Sea View Villa",
        district="Kata",
        province="Phuket",
        property_type="Villa",
        price_display="18M THB",
        price_numeric_min=18_000_000,
        bedrooms_display="3",
        bedrooms_min=3,
        area_display="270",
        images=[],
        detail_page_url="https://tajlandia.com/p/isla-kata",
        address="",
        nearby_places=None,
        status="Available",
        has_pool=True,
        beach_access=True,
        highlights=["Pool"],
        amenities=[],
        description="",
        ownership_type=None,
        latitude=7.8,
        longitude=98.3,
    )
    conversation = MagicMock()
    conversation.id = "c1"
    conversation.session_id = "test_chat_brochure_link_001"
    conversation.site_origin = "tajlandia.com"
    conversation.preferences = {}
    conversation.message_history = []

    monkeypatch.setattr(
        "app.services.public_agent.get_property_by_id",
        lambda db, pid: prop,
    )
    monkeypatch.setattr(
        "app.services.brochure_service.generate_and_upload_brochure",
        lambda **kw: {
            "success": True,
            "file_url": "https://cdn.example.com/brochure.pdf",
            "download_url": "https://cdn.example.com/brochure.pdf",
            "filename": "brochure.pdf",
            "pdf_bytes": b"%PDF-1.4 mock",
        },
    )
    monkeypatch.setattr("app.services.public_agent.enqueue_lead_save", lambda **kw: None)
    monkeypatch.setattr("app.services.public_agent.record_or_update_lead", lambda **kw: None)
    monkeypatch.setattr("app.services.public_agent.append_conversation_turn", lambda **kw: None)
    monkeypatch.setattr("app.services.public_agent.settings.ENABLE_FIRESTORE_SYNC", False)

    resp = agent._handle_brochure_request(
        db=MagicMock(),
        conversation=conversation,
        args={"property_id": "isla-kata-sea-view-villa"},
        user_message="Download brochure for Isla Kata",
        start_time=0.0,
        language="en",
    )

    assert resp["type"] == "brochure"
    assert resp.get("pdf_url")
    assert resp.get("download_url")
    assert "[Download PDF]" in resp["text"]
    assert " - " in resp["text"]
    assert "emailed" not in resp["text"].lower()
    assert "sent via gmail" not in resp["text"].lower()
