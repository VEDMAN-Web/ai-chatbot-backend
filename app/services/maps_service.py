import math
import urllib.parse
from typing import Dict, Any, List, Optional
from app.core.config import settings

# Key Reference Landmarks in Thailand for Proximity & Amenity Distance Calculations
THAILAND_LANDMARKS = [
    # Phuket Landmarks
    {"name": "Phuket International Airport (HKT)", "lat": 8.1132, "lng": 98.3169, "region": "Phuket", "category": "Airport"},
    {"name": "Bang Tao Beach & Laguna", "lat": 7.9942, "lng": 98.2936, "region": "Phuket", "category": "Beach"},
    {"name": "Layan Beach", "lat": 8.0289, "lng": 98.2961, "region": "Phuket", "category": "Beach"},
    {"name": "Surin Beach", "lat": 7.9754, "lng": 98.2779, "region": "Phuket", "category": "Beach"},
    {"name": "Patong Beach", "lat": 7.8967, "lng": 98.2965, "region": "Phuket", "category": "Beach & Nightlife"},
    {"name": "Rawai Beach & Promthep Cape", "lat": 7.7645, "lng": 98.3242, "region": "Phuket", "category": "Beach & Viewpoint"},
    {"name": "Central Phuket Floresta", "lat": 7.8925, "lng": 98.3681, "region": "Phuket", "category": "Shopping & Dining"},
    {"name": "British International School Phuket (BISP)", "lat": 7.9472, "lng": 98.3639, "region": "Phuket", "category": "Education"},
    {"name": "Royal Phuket Marina", "lat": 7.9547, "lng": 98.3892, "region": "Phuket", "category": "Marina & Yachting"},
    {"name": "Blue Canyon Country Club", "lat": 8.1065, "lng": 98.3308, "region": "Phuket", "category": "Golf"},

    # Koh Samui Landmarks
    {"name": "Samui International Airport (USM)", "lat": 9.5478, "lng": 100.0622, "region": "Samui", "category": "Airport"},
    {"name": "Chaweng Beach", "lat": 9.5312, "lng": 100.0638, "region": "Samui", "category": "Beach"},
    {"name": "Fisherman's Village Bophut", "lat": 9.5583, "lng": 100.0317, "region": "Samui", "category": "Dining & Culture"},
    {"name": "Maenam Beach", "lat": 9.5762, "lng": 99.9967, "region": "Samui", "category": "Beach"},
    {"name": "Santiburi Samui Country Club", "lat": 9.5667, "lng": 99.9833, "region": "Samui", "category": "Golf"},
    {"name": "Plai Laem Temple & Beach", "lat": 9.5736, "lng": 100.0664, "region": "Samui", "category": "Beach & Landmark"},
]


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two GPS coordinates in kilometers."""
    R = 6371.0  # Earth radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 1)


def build_google_maps_url(
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    property_name: Optional[str] = None,
    location_str: Optional[str] = None,
    address: Optional[str] = None,
    direct_map_url: Optional[str] = None
) -> str:
    """
    Constructs a verified Google Maps universal search/navigation URL dynamically from database attributes.
    Works on mobile, desktop, Google Maps app, and browsers without any API keys.
    """
    if direct_map_url:
        return direct_map_url

    if latitude is not None and longitude is not None:
        query = f"{latitude},{longitude}"
    else:
        query_parts = [p.strip() for p in [property_name, address, location_str, "Thailand"] if p and p.strip()]
        # Remove duplicate mentions
        seen = set()
        clean_parts = []
        for part in query_parts:
            low = part.lower()
            if low not in seen:
                seen.add(low)
                clean_parts.append(part)
        query = " ".join(clean_parts) if clean_parts else "Thailand"

    params = {"api": "1", "query": query}
    return f"https://www.google.com/maps/search/?{urllib.parse.urlencode(params)}"


def build_google_maps_embed_url(
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    location_str: Optional[str] = None,
    property_name: Optional[str] = None,
    address: Optional[str] = None,
    direct_embed_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Builds an embeddable Google Map iframe URL dynamically from database property records.
    If GOOGLE_MAPS_API_KEY is configured, uses official Google Maps Embed API.
    Otherwise, generates an authentic Google Maps embed (via maps.google.com universal embed)
    grounded strictly in the property's DB coordinates, address, or name.
    """
    if direct_embed_url:
        return {
            "provider": "google_maps_direct",
            "embed_url": direct_embed_url,
            "is_official_api": True
        }

    api_key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")

    # Build clean query string from database fields
    if latitude is not None and longitude is not None:
        query_val = f"{latitude},{longitude}"
    else:
        parts = [p.strip() for p in [property_name, address, location_str, "Thailand"] if p and p.strip()]
        seen = set()
        clean = []
        for p in parts:
            if p.lower() not in seen:
                seen.add(p.lower())
                clean.append(p)
        query_val = " ".join(clean) if clean else "Thailand"

    if api_key:
        encoded_query = urllib.parse.quote(query_val)
        return {
            "provider": "google_maps_embed",
            "embed_url": f"https://www.google.com/maps/embed/v1/place?key={api_key}&q={encoded_query}&zoom=14",
            "is_official_api": True
        }

    # Universal Google Maps Embed (No API Key Required - 100% Real Google Maps)
    encoded_query = urllib.parse.quote(query_val)
    google_universal_embed = f"https://maps.google.com/maps?q={encoded_query}&t=&z=14&ie=UTF8&iwloc=&output=embed"

    return {
        "provider": "google_maps_embed_universal",
        "embed_url": google_universal_embed,
        "is_official_api": False
    }


