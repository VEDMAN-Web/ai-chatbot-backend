"""
Tests for brochure_data loader and brochure_sections registry.
Fully mocked: no Firestore, PostgreSQL, OpenAI, or network.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.brochure_data import (
    BrochureData,
    load_brochure_data,
    localize_label,
    resolve_localized,
    STATUS_LABELS,
    AMENITY_LABELS,
)
from app.services.brochure_sections import (
    BROCHURE_SECTIONS,
    at_a_glance_rows,
    build_brochure_page_plan,
    chunk_list,
    select_sections,
    truncate_at_sentence,
)


EM_DASH = "\u2014"


def test_load_from_firestore_maps_and_localizes_labels():
    raw = {
        "name": {"en": "Kata Sea View Villa"},
        "cityId": "Kata",
        "province": "Phuket",
        "countryId": "thailand",
        "priceFrom": 12_500_000,
        "currency": "THB",
        "completionStatus": "UNDER_CONSTRUCTION",
        "ownershipType": "leasehold",
        "description": "A hillside villa.",
        "highlights": ["Sea view", "Private pool"],
        "facilities": ["pool", "parking", "plavatelnyy-basseyn"],
        "amenities": ["gym", "security"],
        "coverImage": "https://cdn.example/a.jpg",
        "images": ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        "floorPlans": ["https://cdn.example/fp.jpg"],
        "masterPlan": ["https://cdn.example/master.jpg"],
        "completionDate": "Q4 2027",
        "parking": "2 cars",
        "layouts": [{"name": "Type A", "bedrooms": 3, "bathrooms": 3, "area": "280 sqm", "units": 6}],
        "nearbyPlaces": [
            {"category": "beach", "name": "Kata Beach", "distance": "5 min"},
            {"category": "airport", "name": "Phuket Airport", "distance_text": "45 min"},
        ],
    }
    card = {"property_id": "PROP-TEST-001", "name": "Kata Sea View Villa", "raw_firestore_data": raw}

    with patch("app.services.firestore_service.get_property_by_id_from_firestore", return_value=card):
        data = load_brochure_data(MagicMock(), "PROP-TEST-001", site_origin="thai.estate", language="en")

    assert data.name == "Kata Sea View Villa"
    assert data.district == "Kata"
    assert data.price_display == "from 12.50M THB"
    assert data.status == "Under construction"
    assert "Swimming pool" in data.facilities
    assert "Parking" in data.facilities
    # Duplicate slug amenity collapsed / labelled, not raw slug
    assert "plavatelnyy-basseyn" not in data.facilities
    assert data.ownership_type == "Leasehold"
    assert data.hero_image == "https://cdn.example/a.jpg"
    assert "https://cdn.example/fp.jpg" in data.floor_plans
    assert data.completion == "Q4 2027"
    assert data.contact and data.contact.email == "advisor@thai.estate"
    assert "advisor@varsoviaestate.com" not in (data.contact.email or "")
    assert data.unit_types and data.unit_types[0].name == "Type A"
    assert data.nearby_places and data.nearby_places[0].name == "Kata Beach"


def test_load_firestore_fallback_no_invented_price_or_description():
    db = MagicMock()
    

    fs_card = {
        "property_id": "isla-kata-sea-view-villa",
        "name": "Isla Kata",
        "price": "Price on request",  # invented by old normaliser — ignore
        "bedrooms": "1-4 Beds",  # invented default — ignore without layouts
        "area": "N/A",
        "status": "Available",  # invented from isActive
        "description": "Exclusive development 'Isla Kata' in Phuket. Featuring 1-4 Beds, N/A, with pricing starting from Price on request.",
        "images": ["https://cdn.example/hero.jpg"],
        "raw_firestore_data": {
            "name": {"en": "Isla Kata Sea View Villa", "pl": "Isla Kata"},
            "cityId": "phuket",
            "countryId": "thailand",
            "coverImage": "https://cdn.example/hero.jpg",
            "images": ["https://cdn.example/hero.jpg", "https://cdn.example/2.jpg"],
            "amenities": ["pool", "gym"],
            "isActive": True,
            # no priceFrom, no description, no completionStatus
        },
    }

    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=fs_card,
    ):
        data = load_brochure_data(
            db, "isla-kata-sea-view-villa", site_origin="tajlandia.com", language="en"
        )

    assert data.property_id == "isla-kata-sea-view-villa"
    assert data.name == "Isla Kata Sea View Villa"
    assert data.price_display is None
    assert data.price_min is None
    assert data.description is None
    assert data.bedrooms_display is None
    assert data.area_display is None
    assert data.status is None
    assert "Swimming pool" in data.amenities
    assert data.contact.email == "advisor@tajlandia.com"
    assert data.contact.email != "advisor@varsoviaestate.com"


def test_load_neither_store_raises():
    db = MagicMock()
    
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=None,
    ):
        with pytest.raises(ValueError, match="not found"):
            load_brochure_data(db, "missing-prop", site_origin="thai.estate")


def test_multilingual_resolution_pl_fallback_en():
    assert resolve_localized({"en": "Hello", "pl": "Cześć"}, "pl") == "Cześć"
    assert resolve_localized({"en": "Hello"}, "th") == "Hello"
    assert resolve_localized({"th": "สวัสดี"}, "pl") == "สวัสดี"
    assert resolve_localized(None, "en") is None
    assert resolve_localized({"en": ["a", "b"]}, "en", as_list=True) == ["a", "b"]


def test_status_and_amenity_labels_no_raw_slugs():
    assert localize_label("UNDER_CONSTRUCTION", "en", STATUS_LABELS) == "Under construction"
    assert localize_label("under_construction", "pl", STATUS_LABELS) == "W budowie"
    assert localize_label("plavatelnyy-basseyn", "en", AMENITY_LABELS) == "Swimming pool"
    unknown = localize_label("rooftop-cinema", "en", AMENITY_LABELS)
    assert unknown == "Rooftop Cinema"
    assert "rooftop-cinema" != unknown


def test_sparse_firestore_doc_does_not_invent():
    card = {"property_id": "PROP-TEST-001", "name": "Sparse Villa", "raw_firestore_data": {"name": "Sparse Villa"}}
    with patch("app.services.firestore_service.get_property_by_id_from_firestore", return_value=card):
        data = load_brochure_data(MagicMock(), "PROP-TEST-001", language="en")

    assert data.price_display is None
    assert data.description is None
    assert data.bedrooms_display is None
    assert data.developer_name is None
    assert data.images == []
    assert data.facilities == []
    assert "Price on request" not in (data.price_display or "")
    assert "N/A" not in (data.area_display or "")


def test_sections_cover_contact_always_others_skip_when_empty():
    sparse = BrochureData(
        property_id="x",
        name="Sparse Villa",
        contact=None,
        language="en",
    )
    # contact always=True even if contact object missing
    ids = [s.id for s in select_sections(sparse)]
    assert ids[0] == "cover"
    assert ids[-1] == "contact"
    assert "intro" not in ids
    assert "gallery" not in ids
    assert "facilities" not in ids
    assert "floor_plans" not in ids


def test_sections_render_only_when_data_present():
    rich = BrochureData(
        name="Rich Villa",
        description="A long enough verified description about the project.",
        highlights=["Pool", "View"],
        images=["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"],
        facilities=["Swimming pool", "Gym"],
        location_text="Kata, Phuket",
        price_display="from 10M THB",
        floor_plans=["https://cdn.example/fp.jpg"],
        language="en",
    )
    ids = [s.id for s in select_sections(rich)]
    assert "cover" in ids
    assert "intro" in ids
    assert "highlights" in ids
    assert "gallery" in ids
    assert "facilities" in ids
    assert "at_a_glance" in ids
    assert "floor_plans" in ids
    assert "contact" in ids
    assert "master_plan" not in ids
    assert "unit_types" not in ids


def test_at_a_glance_only_real_rows_no_na():
    data = BrochureData(
        name="X",
        location_text="Phuket",
        price_display="from 5M THB",
        status="Under construction",
        area_display=None,
        bedrooms_display=None,
    )
    rows = at_a_glance_rows(data)
    labels = {r["label"] for r in rows}
    assert labels == {"Location", "Price", "Status"}
    assert all(r["value"] and r["value"].upper() != "N/A" for r in rows)


def test_truncate_and_chunk_helpers():
    short = truncate_at_sentence("Short text.")
    assert short == "Short text."

    long = "First sentence here. " + ("Word " * 200) + "Final stop."
    trunc = truncate_at_sentence(long, max_chars=80)
    assert trunc is not None
    assert len(trunc) <= 85
    assert EM_DASH not in trunc

    assert chunk_list(list(range(30)), 14) == [
        list(range(14)),
        list(range(14, 28)),
        list(range(28, 30)),
    ]
    assert chunk_list([], 14) == []


def test_page_plan_paginates_facilities_and_skips_empty():
    facilities = [f"Facility {i}" for i in range(20)]
    data = BrochureData(
        name="Paginated",
        facilities=facilities,
        description=None,
        images=[],
        language="en",
    )
    pages = build_brochure_page_plan(data)
    section_ids = [p["section_id"] for p in pages]
    assert section_ids[0] == "cover"
    assert section_ids[-1] == "contact"
    facility_pages = [p for p in pages if p["section_id"] == "facilities"]
    assert len(facility_pages) == 2
    assert len(facility_pages[0]["facilities"]) == 14
    assert len(facility_pages[1]["facilities"]) == 6
    assert "intro" not in section_ids
    assert "gallery" not in section_ids


def test_firestore_real_price_and_pl_description():
    db = MagicMock()
    
    fs_card = {
        "property_id": "proj-1",
        "name": "Fallback",
        "raw_firestore_data": {
            "name": {"en": "Ocean House", "pl": "Dom Oceaniczny"},
            "description": {
                "en": "English description of the villa.",
                "pl": "Polski opis willi przy oceanie.",
            },
            "priceFrom": 8_000_000,
            "currency": "THB",
            "completionStatus": "under_construction",
            "amenities": ["sea-view"],
            "layouts": [
                {"name": "A", "bedrooms": 2, "bathrooms": 2, "area": "120 sqm"},
                {"name": "B", "bedrooms": 4, "bathrooms": 3, "area": "220 sqm"},
            ],
        },
    }
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=fs_card,
    ):
        data = load_brochure_data(db, "proj-1", site_origin="thai.estate", language="pl")

    assert data.name == "Dom Oceaniczny"
    assert data.description == "Polski opis willi przy oceanie."
    assert data.price_min == 8_000_000
    assert data.price_display is not None
    assert "8" in data.price_display
    assert data.status == "W budowie"
    assert data.bedrooms_display == "2-4"
    assert data.unit_types and len(data.unit_types) == 2
    assert EM_DASH not in (data.description or "")
