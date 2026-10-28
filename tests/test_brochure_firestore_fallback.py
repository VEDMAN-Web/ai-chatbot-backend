"""
Brochure generator from Firestore project documents + honest agent failure.
Uses shared FakeFirestore; no Postgres path when STORE=firestore.
Fully mocked: no network, no real DB credentials.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services.brochure_service import (
    _firestore_card_to_brochure_prop,
    _load_property_for_brochure,
    generate_single_property_brochure_pdf,
)
from app.services.i18n_service import get_brochure_unavailable_text
from app.services.property_index import get_property_index, reset_property_index_for_tests
from app.services.public_agent import PublicAgentService
from tests.test_property_index_f1 import FakeFirestore


FAKE_FS_DOC = {
    "name": {"en": "Isla Kata Sea View Villa"},
    "propertyType": "Villa",
    "priceFrom": 18_500_000,
    "currency": "THB",
    "layouts": [{"bedrooms": 3}],
    "areaFrom": 270,
    "cityId": "Kata",
    "countryId": "thailand",
    "province": "Phuket",
    "completionStatus": "Available",
    "amenities": ["Pool"],
    "tags": ["Pool"],
    "images": ["https://example.com/villa1.jpg"],
    "coverImage": "https://example.com/villa1.jpg",
    "description": {"en": "Verified villa in Kata."},
    "highlights": ["Pool"],
    "isActive": True,
    "websites": ["tajlandia.com", "thai.estate", "varsoviaestate.com"],
    "slug": {"en": "isla-kata-sea-view-villa"},
}


FAKE_FS_SPARSE_DOC = {
    "name": {"en": "Sparse Project"},
    "countryId": "thailand",
    "isActive": True,
    "websites": ["thai.estate", "tajlandia.com", "varsoviaestate.com"],
    "images": [],
}


def _seed(fs: FakeFirestore, doc_id: str, data: dict, monkeypatch):
    reset_property_index_for_tests()
    fs.set("projects", doc_id, data)
    fs.set("meta", "properties_version", {"version": 1, "updated_at": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda target="user": fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda: fs)
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "firestore")
    idx = get_property_index()
    idx.set_client_override(fs)
    idx.ensure_fresh(force=True)
    return idx


@pytest.fixture
def fake_fs(monkeypatch):
    reset_property_index_for_tests()
    fs = FakeFirestore()
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda target="user": fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda: fs)
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "firestore")
    yield fs
    reset_property_index_for_tests()


def test_1_firestore_generates_pdf(fake_fs, monkeypatch):
    _seed(fake_fs, "isla-kata-sea-view-villa", FAKE_FS_DOC, monkeypatch)
    db = MagicMock()
    with patch(
        "app.services.brochure_service.fetch_property_image_reader",
        return_value=MagicMock(),
    ), patch(
        "app.services.brochure_service.default_storage_provider"
    ) as storage, patch(
        "app.services.brochure_service.ImageReader",
        side_effect=lambda *a, **k: MagicMock(),
    ), patch(
        "app.services.brochure_service.resolve_logo_path",
        return_value=None,
    ):
        storage.save_file.return_value = "https://cdn.example.com/brochure.pdf"
        res = generate_single_property_brochure_pdf(
            db=db,
            property_id="isla-kata-sea-view-villa",
            site_origin="tajlandia.com",
            language="en",
        )
    assert res["success"] is True
    assert res["pdf_bytes"].startswith(b"%PDF")
    assert res["property_id"] == "isla-kata-sea-view-villa"
    assert res["pages"] == 8


def test_2_firestore_path_loads_seeded_project(fake_fs, monkeypatch):
    """Replaces the old Postgres-only brochure path with a Firestore project fixture."""
    doc = {
        "name": {"en": "Firestore Villa"},
        "cityId": "Layan",
        "province": "Phuket",
        "countryId": "thailand",
        "propertyType": "Villa",
        "completionStatus": "Available",
        "priceFrom": 12_000_000,
        "currency": "THB",
        "ownershipType": "Leasehold",
        "amenities": ["Pool"],
        "developerName": "Dev Co",
        "description": {"en": "A verified FS listing."},
        "layouts": [{"bedrooms": 3, "bathrooms": 3}],
        "areaFrom": 200,
        "images": [],
        "isActive": True,
        "websites": ["varsoviaestate.com", "tajlandia.com", "thai.estate"],
    }
    _seed(fake_fs, "PROP-FS-001", doc, monkeypatch)
    db = MagicMock()
    # SQL query must never be required in firestore mode
    db.query.side_effect = AssertionError("SQL must not be queried in firestore mode")

    with patch(
        "app.services.brochure_service.fetch_property_image_reader",
        return_value=MagicMock(),
    ), patch(
        "app.services.brochure_service.default_storage_provider"
    ) as storage, patch(
        "app.services.brochure_service.resolve_logo_path",
        return_value=None,
    ):
        storage.save_file.return_value = "https://cdn.example.com/fs.pdf"
        res = generate_single_property_brochure_pdf(
            db=db,
            property_id="PROP-FS-001",
            site_origin="varsoviaestate.com",
            language="en",
        )
    assert res["success"] is True
    assert res["pdf_bytes"].startswith(b"%PDF")
    assert res["property_id"] == "PROP-FS-001"


def test_3_missing_everywhere_raises_and_endpoint_404(fake_fs, monkeypatch):
    _seed(fake_fs, "other", {
        "name": {"en": "Other"},
        "isActive": True,
        "websites": ["tajlandia.com"],
        "priceFrom": 1,
    }, monkeypatch)
    db = MagicMock()
    with pytest.raises(ValueError, match="not found"):
        _load_property_for_brochure(db, "missing-slug-xyz")

    from app.api.v1 import chat as chat_mod

    with patch(
        "app.services.brochure_service.generate_single_property_brochure_pdf",
        side_effect=ValueError("Property with ID 'missing-slug-xyz' not found."),
    ), patch.object(chat_mod, "get_or_create_conversation", return_value=MagicMock()):
        with pytest.raises(HTTPException) as ei:
            chat_mod.download_property_brochure(
                property_id="missing-slug-xyz",
                site_origin="tajlandia.com",
                session_id=None,
                format=None,
                db=MagicMock(),
            )
    assert ei.value.status_code == 404
    assert "not found" in str(ei.value.detail).lower()


def test_4_sparse_firestore_no_images_no_crash_no_invented_price(fake_fs, monkeypatch):
    _seed(fake_fs, "sparse-project", FAKE_FS_SPARSE_DOC, monkeypatch)
    card = get_property_index().get_by_id("sparse-project", include_raw=True)
    assert card is not None
    adapted = _firestore_card_to_brochure_prop(card)
    assert adapted.images == []
    assert adapted.price_display is None
    assert adapted.price_numeric_min is None
    assert adapted.bedrooms_display == "Not documented in verified database"
    # Missing ownership must not invent Freehold/Leasehold (omit / None)
    assert adapted.ownership_type in (None, "", "Confirm with advisor", "Potwierdź z doradcą")
    assert "Freehold / Leasehold" not in str(adapted.ownership_type or "")
    assert "9.xx" not in str(adapted.price_display)
    assert adapted.bedrooms_min is None

    db = MagicMock()
    with patch(
        "app.services.brochure_service.fetch_property_image_reader",
        return_value=MagicMock(),
    ), patch(
        "app.services.brochure_service.default_storage_provider"
    ) as storage, patch(
        "app.services.brochure_service.resolve_logo_path",
        return_value=None,
    ):
        storage.save_file.return_value = "https://cdn.example.com/sparse.pdf"
        res = generate_single_property_brochure_pdf(
            db=db,
            property_id="sparse-project",
            site_origin="thai.estate",
            language="en",
        )
    assert res["pdf_bytes"].startswith(b"%PDF")
    # Must not invent a fake numeric price into metadata
    assert "99" not in (res.get("property_name") or "")


def test_5_agent_brochure_failure_honest_no_fake_link(fake_fs, monkeypatch):
    _seed(fake_fs, "isla-kata-sea-view-villa", FAKE_FS_DOC, monkeypatch)
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None

    for site_origin, advisor, banned in [
        ("tajlandia.com", "advisor@tajlandia.com", "advisor@varsoviaestate.com"),
        ("thai.estate", "advisor@thai.estate", "advisor@varsoviaestate.com"),
    ]:
        conversation = SimpleNamespace(
            id="c1",
            session_id="s1",
            site_origin=site_origin,
            preferences={"last_result_ids": ["isla-kata-sea-view-villa"]},
            message_history=[],
        )
        with patch(
            "app.services.brochure_service.generate_and_upload_brochure",
            side_effect=RuntimeError("upload failed"),
        ), patch(
            "app.services.public_agent.enqueue_lead_save"
        ) as lead_mock, patch(
            "app.services.public_agent.record_or_update_lead"
        ), patch(
            "app.services.public_agent.append_conversation_turn"
        ), patch(
            "app.services.public_agent.settings.ENABLE_FIRESTORE_SYNC", True
        ):
            db = MagicMock()
            result = agent._handle_brochure_request(
                db=db,
                conversation=conversation,
                args={"property_id": "isla-kata-sea-view-villa"},
                user_message="please send brochure for Isla Kata",
                start_time=0.0,
                language="en",
            )

        assert result["type"] == "text"
        assert result.get("brochure_property_id") is None
        assert result.get("pdf_url") is None
        assert result.get("download_url") is None
        assert "[Download PDF]" not in result["text"]
        assert "/api/v1/chat/brochure/" not in result["text"]
        assert advisor in result["text"]
        assert banned not in result["text"]
        assert "\u2014" not in result["text"]
        # Lead recorded as failed request (background enqueue), not as delivered
        assert lead_mock.called
        signal = (lead_mock.call_args.kwargs.get("buying_signal") or "")
        assert "failed" in signal.lower()
        # Must not mark brochures_requested as delivered
        assert "isla-kata-sea-view-villa" not in list(
            (conversation.preferences or {}).get("brochures_requested") or []
        )


def test_brochure_unavailable_i18n_no_emdash():
    texts = {
        lang: get_brochure_unavailable_text(language=lang, site_origin="tajlandia.com", property_label="Isla Kata")
        for lang in ("en", "pl", "th")
    }
    assert len(set(texts.values())) == 3
    for t in texts.values():
        assert t.strip()
        assert "\u2014" not in t
        assert "advisor@tajlandia.com" in t
        assert "advisor@varsoviaestate.com" not in t
