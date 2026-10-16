"""
Brochure data model and honest loaders (PostgreSQL then Firestore).

Never invents prices, specs, descriptions, or marketing copy.
Missing fields stay None or empty lists. Multilingual dicts resolve to
the requested language with fallback to en, then any available value.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.tenant import resolve_tenant

logger = logging.getLogger(__name__)

LangValue = Union[str, Dict[str, Any], List[Any], None]


# ---------------------------------------------------------------------------
# Display label dictionaries (en / pl / th). Unknown tags -> title case.
# ---------------------------------------------------------------------------

STATUS_LABELS: Dict[str, Dict[str, str]] = {
    "under_construction": {
        "en": "Under construction",
        "pl": "W budowie",
        "th": "กำลังก่อสร้าง",
    },
    "under-construction": {
        "en": "Under construction",
        "pl": "W budowie",
        "th": "กำลังก่อสร้าง",
    },
    "pre_sale": {"en": "Pre-sale", "pl": "Przedsprzedaż", "th": "พรีเซล"},
    "pre-sale": {"en": "Pre-sale", "pl": "Przedsprzedaż", "th": "พรีเซล"},
    "presale": {"en": "Pre-sale", "pl": "Przedsprzedaż", "th": "พรีเซล"},
    "ready_to_move_in": {
        "en": "Ready to move in",
        "pl": "Gotowe do zamieszkania",
        "th": "พร้อมเข้าอยู่",
    },
    "ready-to-move-in": {
        "en": "Ready to move in",
        "pl": "Gotowe do zamieszkania",
        "th": "พร้อมเข้าอยู่",
    },
    "available": {"en": "Available", "pl": "Dostępne", "th": "พร้อมขาย"},
    "sold_out": {"en": "Sold out", "pl": "Wyprzedane", "th": "ขายหมดแล้ว"},
    "sold-out": {"en": "Sold out", "pl": "Wyprzedane", "th": "ขายหมดแล้ว"},
    "off_plan": {"en": "Off-plan", "pl": "W ofercie deweloperskiej", "th": "ออฟแพลน"},
    "off-plan": {"en": "Off-plan", "pl": "W ofercie deweloperskiej", "th": "ออฟแพลน"},
    "completed": {"en": "Completed", "pl": "Ukończone", "th": "สร้างเสร็จแล้ว"},
}

AMENITY_LABELS: Dict[str, Dict[str, str]] = {
    "pool": {"en": "Swimming pool", "pl": "Basen", "th": "สระว่ายน้ำ"},
    "swimming_pool": {"en": "Swimming pool", "pl": "Basen", "th": "สระว่ายน้ำ"},
    "swimming-pool": {"en": "Swimming pool", "pl": "Basen", "th": "สระว่ายน้ำ"},
    "plavatelnyy-basseyn": {"en": "Swimming pool", "pl": "Basen", "th": "สระว่ายน้ำ"},
    "plavatelnyy_basseyn": {"en": "Swimming pool", "pl": "Basen", "th": "สระว่ายน้ำ"},
    "gym": {"en": "Gym", "pl": "Siłownia", "th": "ฟิตเนส"},
    "fitness": {"en": "Fitness centre", "pl": "Centrum fitness", "th": "ฟิตเนส"},
    "parking": {"en": "Parking", "pl": "Parking", "th": "ที่จอดรถ"},
    "security": {"en": "24h security", "pl": "Ochrona 24h", "th": "รปภ. 24 ชม."},
    "garden": {"en": "Garden", "pl": "Ogród", "th": "สวน"},
    "sea_view": {"en": "Sea view", "pl": "Widok na morze", "th": "วิวทะเล"},
    "sea-view": {"en": "Sea view", "pl": "Widok na morze", "th": "วิวทะเล"},
    "beach_access": {"en": "Beach access", "pl": "Dostęp do plaży", "th": "ใกล้ชายหาด"},
    "beach-access": {"en": "Beach access", "pl": "Dostęp do plaży", "th": "ใกล้ชายหาด"},
    "concierge": {"en": "Concierge", "pl": "Concierge", "th": "คอนเซียร์จ"},
    "spa": {"en": "Spa", "pl": "Spa", "th": "สปา"},
    "restaurant": {"en": "Restaurant", "pl": "Restauracja", "th": "ร้านอาหาร"},
    "kids_club": {"en": "Kids club", "pl": "Klub dla dzieci", "th": "คลับเด็ก"},
    "kids-club": {"en": "Kids club", "pl": "Klub dla dzieci", "th": "คลับเด็ก"},
    "coworking": {"en": "Co-working", "pl": "Coworking", "th": "โคเวิร์คกิ้ง"},
    "elevator": {"en": "Elevator", "pl": "Winda", "th": "ลิฟต์"},
    "balcony": {"en": "Balcony", "pl": "Balkon", "th": "ระเบียง"},
    "terrace": {"en": "Terrace", "pl": "Taras", "th": "ระเบียงนอกชาน"},
    "wifi": {"en": "Wi-Fi", "pl": "Wi-Fi", "th": "ไวไฟ"},
    "air_conditioning": {
        "en": "Air conditioning",
        "pl": "Klimatyzacja",
        "th": "แอร์",
    },
    "air-conditioning": {
        "en": "Air conditioning",
        "pl": "Klimatyzacja",
        "th": "แอร์",
    },
}

OWNERSHIP_LABELS: Dict[str, Dict[str, str]] = {
    "freehold": {"en": "Freehold", "pl": "Freehold", "th": "กรรมสิทธิ์ Freehold"},
    "leasehold": {"en": "Leasehold", "pl": "Leasehold", "th": "สิทธิเช่าระยะยาว"},
    "condo_freehold": {
        "en": "Foreign freehold condominium",
        "pl": "Freehold dla cudzoziemców (condo)",
        "th": "คอนโดฟรีโฮลด์สำหรับชาวต่างชาติ",
    },
    "company_owned": {
        "en": "Thai company ownership",
        "pl": "Własność spółki tajskiej",
        "th": "ถือครองผ่านบริษัทไทย",
    },
}


# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------

class BrochureNearbyPlace(BaseModel):
    category: Optional[str] = None
    name: Optional[str] = None
    distance_text: Optional[str] = None


class BrochureUnitType(BaseModel):
    name: Optional[str] = None
    size_text: Optional[str] = None
    beds: Optional[Union[int, str]] = None
    baths: Optional[Union[int, str]] = None
    layout_image: Optional[str] = None
    units: Optional[Union[int, str]] = None


class BrochureContact(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    brand_name: Optional[str] = None
    website_url: Optional[str] = None
    logo_path: Optional[str] = None


class BrochureTenantInfo(BaseModel):
    site_origin: Optional[str] = None
    brand_name: Optional[str] = None
    domain: Optional[str] = None
    advisor_email: Optional[str] = None
    website_url: Optional[str] = None
    logo_path: Optional[str] = None


class BrochureData(BaseModel):
    """All fields optional. Missing data stays None / []. Never invent values."""

    property_id: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    tenant: Optional[BrochureTenantInfo] = None
    language: Optional[str] = None
    hero_image: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    location_text: Optional[str] = None
    district: Optional[str] = None
    province: Optional[str] = None
    country: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    developer_name: Optional[str] = None
    developer_logo: Optional[str] = None
    description: Optional[str] = None
    highlights: List[str] = Field(default_factory=list)
    facilities: List[str] = Field(default_factory=list)
    amenities: List[str] = Field(default_factory=list)
    nearby_places: List[BrochureNearbyPlace] = Field(default_factory=list)
    property_type: Optional[str] = None
    status: Optional[str] = None
    ownership_type: Optional[str] = None
    price_display: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    currency: Optional[str] = None
    bedrooms_display: Optional[str] = None
    bathrooms: Optional[Union[int, str]] = None
    area_display: Optional[str] = None
    units: Optional[Union[int, str]] = None
    total_floors: Optional[Union[int, str]] = None
    completion: Optional[str] = None
    parking: Optional[str] = None
    unit_types: List[BrochureUnitType] = Field(default_factory=list)
    floor_plans: List[str] = Field(default_factory=list)
    master_plan: List[str] = Field(default_factory=list)
    contact: Optional[BrochureContact] = None
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers: multilingual resolve, labels, sanitisation
# ---------------------------------------------------------------------------

def _normalize_lang(language: Optional[str]) -> str:
    lang = (language or "en").strip().lower()
    if lang in ("en", "pl", "th"):
        return lang
    return "en"


def resolve_localized(
    value: LangValue,
    language: str = "en",
    *,
    as_list: bool = False,
) -> Optional[Union[str, List[str]]]:
    """
    Resolve multilingual values. Never invents content.
    Dict keys tried: requested lang, en, pl, th, then first non-empty value.
    """
    if value is None:
        return [] if as_list else None

    lang = _normalize_lang(language)

    if isinstance(value, dict):
        for key in (lang, "en", "pl", "th"):
            if key in value and value[key] not in (None, "", []):
                return resolve_localized(value[key], language, as_list=as_list)
        for v in value.values():
            if v not in (None, "", []):
                return resolve_localized(v, language, as_list=as_list)
        return [] if as_list else None

    if isinstance(value, (list, tuple)):
        items: List[str] = []
        for item in value:
            resolved = resolve_localized(item, language, as_list=False)
            if resolved:
                items.append(str(resolved).strip())
        return items if as_list else (", ".join(items) if items else None)

    text = str(value).strip()
    if not text:
        return [] if as_list else None
    return [text] if as_list else text


def _slug_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")


def _title_case_slug(raw: str) -> str:
    """Humanise unknown slug/enum without inventing meaning."""
    text = str(raw).strip()
    if not text:
        return ""
    # Already readable (has spaces or mixed case words)
    if " " in text and not re.fullmatch(r"[A-Z0-9_\-]+", text):
        return text
    cleaned = re.sub(r"[_\-]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() if cleaned else ""


def localize_label(
    raw: Optional[str],
    language: str,
    table: Dict[str, Dict[str, str]],
) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    lang = _normalize_lang(language)
    key = _slug_key(text)
    # Direct and common enum forms
    for candidate in (key, text.lower(), text.upper(), text.replace(" ", "_").lower()):
        labels = table.get(candidate)
        if labels:
            return labels.get(lang) or labels.get("en") or text
    # Already human sentence
    if " " in text and not re.fullmatch(r"[A-Z0-9_\-]+", text) and "_" not in text and "-" not in text:
        return text
    return _title_case_slug(text) or None


def localize_tag_list(
    values: Optional[List[Any]],
    language: str,
    table: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in values or []:
        resolved = resolve_localized(item, language)
        if not resolved:
            continue
        label = (
            localize_label(str(resolved), language, table)
            if table
            else _title_case_slug(str(resolved))
        )
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int_or_str(val: Any) -> Optional[Union[int, str]]:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    try:
        f = float(val)
        if f == int(f):
            return int(f)
        return str(val).strip()
    except (TypeError, ValueError):
        text = str(val).strip()
        return text or None


def _image_url(item: Any) -> Optional[str]:
    if item is None:
        return None
    if isinstance(item, str):
        url = item.strip()
        return url or None
    if isinstance(item, dict):
        for key in ("image_url", "url", "src", "href", "image"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    url = getattr(item, "image_url", None)
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _collect_images(raw_list: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in raw_list or []:
        url = _image_url(item)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _format_price_display(
    price_min: Optional[float],
    price_max: Optional[float],
    currency: Optional[str],
    existing_display: Optional[str] = None,
) -> Optional[str]:
    """Build price_display only from real numbers. Never invents 'Price on request'."""
    if existing_display and str(existing_display).strip():
        text = str(existing_display).strip()
        # Reject known invented placeholders from older normalisers
        if text.lower() in ("price on request", "n/a", "na", "-"):
            pass
        else:
            return text
    cur = (currency or "THB").strip() or "THB"
    if price_min is not None and price_max is not None and price_min != price_max:
        return f"{price_min:,.0f} - {price_max:,.0f} {cur}"
    if price_min is not None:
        if price_min >= 1_000_000:
            return f"from {price_min / 1_000_000:.2f}M {cur}"
        return f"from {price_min:,.0f} {cur}"
    if price_max is not None:
        return f"up to {price_max:,.0f} {cur}"
    return None


def _build_location_text(
    district: Optional[str],
    province: Optional[str],
    country: Optional[str],
    address: Optional[str] = None,
) -> Optional[str]:
    parts = [p for p in [address, district, province, country] if p and str(p).strip()]
    # Deduplicate while preserving order
    seen = set()
    clean: List[str] = []
    for p in parts:
        key = str(p).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(str(p).strip())
    return ", ".join(clean) if clean else None


def _parse_nearby(raw: Any, language: str) -> List[BrochureNearbyPlace]:
    places: List[BrochureNearbyPlace] = []
    if not raw:
        return places
    items = raw
    if isinstance(raw, dict):
        # {category: [names]} or {category: [{name, distance}]}
        items = []
        for cat, vals in raw.items():
            cat_label = localize_label(str(cat), language, AMENITY_LABELS) or _title_case_slug(str(cat))
            if isinstance(vals, list):
                for v in vals:
                    items.append({"category": cat_label, **(v if isinstance(v, dict) else {"name": v})})
            elif vals:
                items.append({"category": cat_label, "name": vals})

    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            name = resolve_localized(item, language)
            if name:
                places.append(BrochureNearbyPlace(name=str(name)))
            continue
        if not isinstance(item, dict):
            continue
        name = resolve_localized(
            item.get("name") or item.get("title") or item.get("place"),
            language,
        )
        if not name:
            continue
        cat = resolve_localized(item.get("category") or item.get("type"), language)
        cat_label = (
            localize_label(str(cat), language, AMENITY_LABELS) or _title_case_slug(str(cat))
            if cat
            else None
        )
        dist = resolve_localized(
            item.get("distance_text") or item.get("distance") or item.get("dist"),
            language,
        )
        places.append(
            BrochureNearbyPlace(
                category=cat_label,
                name=str(name),
                distance_text=str(dist) if dist else None,
            )
        )
    return places


def _parse_unit_types(raw: Any, language: str) -> List[BrochureUnitType]:
    out: List[BrochureUnitType] = []
    if not raw or not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = resolve_localized(
            item.get("name") or item.get("type") or item.get("title"),
            language,
        )
        size = resolve_localized(
            item.get("size_text") or item.get("area") or item.get("size"),
            language,
        )
        beds = item.get("beds") if item.get("beds") is not None else item.get("bedrooms")
        baths = item.get("baths") if item.get("baths") is not None else item.get("bathrooms")
        layout = _image_url(item.get("layout_image") or item.get("image") or item.get("floorPlan"))
        units = item.get("units") if item.get("units") is not None else item.get("count")
        if not any([name, size, beds, baths, layout, units]):
            continue
        out.append(
            BrochureUnitType(
                name=str(name) if name else None,
                size_text=str(size) if size else None,
                beds=_safe_int_or_str(beds),
                baths=_safe_int_or_str(baths),
                layout_image=layout,
                units=_safe_int_or_str(units),
            )
        )
    return out


def _tenant_bundle(site_origin: Optional[str]) -> tuple[BrochureTenantInfo, BrochureContact]:
    tenant = resolve_tenant(site_origin)
    logo_path = None
    try:
        from app.services.offer_dossier_service import resolve_logo_path
        logo_path = resolve_logo_path(tenant.brand_name)
    except Exception:
        logo_path = None

    info = BrochureTenantInfo(
        site_origin=getattr(tenant, "site_origin", None),
        brand_name=getattr(tenant, "brand_name", None),
        domain=getattr(tenant, "domain", None),
        advisor_email=getattr(tenant, "advisor_email", None) or None,
        website_url=getattr(tenant, "website_url", None),
        logo_path=logo_path,
    )
    contact = BrochureContact(
        email=info.advisor_email,
        phone=None,  # Tenant profile has no phone; never invent
        brand_name=info.brand_name,
        website_url=info.website_url,
        logo_path=logo_path,
    )
    return info, contact


# ---------------------------------------------------------------------------
# PostgreSQL mapper
# ---------------------------------------------------------------------------

def _from_postgres(prop: Any, language: str, site_origin: Optional[str]) -> BrochureData:
    lang = _normalize_lang(language)
    tenant_info, contact = _tenant_bundle(site_origin)

    images = _collect_images(getattr(prop, "images", None) or [])
    # Prefer exterior / gallery before floor plans for hero
    hero = images[0] if images else None

    floor_plans: List[str] = []
    gallery: List[str] = []
    for img in getattr(prop, "images", None) or []:
        url = _image_url(img)
        if not url:
            continue
        itype = str(getattr(img, "image_type", "") or "").lower()
        if "floor" in itype:
            if url not in floor_plans:
                floor_plans.append(url)
        else:
            if url not in gallery:
                gallery.append(url)
    if not gallery:
        gallery = list(images)

    district = getattr(prop, "district", None) or None
    province = getattr(prop, "province", None) or None
    country = getattr(prop, "country", None) or None
    if country is not None:
        country = str(country).strip() or None

    price_min = _safe_float(getattr(prop, "price_numeric_min", None))
    price_max = _safe_float(getattr(prop, "price_numeric_max", None))
    currency = getattr(prop, "currency", None) or None
    price_display = _format_price_display(
        price_min,
        price_max,
        currency,
        existing_display=getattr(prop, "price_display", None),
    )

    beds_disp = getattr(prop, "bedrooms_display", None)
    if not beds_disp:
        bmin = getattr(prop, "bedrooms_min", None)
        bmax = getattr(prop, "bedrooms_max", None)
        if bmin is not None and bmax is not None and bmin != bmax:
            beds_disp = f"{bmin}-{bmax}"
        elif bmin is not None:
            beds_disp = str(bmin)
        elif bmax is not None:
            beds_disp = str(bmax)

    area_disp = getattr(prop, "area_display", None)
    if not area_disp:
        amin = getattr(prop, "area_sqm_min", None)
        amax = getattr(prop, "area_sqm_max", None)
        if amin is not None and amax is not None and amin != amax:
            area_disp = f"{amin}-{amax} sqm"
        elif amin is not None:
            area_disp = f"{amin} sqm"
        elif amax is not None:
            area_disp = f"{amax} sqm"

    meta = getattr(prop, "raw_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    completion = resolve_localized(
        meta.get("completion")
        or meta.get("completion_date")
        or meta.get("completionDate"),
        lang,
    )
    parking = resolve_localized(meta.get("parking") or meta.get("parking_text"), lang)

    master_plan = _collect_images(meta.get("master_plan") or meta.get("masterPlan") or [])
    unit_types = _parse_unit_types(meta.get("unit_types") or meta.get("layouts") or [], lang)

    updated = getattr(prop, "updated_at", None)
    updated_at = None
    if isinstance(updated, datetime):
        updated_at = updated.isoformat()
    elif updated:
        updated_at = str(updated)

    return BrochureData(
        property_id=str(getattr(prop, "property_id", "") or "").strip() or None,
        name=str(getattr(prop, "property_name", "") or "").strip() or None,
        slug=None,
        tenant=tenant_info,
        language=lang,
        hero_image=hero,
        images=gallery,
        location_text=_build_location_text(
            district,
            province,
            country,
            address=getattr(prop, "address", None),
        ),
        district=str(district).strip() if district else None,
        province=str(province).strip() if province else None,
        country=country,
        lat=_safe_float(getattr(prop, "latitude", None)),
        lng=_safe_float(getattr(prop, "longitude", None)),
        developer_name=(
            str(getattr(prop, "developer_name", "") or "").strip() or None
        ),
        developer_logo=None,
        description=resolve_localized(getattr(prop, "description", None), lang),
        highlights=localize_tag_list(getattr(prop, "highlights", None) or [], lang),
        facilities=localize_tag_list(
            getattr(prop, "facilities", None) or [], lang, AMENITY_LABELS
        ),
        amenities=localize_tag_list(
            getattr(prop, "amenities", None) or [], lang, AMENITY_LABELS
        ),
        nearby_places=_parse_nearby(getattr(prop, "nearby_places", None), lang),
        property_type=_title_case_slug(getattr(prop, "property_type", None) or "")
        or None,
        status=localize_label(getattr(prop, "status", None), lang, STATUS_LABELS),
        ownership_type=localize_label(
            getattr(prop, "ownership_type", None), lang, OWNERSHIP_LABELS
        ),
        price_display=price_display,
        price_min=price_min,
        price_max=price_max,
        currency=str(currency).strip() if currency else None,
        bedrooms_display=str(beds_disp).strip() if beds_disp else None,
        bathrooms=_safe_int_or_str(getattr(prop, "bathrooms", None)),
        area_display=str(area_disp).strip() if area_disp else None,
        units=_safe_int_or_str(getattr(prop, "units", None)),
        total_floors=_safe_int_or_str(getattr(prop, "total_floors", None)),
        completion=str(completion) if completion else None,
        parking=str(parking) if parking else None,
        unit_types=unit_types,
        floor_plans=floor_plans,
        master_plan=master_plan,
        contact=contact,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Firestore mapper (uses raw document; never uses invented normaliser strings)
# ---------------------------------------------------------------------------

_INVENTED_DESC_MARKERS = (
    "featuring",
    "with pricing starting from",
    "exclusive development",
)


def _is_invented_normaliser_description(desc: str, name: str, location: str) -> bool:
    d = (desc or "").lower()
    if "exclusive development" in d and "featuring" in d and "pricing starting" in d:
        return True
    return False


def _from_firestore_card(card: Dict[str, Any], language: str, site_origin: Optional[str]) -> BrochureData:
    lang = _normalize_lang(language)
    tenant_info, contact = _tenant_bundle(site_origin)
    raw = card.get("raw_firestore_data") if isinstance(card.get("raw_firestore_data"), dict) else {}
    # Prefer raw document fields for honesty
    src = raw if raw else card

    name = resolve_localized(
        src.get("name") or src.get("title") or src.get("projectName") or card.get("name"),
        lang,
    )
    # Reject doc-id fallbacks that look like slugs when name missing
    if name and name == card.get("property_id") and "-" in str(name) and " " not in str(name):
        # Keep slug-like id only if no better name; still OK as identifier display
        pass

    slug_val = resolve_localized(src.get("slug"), lang)
    if not slug_val and isinstance(src.get("slug"), str):
        slug_val = src.get("slug")

    images = _collect_images(src.get("images") or card.get("images") or [])
    cover = _image_url(src.get("coverImage") or src.get("cover_image") or card.get("hero_image"))
    if cover and cover not in images:
        images.insert(0, cover)
    hero = cover or (images[0] if images else None)

    district = resolve_localized(src.get("cityId") or src.get("city") or card.get("district"), lang)
    # Avoid using country as province (legacy normaliser bug)
    province = resolve_localized(src.get("province") or src.get("region"), lang)
    country = resolve_localized(src.get("countryId") or src.get("country") or card.get("province"), lang)
    if country and str(country).lower() in ("thailand", "tajlandia", "th"):
        country = "Thailand" if lang != "pl" else "Tajlandia"
    elif country:
        country = _title_case_slug(str(country))

    address = resolve_localized(src.get("address"), lang)
    location_text = _build_location_text(
        str(district) if district else None,
        str(province) if province else None,
        str(country) if country else None,
        address=str(address) if address else None,
    )

    coords = src.get("coordinates") or {}
    lat = lng = None
    if isinstance(coords, dict):
        lat = _safe_float(coords.get("latitude") or coords.get("lat"))
        lng = _safe_float(coords.get("longitude") or coords.get("lng"))
    else:
        lat = _safe_float(card.get("latitude") or card.get("lat"))
        lng = _safe_float(card.get("longitude") or card.get("lng"))

    price_min = _safe_float(src.get("priceFrom") or card.get("price_numeric_min"))
    price_max = _safe_float(src.get("priceTo") or card.get("price_numeric_max"))
    currency = src.get("currency") or card.get("currency") or None
    if currency:
        currency = str(currency).strip() or None
    # Do not trust normaliser "Price on request"
    existing_price = None
    card_price = card.get("price") or card.get("price_display")
    if card_price and str(card_price).strip().lower() not in ("price on request", "n/a"):
        existing_price = str(card_price).strip()
    price_display = _format_price_display(price_min, price_max, currency, existing_price)

    # Description: only real fields, never the invented normaliser sentence
    description = resolve_localized(
        src.get("description") or src.get("shortDescription"),
        lang,
    )
    if not description:
        inv = src.get("investmentHighlights")
        if isinstance(inv, dict):
            pts = inv.get(lang) or inv.get("en") or inv.get("pl")
            if isinstance(pts, list) and pts:
                description = " ".join(str(p).strip() for p in pts if p)
        elif isinstance(inv, list) and inv:
            description = " ".join(str(p).strip() for p in inv if p)
    if description and _is_invented_normaliser_description(
        str(description), str(name or ""), str(location_text or "")
    ):
        description = None

    # Bedrooms from layouts only when present
    bedrooms_display = None
    layouts = src.get("layouts") or src.get("unitTypes") or src.get("unit_types") or []
    if isinstance(layouts, list) and layouts:
        bed_nums = []
        for lay in layouts:
            if isinstance(lay, dict) and lay.get("bedrooms") is not None:
                try:
                    bed_nums.append(int(lay["bedrooms"]))
                except (TypeError, ValueError):
                    pass
        if bed_nums:
            mn, mx = min(bed_nums), max(bed_nums)
            bedrooms_display = f"{mn}-{mx}" if mn != mx else str(mn)
    card_beds = card.get("bedrooms") or card.get("bedrooms_display")
    if not bedrooms_display and card_beds and str(card_beds).lower() not in ("1-4 beds", "n/a"):
        bedrooms_display = str(card_beds).strip()

    area_from = src.get("areaFrom")
    area_to = src.get("areaTo")
    area_display = None
    if area_from is not None and area_to is not None and area_from != area_to:
        area_display = f"{area_from}-{area_to} sqm"
    elif area_from is not None:
        area_display = f"{area_from} sqm"
    elif area_to is not None:
        area_display = f"{area_to} sqm"
    card_area = card.get("area") or card.get("area_display")
    if not area_display and card_area and str(card_area).strip().upper() not in ("N/A", "NA", "-"):
        area_display = str(card_area).strip()

    amenities_raw = list(src.get("amenities") or []) + list(src.get("tags") or [])
    facilities_raw = list(src.get("facilities") or src.get("facility") or [])
    highlights_raw = list(src.get("highlights") or [])
    if not highlights_raw:
        inv = src.get("investmentHighlights")
        if isinstance(inv, dict):
            highlights_raw = inv.get(lang) or inv.get("en") or []
        elif isinstance(inv, list):
            highlights_raw = inv

    floor_plans = _collect_images(
        src.get("floorPlans") or src.get("floor_plans") or src.get("plans") or []
    )
    master_plan = _collect_images(
        src.get("masterPlan") or src.get("master_plan") or src.get("masterPlans") or []
    )
    # Also classify images by type tags if present
    for img in src.get("images") or []:
        if isinstance(img, dict):
            itype = str(img.get("type") or img.get("image_type") or "").lower()
            url = _image_url(img)
            if not url:
                continue
            if "floor" in itype and url not in floor_plans:
                floor_plans.append(url)
            if "master" in itype and url not in master_plan:
                master_plan.append(url)

    unit_types = _parse_unit_types(layouts if isinstance(layouts, list) else [], lang)

    status_raw = src.get("completionStatus") or src.get("status") or card.get("status")
    # Ignore soft "Available" invented from isActive alone when no real status field
    if not src.get("completionStatus") and not src.get("status") and card.get("status") == "Available":
        if "isActive" in src or "isActive" in card:
            status_raw = None

    ownership = resolve_localized(
        src.get("ownershipType") or src.get("ownership") or src.get("ownership_type"),
        lang,
    )

    developer = None
    raw_dev = src.get("developer")
    if isinstance(raw_dev, dict):
        developer = resolve_localized(
            raw_dev.get("name") or raw_dev.get("title"),
            lang,
        )
        developer_logo = _image_url(raw_dev.get("logo"))
    else:
        developer = resolve_localized(
            src.get("developerName") or src.get("developer"),
            lang,
        )
        developer_logo = None
    if not developer_logo:
        developer_logo = _image_url(src.get("developerLogo") or src.get("developer_logo"))

    completion = resolve_localized(
        src.get("completionDate")
        or src.get("completion")
        or src.get("handoverDate")
        or src.get("expectedCompletion"),
        lang,
    )
    parking = resolve_localized(src.get("parking") or src.get("parkingInfo"), lang)

    prop_type = resolve_localized(src.get("propertyType") or card.get("property_type"), lang)
    if prop_type and str(prop_type).lower() in ("villa / condo", "n/a"):
        prop_type = None
    elif prop_type:
        prop_type = _title_case_slug(str(prop_type))

    units = _safe_int_or_str(src.get("units") or src.get("totalUnits") or src.get("unitCount"))
    total_floors = _safe_int_or_str(src.get("totalFloors") or src.get("floors") or src.get("storeys"))
    bathrooms = _safe_int_or_str(src.get("bathrooms") or card.get("bathrooms"))

    updated = src.get("updatedAt") or src.get("updated_at") or card.get("updated_at")
    updated_at = None
    if updated is not None:
        if hasattr(updated, "isoformat"):
            updated_at = updated.isoformat()
        else:
            updated_at = str(updated)

    return BrochureData(
        property_id=str(card.get("property_id") or src.get("id") or "").strip() or None,
        name=str(name).strip() if name else None,
        slug=str(slug_val).strip() if slug_val else None,
        tenant=tenant_info,
        language=lang,
        hero_image=hero,
        images=images,
        location_text=location_text,
        district=str(district).strip() if district else None,
        province=str(province).strip() if province else None,
        country=str(country).strip() if country else None,
        lat=lat,
        lng=lng,
        developer_name=str(developer).strip() if developer else None,
        developer_logo=developer_logo,
        description=str(description).strip() if description else None,
        highlights=localize_tag_list(highlights_raw, lang),
        facilities=localize_tag_list(facilities_raw, lang, AMENITY_LABELS),
        amenities=localize_tag_list(amenities_raw, lang, AMENITY_LABELS),
        nearby_places=_parse_nearby(src.get("nearbyPlaces") or src.get("nearby_places") or card.get("nearby_places"), lang),
        property_type=prop_type,
        status=localize_label(status_raw, lang, STATUS_LABELS) if status_raw else None,
        ownership_type=localize_label(str(ownership), lang, OWNERSHIP_LABELS) if ownership else None,
        price_display=price_display,
        price_min=price_min,
        price_max=price_max,
        currency=currency,
        bedrooms_display=bedrooms_display,
        bathrooms=bathrooms,
        area_display=area_display,
        units=units,
        total_floors=total_floors,
        completion=str(completion) if completion else None,
        parking=str(parking) if parking else None,
        unit_types=unit_types,
        floor_plans=floor_plans,
        master_plan=master_plan,
        contact=contact,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------

def _load_postgres_property(db: Session, property_id: str) -> Any:
    if db is None:
        return None
    try:
        from app.db.models import Property
        prop = db.query(Property).filter(Property.property_id == property_id.upper()).first()
        if not prop:
            prop = db.query(Property).filter(Property.property_id.ilike(property_id.strip())).first()
        return prop
    except Exception as e:
        logger.warning(f"PostgreSQL brochure data lookup failed for '{property_id}': {e}")
        return None


def load_brochure_data(
    db: Session,
    property_id: str,
    site_origin: Optional[str] = None,
    language: str = "en",
) -> BrochureData:
    """
    Load BrochureData from PostgreSQL first, then Firestore (read-only).
    Raises ValueError if the property exists in neither store.
    Never invents missing values.
    """
    pid = (property_id or "").strip()
    if not pid:
        raise ValueError("Property with ID '' not found.")

    lang = _normalize_lang(language)
    prop = _load_postgres_property(db, pid)
    if prop is not None:
        return _from_postgres(prop, lang, site_origin)

    try:
        from app.services.firestore_service import get_property_by_id_from_firestore
        fs_card = get_property_by_id_from_firestore(pid)
    except Exception as e:
        logger.warning(f"Firestore brochure data lookup failed for '{pid}': {e}")
        fs_card = None

    if fs_card and isinstance(fs_card, dict):
        return _from_firestore_card(fs_card, lang, site_origin)

    raise ValueError(f"Property with ID '{property_id}' not found.")