def get_property_proximity_landmarks(
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    district: Optional[str] = None,
    province: Optional[str] = None,
    nearby_places_from_db: Optional[List[Dict[str, Any]]] = None,
    max_landmarks: int = 5
) -> List[Dict[str, Any]]:
    """
    Returns nearby landmark distances dynamically.
    Priority 1: Uses property's dedicated `nearby_places` directly from the database if present.
    Priority 2: Computes proximity dynamically from property's GPS coordinates (`latitude`, `longitude`).
    """
    # 1. Use database nearby_places if available
    if nearby_places_from_db and isinstance(nearby_places_from_db, list) and len(nearby_places_from_db) > 0:
        formatted = []
        for place in nearby_places_from_db:
            if isinstance(place, dict) and place.get("name"):
                dist_km = place.get("distance_km")
                if dist_km is None:
                    # Try to parse string or fallback
                    dist_km = 1.5
                drive_mins = place.get("drive_time_mins") or max(2, round((float(dist_km) / 35.0) * 60))
                formatted.append({
                    "name": str(place.get("name")),
                    "category": str(place.get("category", "Attraction")),
                    "distance_km": round(float(dist_km), 1),
                    "drive_time_mins": int(drive_mins),
                    "region": str(place.get("region", province or district or "Local"))
                })
        if formatted:
            return formatted[:max_landmarks]

    # 2. Compute dynamically from property coordinates
    lat = latitude
    lng = longitude

    if lat is None or lng is None:
        # If coordinates are missing, fallback to regional coordinate lookup
        DISTRICT_COORDS = {
            "bang tao": (7.9942, 98.2936),
            "layan": (8.0289, 98.2961),
            "karon": (7.8347, 98.2975),
            "kata": (7.8214, 98.3012),
            "rawai": (7.7761, 98.3189),
            "kamala": (7.9542, 98.2831),
            "surin": (7.9754, 98.2779),
            "patong": (7.8967, 98.2965),
            "chaweng": (9.5312, 100.0638),
            "maenam": (9.5762, 99.9967),
            "bophut": (9.5583, 100.0317),
            "plai laem": (9.5736, 100.0664),
        }
        d_key = (district or "").lower().strip()
        if d_key in DISTRICT_COORDS:
            lat, lng = DISTRICT_COORDS[d_key]
        else:
            p_key = (province or "").lower().strip()
            if "samui" in p_key or "surat" in p_key:
                lat, lng = (9.5312, 100.0638)
            else:
                lat, lng = (7.9942, 98.2936)

    results = []
    for lm in THAILAND_LANDMARKS:
        dist_km = haversine_distance_km(lat, lng, lm["lat"], lm["lng"])
        drive_mins = max(3, round((dist_km / 35.0) * 60))
        results.append({
            "name": lm["name"],
            "category": lm["category"],
            "distance_km": dist_km,
            "drive_time_mins": drive_mins,
            "region": lm["region"]
        })

    # Sort by closest distance
    results.sort(key=lambda x: x["distance_km"])
    return results[:max_landmarks]
