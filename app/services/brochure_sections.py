"""
Brochure section registry for the deterministic HTML/PDF pipeline.

Single-property portrait brochure (up to 8 A4 pages). Sections without
verified data are skipped; page numbers renumber. No empty boxes, no "N/A".
"""

from __future__ import annotations

import re
from typing import Optional
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.services.brochure_data import BrochureData, BrochureNearbyPlace


MAX_DESCRIPTION_CHARS = 900
MAX_HIGHLIGHTS = 6
MAX_GALLERY_IMAGES = 6
MAX_NEARBY_PER_CATEGORY = 6
MAX_UNIT_TYPES = 8
MAX_FLOOR_PLANS = 6
MAX_MASTER_PLAN = 4
# Retained for tests that still call chunk_list with facilities page size
MAX_FACILITIES_PER_PAGE = 14


FACT_LABELS = {
    "en": {
        "area": "Area",
        "stage": "Stage",
        "ownership": "Ownership type",
        "amenities": "Amenities",
        "developer": "Developer",
        "location": "Location",
        "units": "Units",
        "property_type": "Property type",
        "bedrooms": "Bedrooms",
        "bathrooms": "Bathrooms",
        "price": "Price",
        "completion": "Completion",
        "parking": "Parking",
        "floors": "Floors",
        "status": "Status",
    },
    "pl": {
        "area": "Powierzchnia",
        "stage": "Etap projektu",
        "ownership": "Forma nabycia",
        "amenities": "Udogodnienia",
        "developer": "Deweloper",
        "location": "Lokalizacja",
        "units": "Lokale",
        "property_type": "Typ nieruchomości",
        "bedrooms": "Sypialnie",
        "bathrooms": "Łazienki",
        "price": "Cena",
        "completion": "Ukończenie",
        "parking": "Parking",
        "floors": "Piętra",
        "status": "Status",
    },
    "th": {
        "area": "พื้นที่",
        "stage": "ขั้นตอนโครงการ",
        "ownership": "รูปแบบกรรมสิทธิ์",
        "amenities": "สิ่งอำนวยความสะดวก",
        "developer": "ผู้พัฒนา",
        "location": "ทำเล",
        "units": "ยูนิต",
        "property_type": "ประเภทอสังหาริมทรัพย์",
        "bedrooms": "ห้องนอน",
        "bathrooms": "ห้องน้ำ",
        "price": "ราคา",
        "completion": "แล้วเสร็จ",
        "parking": "ที่จอดรถ",
        "floors": "ชั้น",
        "status": "สถานะ",
    },
}

NEXT_STEPS = {
    "en": [
        "Share your budget, timeline, and preferred ownership type.",
        "We confirm availability and arrange a viewing or video tour.",
        "Review contracts with our advisors before reservation.",
    ],
    "pl": [
        "Podaj budżet, termin oraz preferowaną formę nabycia.",
        "Potwierdzamy dostępność i umawiamy oględziny lub wideoturę.",
        "Sprawdzamy umowę z doradcą przed rezerwacją.",
    ],
    "th": [
        "แจ้งงบประมาณ ระยะเวลา และรูปแบบกรรมสิทธิ์ที่ต้องการ",
        "เรายืนยันความพร้อมและนัดชมโครงการหรือทัวร์วิดีโอ",
        "ตรวจสัญญาพร้อมที่ปรึกษาก่อนจอง",
    ],
}


