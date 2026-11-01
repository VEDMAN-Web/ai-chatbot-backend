from __future__ import annotations
import pytest
from app.api.v1.admin import PropertyCreateRequest, PropertyUpdateRequest
from app.services.property_normalizer import validate_project_schema, COMPULSORY_FIELDS, OPTIONAL_FIELDS

def test_compulsory_fields_validation_failure():
    # Empty data should fail compulsory fields validation
    is_valid, errors = validate_project_schema({})
    assert is_valid is False
    assert any("Project Name (PL)" in e for e in errors)
    assert any("Project Name (EN)" in e for e in errors)
    assert any("Project Type" in e for e in errors)
    assert any("Country" in e for e in errors)
    assert any("City" in e for e in errors)
    assert any("Short Description (PL)" in e for e in errors)
    assert any("Short Description (EN)" in e for e in errors)
    assert any("Media" in e for e in errors)

def test_compulsory_and_optional_fields_success():
    payload = {
        # 1. Basic Info
        "project_name_pl": "Luksusowa Willa Horizon",
        "project_name_en": "Villa Horizon Luxury Residences",
        "property_type": "Villa",
        "slug_pl": "luksusowa-willa-horizon",
        "slug_en": "villa-horizon-luxury-residences",
        "subtitle_pl": "Ekskluzywne wille nad morzem",
        "subtitle_en": "Exclusive seaside villas",
        "country": "Thailand",
        "city": "Phuket",
        "websites": ["tajlandia.com", "varsoviaestate.com"],
        # 2. Pricing
        "currency": "THB",
        "price_from": 18500000.0,
        "price_to": 24000000.0,
        "expected_roi": "7.5%",
        "rental_yield": "8.0%",
        # 3. Details
        "completion_status": "Under construction",
        "completion_date": "Q4 2026",
        "total_units": 24,
        "available_units": 8,
        "area_from_sqm": 320.0,
        "area_to_sqm": 450.0,
        "amenities": ["Infinity Pool", "Private Gym", "24/7 Security"],
        "tags": ["Seaview", "Luxury", "Investment"],
        # 4. Description PL
        "short_description_pl": "Oszałamiająca luksusowa willa z basenem i panoramicznym widokiem na morze.",
        "full_description_pl": "Pełny opis projektu po polsku...",
        # 5. Description EN
        "short_description_en": "Stunning luxury pool villa with panoramic sea views.",
        "full_description_en": "Full description of the project in English...",
        # 6. Media
        "media": ["https://tajlandia.com/media/villa1.webp"],
        "video_url": "https://youtube.com/watch?v=sample",
        # 7. Location
        "search_location": "Bang Tao Beach, Phuket",
        "latitude": 7.9843,
        "longitude": 98.2983,
        "address_en": "123 Lagoon Road, Choeng Thale, Phuket",
        "address_pl": "123 Lagoon Road, Choeng Thale, Phuket",
        "neighborhood_lifestyle": "5 minutes from Boat Avenue and Bang Tao Beach.",
        # 8. Layouts
        "layouts": [
            {"name": "Villa Type A", "bedrooms": 3, "bathrooms": 4, "area_sqm": 320.0, "price": "18.5M THB"}
        ],
        # 9. SEO
        "seo_title_pl": "Luksusowe Wille w Phuket | Varsovia Estate",
        "seo_title_en": "Luxury Villas in Phuket | Tajlandia",
        "seo_description_pl": "Kup luksusową willę w Tajlandii z gwarantowanym zwrotem z inwestycji.",
        "seo_description_en": "Buy a luxury villa in Thailand with guaranteed rental yield.",
        # 10. Publish
        "active": True,
        "featured": True,
        "top_list": True
    }
    
    # 1. Custom schema validator
    is_valid, errors = validate_project_schema(payload)
    assert is_valid is True, errors

    # 2. Pydantic request model
    req = PropertyCreateRequest(**payload)
    assert req.project_name_pl == "Luksusowa Willa Horizon"
    assert req.project_name_en == "Villa Horizon Luxury Residences"
    assert req.property_type == "Villa"
    assert req.country == "Thailand"
    assert req.city == "Phuket"
    assert req.expected_roi == "7.5%"
    assert req.rental_yield == "8.0%"
    assert req.available_units == 8
    assert len(req.media) == 1
    assert len(req.layouts) == 1
    assert req.active is True
    assert req.featured is True
    assert req.top_list is True

def test_missing_media_flags_manual_review_in_ingest():
    from app.services.drive_folder_ingest import missing_required
    
    prov_without_images = {
        "property_name": {"value": "Villa Horizon", "status": "found"},
        "property_type": {"value": "Villa", "status": "found"},
        "country": {"value": "Thailand", "status": "found"},
        "city": {"value": "Phuket", "status": "found"},
        "short_description_en": {"value": "Stunning villa", "status": "found"},
        "short_description_pl": {"value": "Oszałamiająca willa", "status": "found"},
        # No images / media!
    }
    missing = missing_required(prov_without_images)
    assert "media" in missing, f"Expected 'media' in missing fields, got: {missing}"

    prov_with_images = dict(prov_without_images)
    prov_with_images["images"] = {"value": ["https://tajlandia.com/villa.webp"], "status": "found"}
    missing_with_images = missing_required(prov_with_images)
    assert "media" not in missing_with_images
