"""
Brochure generator Firestore fallback + honest agent failure.
Fully mocked: no network, no real DB, no Firestore credentials.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.brochure_service import (
    _firestore_card_to_brochure_prop,
    _load_property_for_brochure,
    generate_single_property_brochure_pdf,
)
from app.services.i18n_service import get_brochure_unavailable_text
from app.services.public_agent import PublicAgentService


FAKE_FS_CARD = {
    "property_id": "isla-kata-sea-view-villa",
    "name": "Isla Kata Sea View Villa",
    "property_type": "Villa",
    "price": "from 18.50M THB",
    "price_numeric_min": 18_500_000,
    "bedrooms": "3 Beds",
    "area": "270 sqm",
    "location": "Phuket, Kata",
    "district": "phuket",
    "province": "thailand",
    "status": "Available",
    "has_pool": True,
    "beach_access": True,
    "images": ["https://example.com/villa1.jpg"],
    "description": "Verified villa in Kata.",
    "highlights": ["Pool"],
}


FAKE_FS_SPARSE = {
    "property_id": "sparse-project",
    "name": "Sparse Project",
    "property_type": None,
    "price": None,
    "price_numeric_min": None,
    "bedrooms": None,
    "area": None,
    "location": "Thailand",
    "district": None,
    "province": None,
    "status": None,
    "has_pool": False,
    "images": [],
    "description": "",
    "highlights": [],
}


def _mock_db_no_prop():
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = None
    db.query.return_value = q
    return db


def _mock_db_with_prop(prop):
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = prop
    db.query.return_value = q
    return db


def test_1_firestore_fallback_generates_pdf():
    db = _mock_db_no_prop()
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=FAKE_FS_CARD,
    ), patch(
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
        # drawImage may fail on MagicMock; brochure already catches that
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


def test_3_missing_everywhere_raises_and_endpoint_404():
    db = _mock_db_no_prop()
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=None,
    ):
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


def test_4_sparse_firestore_no_images_no_crash_no_invented_price():
    adapted = _firestore_card_to_brochure_prop(FAKE_FS_SPARSE)
    assert adapted.images == []
    assert adapted.price_display is None
    assert adapted.price_numeric_min is None
    assert adapted.bedrooms_display == "Not documented in verified database"
    # Missing ownership must not invent Freehold/Leasehold (omit / None)
    assert adapted.ownership_type in (None, "", "Confirm with advisor", "Potwierdź z doradcą")
    assert "Freehold / Leasehold" not in str(adapted.ownership_type or "")
    assert "9.xx" not in str(adapted.price_display)
    assert adapted.bedrooms_min is None

    db = _mock_db_no_prop()
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=FAKE_FS_SPARSE,
    ), patch(
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


def test_5_agent_brochure_failure_honest_no_fake_link():
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
            "app.services.firestore_service.get_property_by_id_from_firestore",
            return_value=FAKE_FS_CARD,
        ):
            db = MagicMock()
            q = MagicMock()
            q.filter.return_value.first.return_value = None
            db.query.return_value = q
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