def _is_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def truncate_at_sentence(text: Optional[str], max_chars: int = MAX_DESCRIPTION_CHARS) -> Optional[str]:
    """
    Truncate at a sentence boundary. Never pads or invents closing text.
    Returns None for empty input.
    """
    if not text or not str(text).strip():
        return None
    cleaned = re.sub(r"\s+", " ", str(text).strip())
    if len(cleaned) <= max_chars:
        return cleaned

    cut = cleaned[:max_chars]
    ends = [m.end() for m in re.finditer(r"[.!?…](?:\s|$)", cut)]
    if ends and ends[-1] >= max(80, max_chars // 3):
        return cut[: ends[-1]].strip()

    sp = cut.rfind(" ")
    if sp >= max(40, max_chars // 4):
        return cut[:sp].rstrip(" ,;:") + "."
    return cut.rstrip(" ,;:") + "."


def chunk_list(items: Sequence[Any], size: int) -> List[List[Any]]:
    """Split a sequence into chunks of at most `size` (size <= 0 -> single chunk)."""
    if not items:
        return []
    if size <= 0:
        return [list(items)]
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def group_nearby_places(
    places: Sequence[BrochureNearbyPlace],
    max_per_category: int = MAX_NEARBY_PER_CATEGORY,
) -> Dict[str, List[BrochureNearbyPlace]]:
    """Group nearby places by category; drop empty names; clamp per category."""
    grouped: Dict[str, List[BrochureNearbyPlace]] = {}
    for place in places or []:
        name = (place.name or "").strip()
        if not name:
            continue
        cat = (place.category or "").strip() or "Nearby"
        bucket = grouped.setdefault(cat, [])
        if len(bucket) >= max_per_category:
            continue
        bucket.append(place)
    return grouped


def _fact_labels(lang: Optional[str]) -> Dict[str, str]:
    key = (lang or "en").lower()
    if key not in FACT_LABELS:
        key = "en"
    return FACT_LABELS[key]


def at_a_glance_rows(data: BrochureData) -> List[Dict[str, str]]:
    """
    Build specification rows that have real values only.
    Never emits N/A or empty cells. Labels follow brochure language.
    """
    fl = _fact_labels(data.language)
    candidates = [
        (fl["developer"], data.developer_name),
        (fl["location"], data.location_text),
        (fl["units"], str(data.units) if data.units is not None else None),
        (fl["property_type"], data.property_type),
        (fl["area"], data.area_display),
        (fl["bedrooms"], data.bedrooms_display),
        (fl["bathrooms"], str(data.bathrooms) if data.bathrooms is not None else None),
        (fl["ownership"], data.ownership_type),
        (fl["status"], data.status),
        (fl["price"], data.price_display),
        (fl["completion"], data.completion),
        (fl["parking"], data.parking),
        (fl["floors"], str(data.total_floors) if data.total_floors is not None else None),
    ]
    rows: List[Dict[str, str]] = []
    for label, value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.upper() in ("N/A", "NA", "-"):
            continue
        rows.append({"label": label, "value": text})
    return rows


def overview_fact_cells(data: BrochureData) -> List[Dict[str, str]]:
    """Area / Stage / Ownership / Amenities — omit missing cells so the grid reflows."""
    fl = _fact_labels(data.language)
    cells: List[Dict[str, str]] = []
    if data.area_display and str(data.area_display).strip().upper() not in ("N/A", "NA", "-"):
        cells.append({"label": fl["area"], "value": str(data.area_display).strip()})
    if data.status and str(data.status).strip().upper() not in ("N/A", "NA", "-"):
        cells.append({"label": fl["stage"], "value": str(data.status).strip()})
    if data.ownership_type and str(data.ownership_type).strip().upper() not in ("N/A", "NA", "-"):
        cells.append({"label": fl["ownership"], "value": str(data.ownership_type).strip()})
    amenity_bits = [a for a in (data.amenities or data.facilities or []) if str(a).strip()]
    if amenity_bits:
        # Compact list for the fact cell
        shown = ", ".join(str(a).strip() for a in amenity_bits[:4])
        if len(amenity_bits) > 4:
            shown += f" +{len(amenity_bits) - 4}"
        cells.append({"label": fl["amenities"], "value": shown})
    return cells


@dataclass
class BrochureSection:
    id: str
    required: List[str]
    template: str
    always: bool = False
    # "all" = every required field nonempty; "any" = at least one nonempty
    require_mode: str = "all"

    def has_data(self, data: BrochureData) -> bool:
        if self.always:
            return True
        if not self.required:
            return False

        flags = [_is_nonempty(getattr(data, field_name, None)) for field_name in self.required]
        if self.require_mode == "any":
            return any(flags)
        return all(flags)


# Up to 8 portrait pages for one property (skip when no data).
BROCHURE_SECTIONS: List[BrochureSection] = [
    BrochureSection(
        id="cover",
        required=["name"],
        template="sections/cover.html",
        always=True,
    ),
    BrochureSection(
        id="intro",
        required=["description", "highlights", "area_display", "status", "price_display", "location_text"],
        template="sections/intro.html",
        require_mode="any",
    ),
    BrochureSection(
        id="overview",
        required=[
            "description",
            "price_display",
            "area_display",
            "status",
            "ownership_type",
            "amenities",
            "facilities",
            "hero_image",
            "images",
        ],
        template="sections/overview.html",
        require_mode="any",
    ),
    BrochureSection(
        id="gallery",
        required=["images"],
        template="sections/gallery.html",
    ),
    BrochureSection(
        id="unit_types",
        required=["unit_types", "floor_plans"],
        template="sections/unit_types.html",
        require_mode="any",
    ),
    BrochureSection(
        id="nearby_places",
        required=["nearby_places", "location_text"],
        template="sections/nearby_places.html",
        require_mode="any",
    ),
    BrochureSection(
        id="investment",
        required=["investment_info"],
        template="sections/investment.html",
    ),
    BrochureSection(
        id="contact",
        required=["contact"],
        template="sections/contact.html",
        always=True,
    ),
]


def select_sections(data: BrochureData, copy: Optional[Any] = None) -> List[BrochureSection]:
    """Return sections that should render for this property."""
    out: List[BrochureSection] = []
    for sec in BROCHURE_SECTIONS:
        if sec.has_data(data):
            out.append(sec)
            continue
        if copy is not None and sec.id == "intro" and (
            getattr(copy, "intro", None)
            or getattr(copy, "headline", None)
            or getattr(copy, "highlights", None)
        ):
            out.append(sec)
        elif copy is not None and sec.id == "overview" and (
            getattr(copy, "intro", None) or getattr(copy, "headline", None)
        ):
            out.append(sec)
        elif copy is not None and sec.id == "nearby_places" and getattr(copy, "location_blurb", None):
            out.append(sec)
        elif copy is not None and sec.id == "investment" and getattr(copy, "investment_blurb", None):
            # Only when verified investment_info exists — never invent from copy alone
            if _is_nonempty(data.investment_info):
                out.append(sec)
    return out


def _cta_text(lang: Optional[str]) -> str:
    return {
        "en": "Book a viewing",
        "pl": "Umów oględziny",
        "th": "นัดชมโครงการ",
    }.get((lang or "en").lower(), "Book a viewing")


def _cta_url(data: BrochureData) -> Optional[str]:
    if data.contact and data.contact.website_url:
        return data.contact.website_url
    if data.tenant and data.tenant.website_url:
        return data.tenant.website_url
    if data.contact and data.contact.email:
        return f"mailto:{data.contact.email}"
    return None


def prepare_section_context(
    section: BrochureSection,
    data: BrochureData,
    copy: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Build template context for one section with clamps / pagination.
    Does not invent values. Empty optional keys are omitted.
    Optional `copy` (BrochureCopy) replaces prose slots only; fact tables stay from data.
    """
    ctx: Dict[str, Any] = {
        "section_id": section.id,
        "data": data,
        "property": data,
    }
    if copy is not None:
        ctx["copy"] = copy

    lang = (data.language or "en").lower()
    cta = _cta_text(lang)
    cta_url = _cta_url(data)
    ctx["cta_text"] = cta
    if cta_url:
        ctx["cta_url"] = cta_url

    if section.id == "intro":
        if copy is not None and getattr(copy, "intro", None):
            ctx["description"] = str(copy.intro)
        else:
            truncated = truncate_at_sentence(data.description)
            if truncated:
                ctx["description"] = truncated
        if copy is not None and getattr(copy, "headline", None):
            ctx["headline"] = str(copy.headline)
        if copy is not None and getattr(copy, "location_blurb", None):
            ctx["location_blurb"] = str(copy.location_blurb)
        if copy is not None and getattr(copy, "lifestyle_blurb", None):
            ctx["lifestyle_blurb"] = str(copy.lifestyle_blurb)
        if copy is not None and getattr(copy, "highlights", None):
            items = [h for h in (copy.highlights or []) if str(h).strip()][:MAX_HIGHLIGHTS]
        else:
            items = [h for h in (data.highlights or []) if str(h).strip()][:MAX_HIGHLIGHTS]
        if items:
            ctx["highlights"] = items
        rows = at_a_glance_rows(data)
        if rows:
            ctx["rows"] = rows

    elif section.id == "overview":
        if copy is not None and getattr(copy, "intro", None):
            ctx["description"] = truncate_at_sentence(str(copy.intro), 700)
        else:
            truncated = truncate_at_sentence(data.description, 700)
            if truncated:
                ctx["description"] = truncated
        cells = overview_fact_cells(data)
        if cells:
            ctx["fact_cells"] = cells
        if data.hero_image:
            ctx["hero_image"] = data.hero_image
        elif data.images:
            ctx["hero_image"] = data.images[0]
        if data.investment_info:
            notice = truncate_at_sentence(data.investment_info, 220)
            if notice:
                ctx["notice"] = notice

    elif section.id == "gallery":
        imgs = [u for u in (data.images or []) if u][:MAX_GALLERY_IMAGES]
        if imgs:
            ctx["images"] = imgs
            ctx["hero_image"] = data.hero_image or imgs[0]

    elif section.id == "nearby_places":
        grouped = group_nearby_places(data.nearby_places or [])
        if grouped:
            ctx["nearby_by_category"] = grouped
        if copy is not None and getattr(copy, "location_blurb", None):
            ctx["location_blurb"] = str(copy.location_blurb)

    elif section.id == "unit_types":
        units = list(data.unit_types or [])[:MAX_UNIT_TYPES]
        if units:
            ctx["unit_types"] = units
        plans = [u for u in (data.floor_plans or []) if u][:MAX_FLOOR_PLANS]
        if plans:
            ctx["floor_plans"] = plans

    elif section.id == "investment":
        if data.investment_info:
            text = truncate_at_sentence(data.investment_info, 1200)
            if text:
                ctx["investment_text"] = text
        if copy is not None and getattr(copy, "investment_blurb", None) and data.investment_info:
            ctx["investment_blurb"] = str(copy.investment_blurb)

    elif section.id == "cover":
        if copy is not None and getattr(copy, "headline", None):
            ctx["name"] = str(copy.headline)
            ctx["headline"] = str(copy.headline)
        else:
            name = data.name or data.property_id
            if name and data.location_text:
                ctx["name"] = f"{name}"
                ctx["headline"] = f"{name}"
            else:
                ctx["name"] = name
        if data.hero_image:
            ctx["hero_image"] = data.hero_image
        elif data.images:
            ctx["hero_image"] = data.images[0]
        if data.location_text:
            ctx["location_text"] = data.location_text
        if data.tenant:
            ctx["tenant"] = data.tenant
        if data.property_type:
            ctx["subtitle"] = data.property_type
        ctx["date_label"] = datetime.now(timezone.utc).strftime("%d %b %Y")

    elif section.id == "contact":
        ctx["contact"] = data.contact
        ctx["tenant"] = data.tenant
        if data.name:
            ctx["name"] = data.name
        if copy is not None and getattr(copy, "closing_line", None):
            ctx["closing_line"] = str(copy.closing_line)
        steps = NEXT_STEPS.get(lang, NEXT_STEPS["en"])
        ctx["next_steps"] = list(steps)

    return ctx


def build_brochure_page_plan(data: BrochureData, copy: Optional[Any] = None) -> List[Dict[str, Any]]:
    """
    Expand sections into renderable page contexts.
    Cover and contact always appear. Skipped sections renumber downstream.
    """
    pages: List[Dict[str, Any]] = []
    for section in select_sections(data, copy=copy):
        ctx = prepare_section_context(section, data, copy=copy)

        if not section.always:
            content_keys = [
                k
                for k in ctx.keys()
                if k not in ("section_id", "data", "property", "copy", "cta_text", "cta_url")
                and _is_nonempty(ctx.get(k))
            ]
            # nearby_places may only have location_text on the data object
            if section.id == "nearby_places" and not content_keys and data.location_text:
                content_keys = ["location_text"]
            if not content_keys:
                continue

        pages.append(
            {
                "section_id": section.id,
                "template": section.template,
                "page_index": 0,
                "page_count": 1,
                **{k: v for k, v in ctx.items() if k not in ("section_id",)},
            }
        )
    return pages
