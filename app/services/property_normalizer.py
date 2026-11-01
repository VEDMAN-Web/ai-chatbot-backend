"""
app/services/property_normalizer.py

Enterprise real estate data normalization engine.
Handles:
1. Canonical Property Type normalization (Villa, Condominium, Townhouse, Apartment, House, Land, Commercial).
2. Construction / Listing Status normalization (Ready to move in, Under construction, Pre-sale, Available, Planned).
3. Universal Multi-Currency & Numerical Shorthand pricing (Cr, Crore, Lakh, Lac, MB, M, Million, k, Thousand, AED, USD, EUR, INR, THB).
4. International Area Unit conversions to metric sqm (Sq. Ft, Sq. Yards, Sq. Wah, Rai, Ngan, Guntha, Vigha, Acres, Hectares).
5. Bedroom & Bathroom parsing (e.g. 2.5 baths, Maid rooms, Duplex, 2-4 BHK ranges).
6. Multi-Unit configuration disaggregation table builder.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Canonical types accepted by database and UI
CANONICAL_TYPES = {
    "villa": "Villa",
    "condominium": "Condominium",
    "condo": "Condominium",
    "apartment": "Apartment",
    "flat": "Apartment",
    "bhk": "Apartment",
    "townhouse": "Townhouse",
    "townhome": "Townhouse",
    "penthouse": "Penthouse",
    "house": "House",
    "bungalow": "House",
    "independent house": "House",
    "land": "Land",
    "plot": "Land",
    "commercial": "Commercial",
    "office": "Commercial",
    "shop": "Commercial",
    "retail": "Commercial",
}

CANONICAL_STATUSES = {
    "ready to move in": "Ready to move in",
    "ready": "Ready to move in",
    "ready possession": "Ready to move in",
    "completed": "Ready to move in",
    "immediate": "Ready to move in",
    "under construction": "Under construction",
    "ongoing": "Under construction",
    "civil work": "Under construction",
    "handover": "Under construction",
    "possession soon": "Under construction",
    "pre-sale": "Pre-sale",
    "presale": "Pre-sale",
    "launching soon": "Pre-sale",
    "pre-launch": "Pre-sale",
    "new launch": "Pre-sale",
    "off-plan": "Off-plan",
    "available": "Available",
    "for sale": "For sale",
    "planned": "Planned",
}

# Area conversion multiplier to standard Square Meters (sqm)
AREA_TO_SQM_MULTIPLIERS = {
    "sqm": 1.0,
    "sq.m": 1.0,
    "sq m": 1.0,
    "sq. m": 1.0,
    "square meter": 1.0,
    "square meters": 1.0,
    "sqft": 0.092903,
    "sq.ft": 0.092903,
    "sq ft": 0.092903,
    "sq. ft": 0.092903,
    "square feet": 0.092903,
    "square foot": 0.092903,
    "sq.yd": 0.836127,
    "sq yd": 0.836127,
    "sq.yard": 0.836127,
    "sq yard": 0.836127,
    "sq.yards": 0.836127,
    "sq yards": 0.836127,
    "square yards": 0.836127,
    "sq wah": 4.0,
    "sq.wah": 4.0,
    "tarang wah": 4.0,
    "rai": 1600.0,
    "ngan": 400.0,
    "guntha": 101.171,
    "vigha": 1618.74,
    "bigha": 1618.74,
    "acre": 4046.86,
    "acres": 4046.86,
    "hectare": 10000.0,
    "hectares": 10000.0,
}


def normalize_property_type(raw_type: Optional[str]) -> str:
    """Normalizes raw input or localized real estate terms to canonical types."""
    if not raw_type:
        return "Apartment"
    clean = raw_type.strip().lower()
    for key, canonical in CANONICAL_TYPES.items():
        if key in clean:
            return canonical
    return "Apartment"


def normalize_status(raw_status: Optional[str]) -> str:
    """Normalizes construction / sales status to canonical enum."""
    if not raw_status:
        return "Ready to move in"
    clean = raw_status.strip().lower()
    for key, canonical in CANONICAL_STATUSES.items():
        if key in clean:
            return canonical
    return "Ready to move in"


def parse_and_normalize_price(
    raw_text: str,
    default_currency: Optional[str] = None
) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """
    Parses complex pricing strings across currencies and numbering systems.
    
    Returns:
    (price_numeric_min, price_numeric_max, price_display, detected_currency)
    """
    if not raw_text or not raw_text.strip():
        return None, None, None, default_currency

    clean = raw_text.strip()
    
    # Check for POA / On Request
    if re.search(r"\b(poa|price on request|on request|contact for price|call for price)\b", clean, re.I):
        return None, None, "Price on Request", default_currency or "THB"

    # Detect currency
    currency = default_currency
    if "₹" in clean or re.search(r"\b(inr|rupee|rupees|rs\.?|lakh|crore|cr\.?|lac)\b", clean, re.I):
        currency = "INR"
    elif "$" in clean or re.search(r"\b(usd|dollar|dollars)\b", clean, re.I):
        currency = "USD"
    elif "€" in clean or re.search(r"\b(eur|euro|euros)\b", clean, re.I):
        currency = "EUR"
    elif "฿" in clean or re.search(r"\b(thb|baht)\b", clean, re.I):
        currency = "THB"
    elif "aed" in clean or re.search(r"\b(dirham|dirhams)\b", clean, re.I):
        currency = "AED"
    elif not currency:
        currency = "THB"

    # Multiplier lookup helper
    def parse_amount(val_str: str, mult_str: str) -> float:
        val = float(val_str.replace(",", ""))
        m = (mult_str or "").lower().strip()
        if m in ("cr", "crore", "crores"):
            return val * 10_000_000.0
        elif m in ("lakh", "lakhs", "lac", "lacs", "l"):
            return val * 100_000.0
        elif m in ("m", "million", "millions", "mb"):
            return val * 1_000_000.0
        elif m in ("k", "thousand", "thousands"):
            return val * 1_000.0
        return val

    # Match expressions like "45 Lakh to 1.25 Cr" or "350k - 500k" or "8.5 MB"
    range_pattern = re.compile(
        r"(\d+(?:[.,]\d+)?)\s*(cr|crore|lakh|lac|m|mb|k|million|thousand)?\s*(?:to|-|–)\s*(\d+(?:[.,]\d+)?)\s*(cr|crore|lakh|lac|m|mb|k|million|thousand)?",
        re.I
    )
    m_range = range_pattern.search(clean)
    if m_range:
        v1_str, m1, v2_str, m2 = m_range.groups()
        # If the first value lacks multiplier, borrow from second (e.g. 50 to 75 Lakh)
        m1 = m1 or m2
        min_p = parse_amount(v1_str, m1)
        max_p = parse_amount(v2_str, m2)
        display = f"{min_p:,.0f} - {max_p:,.0f} {currency}"
        return min_p, max_p, display, currency

    # Single amount with possible shorthand
    single_pattern = re.compile(
        r"(?:starting\s+from|from|price:?)?\s*([₹$€฿]?)\s*(\d+(?:[.,]\d+)?)\s*(cr|crore|lakh|lac|m|mb|k|million|thousand)?",
        re.I
    )
    m_single = single_pattern.search(clean)
    if m_single:
        _, val_str, mult_str = m_single.groups()
        amount = parse_amount(val_str, mult_str)
        display = f"{amount:,.0f} {currency}"
        return amount, None, display, currency

    # Fallback raw numbers
    nums = re.findall(r"\b\d[\d,.]*\b", clean)
    if nums:
        try:
            raw_n = float(nums[0].replace(",", ""))
            return raw_n, None, f"{raw_n:,.0f} {currency}", currency
        except Exception:
            pass

    return None, None, clean, currency


def parse_and_normalize_area(
    raw_text: str
) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """
    Parses area strings and converts to metric square meters (sqm).
    
    Returns:
    (area_sqm_min, area_sqm_max, area_display, original_area_type)
    """
    if not raw_text or not raw_text.strip():
        return None, None, None, None

    clean = raw_text.strip().lower()

    # Detect area unit
    multiplier = 1.0
    detected_unit = "sqm"
    for unit_key, mult in AREA_TO_SQM_MULTIPLIERS.items():
        if unit_key in clean:
            multiplier = mult
            detected_unit = unit_key
            break

    # Detect area measurement type (Carpet, Super Built-up, Usable, Plot)
    area_type = "usable"
    if "carpet" in clean:
        area_type = "carpet"
    elif "super" in clean or "built-up" in clean or "builtup" in clean:
        area_type = "super_built_up"
    elif "plot" in clean or "land" in clean:
        area_type = "plot"

    # Search for range: "1200 - 2400 sq.ft"
    range_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:to|-|–)\s*(\d+(?:[.,]\d+)?)", clean)
    if range_match:
        a1 = float(range_match.group(1).replace(",", "")) * multiplier
        a2 = float(range_match.group(2).replace(",", "")) * multiplier
        display = f"{round(a1, 1)}–{round(a2, 1)} sqm"
        return round(a1, 2), round(a2, 2), display, area_type

    # Single value
    single_match = re.search(r"(\d+(?:[.,]\d+)?)", clean)
    if single_match:
        a = float(single_match.group(1).replace(",", "")) * multiplier
        display = f"{round(a, 1)} sqm"
        return round(a, 2), None, display, area_type

    return None, None, raw_text, area_type


def parse_bedrooms_and_bathrooms(
    raw_text: str
) -> Tuple[Optional[int], Optional[int], Optional[str], Optional[int]]:
    """
    Parses bedroom ranges (e.g. '2, 3 & 4 BHK' or '2-3 beds') and bathrooms (e.g. 2.5 bath).
    
    Returns:
    (bedrooms_min, bedrooms_max, bedrooms_display, bathrooms)
    """
    if not raw_text:
        return None, None, None, None

    clean = raw_text.strip()
    
    # Check for BHK pattern: "2, 3 & 4 BHK" or "3 BHK"
    bhk_matches = [int(n) for n in re.findall(r"\b([1-9])\s*(?:bhk|bed|bedroom)", clean, re.I)]
    if not bhk_matches:
        # Check range: "2-4 BHK"
        r = re.search(r"\b([1-9])\s*(?:-|to|–)\s*([1-9])\s*(?:bhk|bed|bedroom)", clean, re.I)
        if r:
            bhk_matches = [int(r.group(1)), int(r.group(2))]

    bed_min = min(bhk_matches) if bhk_matches else None
    bed_max = max(bhk_matches) if bhk_matches and len(bhk_matches) > 1 else None
    
    bed_display = None
    if bed_min and bed_max and bed_min != bed_max:
        bed_display = f"{bed_min}–{bed_max} Bedrooms"
    elif bed_min:
        bed_display = f"{bed_min} Bedrooms"

    # Bathrooms
    baths = None
    bath_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:bath|bathroom|toilet)", clean, re.I)
    if bath_match:
        try:
            baths = int(round(float(bath_match.group(1))))
        except Exception:
            pass

    return bed_min, bed_max, bed_display, baths


def parse_floors_and_units(raw_text: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Parses expressions like 'Ground + 14 floors' or 'G + 14' and '240 total units'.
    """
    if not raw_text:
        return None, None

    floors = None
    units = None

    # Floors: Ground + X or G + X
    g_match = re.search(r"\b(?:ground|g)\s*\+\s*(\d+)", raw_text, re.I)
    if g_match:
        floors = int(g_match.group(1)) + 1
    else:
        f_match = re.search(r"(\d+)\s*(?:floors?|storeys?|stories?)", raw_text, re.I)
        if f_match:
            floors = int(f_match.group(1))

    # Units
    u_match = re.search(r"(\d+)\s*(?:total\s+)?(?:units?|apartments?|flats?|villas?|residences?)", raw_text, re.I)
    if u_match:
        units = int(u_match.group(1))

    return floors, units


