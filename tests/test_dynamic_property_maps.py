import pytest
from app.services.maps_service import (
    build_google_maps_url,
    build_google_maps_embed_url,
    get_property_proximity_landmarks,
)


def test_dynamic_google_maps_url_from_db_coordinates():
    # When a new property in Chiang Mai is added with GPS coordinates in the database
    url = build_google_maps_url(
        latitude=18.7883,
        longitude=98.9853,
        property_name="Chiang Mai Luxury Villa",
        location_str="Nimman, Chiang Mai"
    )
    assert "query=18.7883%2C98.9853" in url
    assert "https://www.google.com/maps/search/?" in url


def test_dynamic_google_maps_url_from_db_address():
    # When a new property is added with address but without lat/lng
    url = build_google_maps_url(
        latitude=None,
        longitude=None,
        property_name="Beachfront Villa Samui",
        location_str="Chaweng, Koh Samui",
        address="123 Chaweng Beach Road"
    )
    assert "Beachfront+Villa+Samui" in url
    assert "123+Chaweng+Beach+Road" in url


def test_dynamic_google_maps_embed_universal():
    # Dynamic Google Maps embed without requiring any API key
    embed_info = build_google_maps_embed_url(
        latitude=9.5312,
        longitude=100.0638,
        property_name="Samui Seafront Residence",
        location_str="Koh Samui"
    )
    assert embed_info["provider"] == "google_maps_embed_universal"
    assert "maps.google.com/maps?q=9.5312%2C100.0638" in embed_info["embed_url"]
    assert "output=embed" in embed_info["embed_url"]


def test_dynamic_landmarks_from_database():
    # If the database Property record has custom nearby_places
    custom_places = [
        {"name": "Private Beach Club", "distance_km": 0.5, "drive_time_mins": 2},
        {"name": "International Hospital", "distance_km": 4.2, "drive_time_mins": 8}
    ]
    landmarks = get_property_proximity_landmarks(
        latitude=8.0,
        longitude=98.3,
        nearby_places_from_db=custom_places
    )
    assert len(landmarks) == 2
    assert landmarks[0]["name"] == "Private Beach Club"
    assert landmarks[0]["distance_km"] == 0.5
    assert landmarks[1]["name"] == "International Hospital"
