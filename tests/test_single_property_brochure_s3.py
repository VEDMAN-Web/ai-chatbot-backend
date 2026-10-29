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
    assert "s3.example.com" in provider.endpoint_url
    assert provider.bucket_name == "tajlandia"
    assert provider.region_name == "us-east-1"
    assert provider.profile_name == "hf"


def test_generate_single_property_brochure_pdf():
    """Brochure is built from a Firestore listing (seeded into the in-memory fake)."""
    from app.core.deps import FirestoreSession
    from app.services.firestore_service import get_firestore_client

    get_firestore_client("user").collection("projects").document("brochure-villa-1").set({
        "name": "Brochure Villa", "province": "Phuket", "propertyType": "Villa", "priceFrom": 5000000,
        "isActive": True, "status": "Available", "websites": ["tajlandia.com"], "bedrooms_min": 3,
        "bedrooms_max": 4, "bedrooms_display": "3-4",
    })
    db = FirestoreSession()
    res = generate_single_property_brochure_pdf(
        db=db, property_id="brochure-villa-1", site_origin="tajlandia.com", language="en",
    )
    assert res["success"] is True
    assert res["pages"] == 8
    assert res["property_id"] == "brochure-villa-1"
    assert len(res["pdf_bytes"]) > 10000

    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(res["pdf_bytes"]))
    assert len(reader.pages) == 8