def disaggregate_unit_types(text: str) -> List[Dict[str, Any]]:
    """
    Extracts distinct unit configurations from multi-unit brochure tables or descriptions.
    e.g. Type A: 2 BHK, 1200 sqft, 80 Lakh
         Type B: 3 BHK, 1600 sqft, 1.1 Crore
    """
    configs: List[Dict[str, Any]] = []
    
    # Split by lines and scan for BHK rows
    lines = text.split("\n")
    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        bhk_m = re.search(r"\b([1-9])\s*(?:bhk|bed|bedroom)\b", line_clean, re.I)
        if bhk_m:
            beds = int(bhk_m.group(1))
            min_a, max_a, a_disp, a_type = parse_and_normalize_area(line_clean)
            min_p, max_p, p_disp, curr = parse_and_normalize_price(line_clean)

            configs.append({
                "unit_name": f"{beds} BHK",
                "bedrooms": beds,
                "area_sqm_min": min_a,
                "area_sqm_max": max_a,
                "area_display": a_disp,
                "price_numeric_min": min_p,
                "price_numeric_max": max_p,
                "price_display": p_disp,
                "currency": curr,
                "raw_line": line_clean
            })

    return configs

def normalize_percentage_rate(raw_val: Any) -> Optional[str]:
    """
    Normalizes ROI and Rental Yield expressions to clean percentage strings.
    e.g. 7 -> '7%', '8.5' -> '8.5%', '7-9%' -> '7-9%', 'up to 10%' -> '10%'
    """
    if raw_val is None:
        return None
    s = str(raw_val).strip()
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*%", s)
    if m:
        return f"{m.group(1)}%"
    m_num = re.search(r"(\d+(?:\.\d+)?)", s)
    if m_num:
        return f"{m_num.group(1)}%"
    return s


