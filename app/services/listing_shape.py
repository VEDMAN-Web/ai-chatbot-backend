"""Make admin-bot listings match what the three websites read from Firestore `projects`.

The websites (varsovia-estate repo, src/types/index.ts `Project`) read localized {pl, en}
objects, real ids from `cities` / `countries` / `developers`, a lowercase property type and a
`coordinates` object. Missing or wrongly shaped fields have crashed or 404'd live pages before,
so a new listing gets every field with a safe value, and the city must be one the websites know.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Tuple[float, Any, Dict[str, Tuple[str, Dict[str, Any]]]]] = {}
_CACHE_TTL_SEC = 120

# Backend label (action_service.ALLOWED_PROPERTY_TYPES values) -> website PropertyType
WEBSITE_TYPE = {
    "villa": "villa", "house": "villa", "condominium": "condo", "condo": "condo",
    "apartment": "apartment", "townhouse": "townhouse", "penthouse": "penthouse",
    "land": "land", "hotel unit": "hotel_unit", "hotel_unit": "hotel_unit", "commercial": "condo",
}
# Website PropertyType -> backend label shown by the bots
TYPE_LABEL = {
    "villa": "Villa", "condo": "Condominium", "apartment": "Apartment", "townhouse": "Townhouse",
    "penthouse": "Penthouse", "land": "Land", "hotel_unit": "Hotel unit",
}
WEBSITE_CURRENCIES = {"USD", "THB", "EUR", "PLN", "VND"}
_COUNTRY_CURRENCY = {"thailand": "THB", "cambodia": "USD", "vietnam": "VND"}
# Used only when the city has no coordinates: the website map needs numbers, never 0,0.
_COUNTRY_CENTRE = {
    "thailand": (13.7563, 100.5018), "cambodia": (11.5564, 104.9282), "vietnam": (10.8231, 106.6297),
}
_LOCALIZED = ("name", "slug", "subtitle", "description", "shortDescription", "address", "seoTitle", "seoDescription")

# Place words staff and imports use -> website city / country id. Only used when that id exists.
CITY_ALIASES = {
    "samui": "samui", "koh-samui": "samui", "ko-samui": "samui", "lamai": "samui", "bophut": "samui",
    "bo-phut": "samui", "chaweng": "samui", "maenam": "samui", "mae-nam": "samui", "choeng-mon": "samui",
    "lipa-noi": "samui", "bang-por": "samui", "bang-po": "samui", "taling-ngam": "samui", "nathon": "samui",
    "na-thon": "samui", "bang-rak": "samui", "plai-laem": "samui", "bang-kao": "samui", "hua-thanon": "samui",
    "phuket": "phuket", "phuket-town": "phuket", "kathu": "phuket", "patong": "phuket", "kamala": "phuket",
    "rawai": "phuket", "chalong": "phuket", "karon": "phuket", "kata": "phuket", "kata-noi": "phuket",
    "bang-tao": "phuket", "bangtao": "phuket", "cherngtalay": "phuket", "choeng-thale": "phuket",
    "thalang": "phuket", "mai-khao": "phuket", "nai-harn": "phuket", "nai-yang": "phuket",     "layan": "phuket", "cape-yamu": "phuket", "wichit": "phuket", "koh-kaew": "phuket",
    "bangkok": "bangkok", "krung-thep": "bangkok", "bkk": "bangkok",     "silom": "bangkok", "sathorn": "bangkok", "thonglor": "bangkok", "thong-lo": "bangkok",
    "ekkamai": "bangkok", "asok": "bangkok", "asoke": "bangkok", "phrom-phong": "bangkok",     "ratchada": "bangkok", "ratchadaphisek": "bangkok", "bang-na": "bangkok",
    "pattaya": "pattaya", "jomtien": "pattaya", "naklua": "pattaya", "na-kluea": "pattaya",
    "pratumnak": "pattaya", "wongamat": "pattaya", "na-jomtien": "pattaya", "bang-saray": "pattaya",
    "phnom-penh": "phnom-penh", "bkk1": "phnom-penh", "boeung-keng-kang": "phnom-penh",
    "chamkarmon": "phnom-penh", "daun-penh": "phnom-penh", "tonle-bassac": "phnom-penh",
    "toul-kork": "phnom-penh", "siem-reap": "siem-reap", "sihanoukville": "sihanoukville",
    "kampong-som": "sihanoukville", "preah-sihanouk": "sihanoukville",
    "ho-chi-minh": "ho-chi-minh", "ho-chi-minh-city": "ho-chi-minh", "hcmc": "ho-chi-minh",
    "saigon": "ho-chi-minh", "hanoi": "hanoi", "ha-noi": "hanoi", "da-nang": "da-nang", "danang": "da-nang",
    # Pattaya's province and district, Polish spellings, common typos
    "chonburi": "pattaya", "chon-buri": "pattaya", "bang-lamung": "pattaya", "banglamung": "pattaya",
    "pattaja": "pattaya", "puket": "phuket", "bangkoku": "bangkok",
    "krung-thep-maha-nakhon": "bangkok", "nai-thon": "phuket", "ao-por": "phuket", "ao-po": "phuket",
    "sihanouk-ville": "sihanoukville", "cherng-talay": "phuket",
    "ko-pha-ngan": "phangan", "koh-pha-ngan": "phangan", "pha-ngan": "phangan", "koh-phangan": "phangan",
}
# Place names written in Thai script (slugify would drop them)
THAI_SCRIPT_ALIASES = {
    "เกาะสมุย": "samui", "สมุย": "samui", "ภูเก็ต": "phuket", "กรุงเทพ": "bangkok", "พัทยา": "pattaya",
    "ชลบุรี": "pattaya",
}
# Words that describe a place but are not its name ("Lamai Beach", "Phuket Island", "North Pattaya")
_NOISE = {"beach", "city", "island", "town", "province", "north", "south", "east", "west", "central",
          "district", "area", "bay", "road", "the", "thailand", "tajlandia", "metropolis", "wyspa"}
COUNTRY_ALIASES = {
    "thailand": "thailand", "tajlandia": "thailand", "thai": "thailand", "th": "thailand",
    "cambodia": "cambodia", "kambodza": "cambodia", "kh": "cambodia", "malediwy": "maldives",
    "vietnam": "vietnam", "viet-nam": "vietnam", "wietnam": "vietnam", "vn": "vietnam",
}
_EXTRA_LETTERS = str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ø": "o", "ß": "ss"})


def slugify(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").translate(_EXTRA_LETTERS))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _labels(doc_id: str, data: Dict[str, Any]) -> List[str]:
    out = [slugify(doc_id)]
    for key in ("name", "slug", "title"):
        value = data.get(key)
        for v in (value.values() if isinstance(value, dict) else [value]):
            if v:
                out.append(slugify(v))
    return [x for x in out if x]


def place_index(client: Any, collection: str) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    """{label: (doc id, doc)} for a small reference collection. Empty if it cannot be read."""
    key = f"{id(client)}:{collection}"
    hit = _CACHE.get(key)
    if hit and hit[1] is client and time.time() - hit[0] < _CACHE_TTL_SEC:
        return hit[2]
    index: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    try:
        for snap in client.collection(collection).stream():
            data = snap.to_dict() or {}
            for label in _labels(snap.id, data):
                index.setdefault(label, (snap.id, data))
    except Exception as exc:  # never block a save on a lookup
        logger.warning("Could not read %s for listing ids: %s", collection, exc)
        return {}
    _CACHE[key] = (time.time(), client, index)
    return index


_PLACE_KEYS = ("city", "province", "location", "area", "district", "subdistrict", "address")
_SPLIT = re.compile(r"[,/;()|]| - ")


def _place_words(fields: Dict[str, Any]) -> List[str]:
    """Place texts, the explicit city/province first, then area/district, then the address."""
    words: List[str] = []
    for key in _PLACE_KEYS:
        value = fields.get(key)
        if isinstance(value, dict):
            value = value.get("en") or value.get("pl") or ""
        if value:
            words.append(str(value))
    return words


def _candidates(text: str) -> List[str]:
    """Slugs to try for one place text: the whole text, each part, then each run of words."""
    out: List[str] = []
    for part in [text] + [p for p in _SPLIT.split(text) if p.strip()]:
        tokens = [t for t in slugify(part).split("-") if t]
        cleaned = [t for t in tokens if t not in _NOISE]
        for seq in (tokens, cleaned):
            if seq:
                out.append("-".join(seq))
        for size in (3, 2, 1):  # "Bophut Beach Koh Samui" -> "koh-samui", "bophut"
            for i in range(0, max(len(cleaned) - size + 1, 0)):
                out.append("-".join(cleaned[i:i + size]))
    return [c for c in dict.fromkeys(out) if c]


def _match(cities: Dict[str, Tuple[str, Dict[str, Any]]], text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    for thai, city_id in THAI_SCRIPT_ALIASES.items():
        if thai in text and city_id in cities:
            return cities[city_id]
    for cand in _candidates(text):
        for key in (cand, CITY_ALIASES.get(cand), cand[4:] if cand.startswith("koh-") else None,
                    cand[3:] if cand.startswith("ko-") else None):
            if key and key in cities:
                return cities[key]
    return None


def find_city(client: Any, fields: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    cities = place_index(client, "cities")
    if not cities:
        return None
    for word in _place_words(fields):
        hit = _match(cities, word)
        if hit:
            return hit
    return None


def city_problem(client: Any, fields: Dict[str, Any]) -> Optional[str]:
    """Message for staff when a NEW listing's place is not a city the websites know, else None.

    Returns None when the city list cannot be read, so an outage never blocks saving.
    """
    cities = place_index(client, "cities")
    if not cities or find_city(client, fields):
        return None
    names = sorted({
        (doc.get("name") or {}).get("en") if isinstance(doc.get("name"), dict) else str(doc.get("name") or cid)
        for cid, doc in cities.values() if doc.get("isActive", True) is not False
    } - {None, ""})
    place = ", ".join(_place_words(fields)) or "(no location given)"
    return (f"The location '{place}' is not a city the websites know, so the listing would not show. "
            f"Tell me the city. Known cities: {', '.join(names)}.")


def resolve_place_ids(client: Any, fields: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """cityId / countryId / developerId the websites use. Only sets what it can match."""
    out: Dict[str, Any] = {}
    city = find_city(client, fields) if any(fields.get(k) for k in _PLACE_KEYS) else None
    if city:
        out["cityId"] = city[0]
        if city[1].get("countryId"):
            out["countryId"] = str(city[1]["countryId"])
    if "countryId" not in out and not existing.get("countryId") and fields.get("country"):
        countries = place_index(client, "countries")
        slug = slugify(fields["country"])
        for candidate in (slug, COUNTRY_ALIASES.get(slug)):
            if candidate and candidate in countries:
                out["countryId"] = countries[candidate][0]
                break
    dev = fields.get("developer_name")
    if dev:
        hit = place_index(client, "developers").get(slugify(dev))
        if hit:
            out["developerId"] = hit[0]
    return out


def website_type(value: Any) -> Optional[str]:
    key = str(value or "").strip().lower().replace("_", " ")
    if key in TYPE_LABEL:
        return key if key != "hotel unit" else "hotel_unit"
    return WEBSITE_TYPE.get(key)


def type_label(doc: Dict[str, Any]) -> Optional[str]:
    """What the bots show for a stored type: the saved label, else the website value mapped back."""
    label = doc.get("propertyTypeLabel")
    raw = doc.get("propertyType")
    if label and (raw is None or website_type(label) == str(raw).strip().lower()):
        return str(label)
    if raw is None:
        return None
    return TYPE_LABEL.get(str(raw).strip().lower(), str(raw))


def completion_status(status: Any) -> str:
    s = str(status or "").strip().lower()
    if "construction" in s:
        return "under_construction"
    if "off" in s or "pre" in s:
        return "pre_sale"
    if "plan" in s:
        return "planned"
    return "ready"


def localized(value: Any) -> Dict[str, str]:
    if isinstance(value, dict):
        pl = str(value.get("pl") or value.get("en") or "")
        en = str(value.get("en") or value.get("pl") or "")
        return {**{k: str(v or "") for k, v in value.items()}, "pl": pl, "en": en}
    text = "" if value is None else str(value)
    return {"pl": text, "en": text}


def merge_localized(existing: Any, new_text: Any) -> Any:
    """Update of a {pl, en} field with plain text keeps the object shape (sets both languages)."""
    if isinstance(existing, dict) and not isinstance(new_text, dict):
        merged = dict(existing)
        merged["en"] = str(new_text or "")
        merged["pl"] = str(new_text or "")
        return merged
    return new_text


def coordinates_for(city: Optional[Tuple[str, Dict[str, Any]]], country_id: str) -> Dict[str, float]:
    coords = (city[1].get("coordinates") if city else None) or {}
    try:
        lat, lng = float(coords.get("lat")), float(coords.get("lng"))
        if lat or lng:
            return {"lat": lat, "lng": lng}
    except (TypeError, ValueError, AttributeError):
        pass
    lat, lng = _COUNTRY_CENTRE.get(country_id, _COUNTRY_CENTRE["thailand"])
    return {"lat": lat, "lng": lng}


def apply_new_listing_shape(client: Any, doc: Dict[str, Any], fields: Dict[str, Any], doc_id: str) -> None:
    """Fill every website field of a NEW listing with a correctly shaped, non-empty-safe value."""
    name = doc.get("name") or doc.get("title") or fields.get("property_name") or doc_id
    doc["name"] = localized(name)
    doc["slug"] = localized(doc.get("slug") or slugify(doc_id) or doc_id)
    if doc.get("address") in (None, "") and (fields.get("address") or fields.get("district")):
        doc["address"] = fields.get("address") or fields.get("district")
    for key in _LOCALIZED:
        doc[key] = localized(doc.get(key)) if key in doc else {"pl": "", "en": ""}

    city = find_city(client, {**fields, "city": doc.get("cityId")}) if doc.get("cityId") or fields else None
    if city:
        doc["cityId"] = city[0]
        doc.setdefault("countryId", str(city[1].get("countryId") or ""))
    for key in ("countryId", "cityId", "developerId", "completionDate", "videoUrl", "paymentPlan", "coverImage"):
        doc[key] = str(doc.get(key) or "")

    def _num(v: Any) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    if not doc.get("priceFrom") and fields.get("price_numeric_min"):
        doc["priceFrom"] = fields["price_numeric_min"]
    if not doc.get("priceFrom"):
        text = doc.get("priceDisplay") or fields.get("price_display") or fields.get("price")
        try:
            from app.services.admin_property_fields import parse_price_amount
            number = parse_price_amount(text) if text not in (None, "") else None
        except Exception:
            number = None
        if number and number > 0:
            doc["priceFrom"] = number
    if not doc.get("priceTo") and fields.get("price_numeric_max"):
        doc["priceTo"] = fields["price_numeric_max"]
    if not doc.get("areaFrom") and fields.get("area_sqm_min"):
        doc["areaFrom"] = fields["area_sqm_min"]
    if not doc.get("areaTo") and fields.get("area_sqm_max"):
        doc["areaTo"] = fields["area_sqm_max"]
    for key in ("priceFrom", "priceTo", "pricePerSqm", "totalUnits", "availableUnits", "areaFrom", "areaTo",
                "expectedROI", "rentalYield"):
        doc[key] = _num(doc.get(key))
    if not doc["priceFrom"]:
        doc["priceDisplay"] = doc.get("priceDisplay") or fields.get("price_display") or "Price on Request"
        doc["priceOnRequest"] = True
    for key in ("images", "amenities", "tags", "layouts"):
        doc[key] = list(doc.get(key) or [])
    for key in ("isFeatured", "isTopList"):
        doc[key] = bool(doc.get(key) or False)
    if "isActive" not in doc:
        doc["isActive"] = True

    raw_type = doc.get("propertyType") or fields.get("property_type") or fields.get("type")
    doc["propertyType"] = website_type(raw_type) or "condo"
    if raw_type and not doc.get("propertyTypeLabel"):
        doc["propertyTypeLabel"] = str(raw_type)
    if doc.get("completionStatus") not in ("ready", "under_construction", "pre_sale", "planned"):
        doc["completionStatus"] = completion_status(doc.get("status") or fields.get("status"))
    currency = str(fields.get("currency") or doc.get("currency") or "").upper()
    doc["currency"] = currency if currency in WEBSITE_CURRENCIES else _COUNTRY_CURRENCY.get(doc["countryId"], "THB")
    if not isinstance(doc.get("coordinates"), dict) or not doc["coordinates"].get("lat"):
        doc["coordinates"] = coordinates_for(city, doc["countryId"])