def generate_project_slug(name: Optional[str]) -> str:
    """
    Generates a clean URL slug for Polish or English project names.
    """
    if not name:
        return ""
    # Map Polish diacritics
    pl_trans = str.maketrans({
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'a', 'Ć': 'c', 'Ę': 'e', 'Ł': 'l', 'Ń': 'n',
        'Ó': 'o', 'Ś': 's', 'Ź': 'z', 'Ż': 'z'
    })
    cleaned = name.translate(pl_trans).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return slug[:120]


# Exact specification of Compulsory vs Optional fields
COMPULSORY_FIELDS = [
    ("project_name_pl", "Project Name (PL)"),
    ("project_name_en", "Project Name (EN)"),
    ("property_type", "Project Type"),
    ("country", "Country"),
    ("city", "City"),
    ("short_description_pl", "Short Description (PL)"),
    ("short_description_en", "Short Description (EN)"),
    ("media", "Media (at least 1 image/asset)"),
]

OPTIONAL_FIELDS = [
    # Basic info
    "slug_pl", "slug_en", "subtitle_pl", "subtitle_en", "websites",
    # Pricing
    "currency", "price_from", "price_to", "expected_roi", "rental_yield",
    # Details
    "completion_status", "completion_date", "total_units", "available_units",
    "area_from_sqm", "area_to_sqm", "amenities", "tags",
    # Descriptions
    "full_description_pl", "full_description_en",
    # Media
    "video_url",
    # Location
    "search_location", "latitude", "longitude", "address_en", "address_pl", "neighborhood_lifestyle",
    # Layouts
    "layouts",
    # SEO
    "seo_title_pl", "seo_title_en", "seo_description_pl", "seo_description_en",
    # Publish
    "active", "featured", "top_list"
]


def validate_project_schema(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validates that all compulsory project fields are present and non-empty.
    Returns (is_valid, list_of_missing_or_invalid_field_descriptions).
    """
    errors: List[str] = []
    
    # 1. Check Project Name (PL)
    if not (data.get("project_name_pl") or "").strip():
        errors.append("Project Name (PL) is required")
        
    # 2. Check Project Name (EN)
    if not (data.get("project_name_en") or data.get("property_name") or "").strip():
        errors.append("Project Name (EN) is required")
        
    # 3. Check Project Type
    if not (data.get("property_type") or "").strip():
        errors.append("Project Type is required (e.g. Villa, Condominium, Apartment, Townhouse)")
        
    # 4. Check Country
    if not (data.get("country") or "").strip():
        errors.append("Country is required")
        
    # 5. Check City
    city = data.get("city") or data.get("province") or data.get("district") or data.get("area")
    if not (city or "").strip():
        errors.append("City is required")
        
    # 6. Check Short Description (PL)
    if not (data.get("short_description_pl") or "").strip():
        errors.append("Short Description (PL) is required")
        
    # 7. Check Short Description (EN)
    if not (data.get("short_description_en") or "").strip():
        errors.append("Short Description (EN) is required")
        
    # 8. Check Media
    media = data.get("media") or data.get("images") or []
    if not media or len(media) == 0:
        errors.append("Media is required (must contain at least 1 image URL)")
        
    return len(errors) == 0, errors
