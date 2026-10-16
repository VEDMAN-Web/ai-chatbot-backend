"""
Brochure section registry for the deterministic HTML/PDF pipeline.

Each section declares required BrochureData fields. Sections without data
are skipped (except cover and contact, which always render). No empty
boxes, no "N/A". Long lists/paragraphs are clamped or paginated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from app.services.brochure_data import BrochureData, BrochureNearbyPlace


# Safe lengths for landscape A4 HTML pages (approx.)
MAX_DESCRIPTION_CHARS = 900
MAX_HIGHLIGHTS = 8
MAX_FACILITIES_PER_PAGE = 14
MAX_GALLERY_IMAGES = 6
MIN_GALLERY_IMAGES = 1
MAX_NEARBY_PER_CATEGORY = 6
MAX_UNIT_TYPES = 8
MAX_FLOOR_PLANS = 6
MAX_MASTER_PLAN = 4


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
    # Prefer last sentence end within the window
    ends = [m.end() for m in re.finditer(r"[.!?…](?:\s|$)", cut)]
    if ends and ends[-1] >= max(80, max_chars // 3):
        return cut[: ends[-1]].strip()

    # Fall back to last word boundary
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


def at_a_glance_rows(data: BrochureData) -> List[Dict[str, str]]:
    """
    Build specification rows that have real values only.
    Never emits N/A or empty cells.
    """
    candidates = [
        ("Developer", data.developer_name),
        ("Location", data.location_text),
        ("Units", str(data.units) if data.units is not None else None),
        ("Property type", data.property_type),
        ("Sizes", data.area_display),
        ("Bedrooms", data.bedrooms_display),
        ("Bathrooms", str(data.bathrooms) if data.bathrooms is not None else None),
        ("Ownership", data.ownership_type),
        ("Status", data.status),
        ("Price", data.price_display),
        ("Completion", data.completion),
        ("Parking", data.parking),
        ("Floors", str(data.total_floors) if data.total_floors is not None else None),
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


# Ordered like the landscape reference structure (cover ... contact).
BROCHURE_SECTIONS: List[BrochureSection] = [
    BrochureSection(
        id="cover",
        required=["name"],
        template="sections/cover.html",
        always=True,
    ),
    BrochureSection(
        id="intro",
        required=["description"],
        template="sections/intro.html",
    ),
    BrochureSection(
        id="highlights",
        required=["highlights"],
        template="sections/highlights.html",
    ),
    BrochureSection(
        id="gallery",
        required=["images"],
        template="sections/gallery.html",
    ),
    BrochureSection(
        id="facilities",
        required=["facilities"],
        template="sections/facilities.html",
    ),
    BrochureSection(
        id="at_a_glance",
        required=[
            "developer_name",
            "location_text",
            "units",
            "property_type",
            "area_display",
            "bedrooms_display",
            "ownership_type",
            "status",
            "price_display",
            "completion",
            "parking",
            "total_floors",
            "bathrooms",
        ],
        template="sections/at_a_glance.html",
        require_mode="any",
    ),
    BrochureSection(
        id="nearby_places",
        required=["nearby_places"],
        template="sections/nearby_places.html",
    ),
    BrochureSection(
        id="unit_types",
        required=["unit_types"],
        template="sections/unit_types.html",
    ),
    BrochureSection(
        id="floor_plans",
        required=["floor_plans"],
        template="sections/floor_plans.html",
    ),
    BrochureSection(
        id="master_plan",
        required=["master_plan"],
        template="sections/master_plan.html",
    ),
    BrochureSection(
        id="contact",
        required=["contact"],
        template="sections/contact.html",
        always=True,
    ),
]


def select_sections(data: BrochureData) -> List[BrochureSection]:
    """Return sections that should render for this property."""
    return [sec for sec in BROCHURE_SECTIONS if sec.has_data(data)]


def prepare_section_context(section: BrochureSection, data: BrochureData) -> Dict[str, Any]:
    """
    Build template context for one section with clamps / pagination.
    Does not invent values. Empty optional keys are omitted.
    """
    ctx: Dict[str, Any] = {
        "section_id": section.id,
        "data": data,
        "property": data,
    }

    if section.id == "intro":
        truncated = truncate_at_sentence(data.description)
        if truncated:
            ctx["description"] = truncated

    elif section.id == "highlights":
        items = [h for h in (data.highlights or []) if str(h).strip()][:MAX_HIGHLIGHTS]
        if items:
            ctx["highlights"] = items

    elif section.id == "gallery":
        imgs = [u for u in (data.images or []) if u][:MAX_GALLERY_IMAGES]
        if imgs:
            ctx["images"] = imgs
            ctx["hero_image"] = data.hero_image or imgs[0]

    elif section.id == "facilities":
        items = [f for f in (data.facilities or []) if str(f).strip()]
        # Prefer facilities; fall back is not applied (amenities has its own path if needed)
        pages = chunk_list(items, MAX_FACILITIES_PER_PAGE)
        if pages:
            ctx["facility_pages"] = pages
            ctx["facilities"] = items

    elif section.id == "at_a_glance":
        rows = at_a_glance_rows(data)
        if rows:
            ctx["rows"] = rows

    elif section.id == "nearby_places":
        grouped = group_nearby_places(data.nearby_places or [])
        if grouped:
            ctx["nearby_by_category"] = grouped

    elif section.id == "unit_types":
        units = list(data.unit_types or [])[:MAX_UNIT_TYPES]
        if units:
            ctx["unit_types"] = units

    elif section.id == "floor_plans":
        plans = [u for u in (data.floor_plans or []) if u][:MAX_FLOOR_PLANS]
        if plans:
            ctx["floor_plans"] = plans

    elif section.id == "master_plan":
        plans = [u for u in (data.master_plan or []) if u][:MAX_MASTER_PLAN]
        if plans:
            ctx["master_plan"] = plans

    elif section.id == "cover":
        ctx["name"] = data.name or data.property_id
        if data.hero_image:
            ctx["hero_image"] = data.hero_image
        elif data.images:
            ctx["hero_image"] = data.images[0]
        if data.location_text:
            ctx["location_text"] = data.location_text
        if data.tenant:
            ctx["tenant"] = data.tenant

    elif section.id == "contact":
        ctx["contact"] = data.contact
        ctx["tenant"] = data.tenant
        if data.name:
            ctx["name"] = data.name

    return ctx


def build_brochure_page_plan(data: BrochureData) -> List[Dict[str, Any]]:
    """
    Expand sections into renderable page contexts.
    Facilities may become multiple pages when the list is long.
    Cover and contact always appear when select_sections includes them.
    """
    pages: List[Dict[str, Any]] = []
    for section in select_sections(data):
        ctx = prepare_section_context(section, data)

        if section.id == "facilities":
            facility_pages = ctx.get("facility_pages") or []
            if not facility_pages:
                continue
            for idx, chunk in enumerate(facility_pages):
                pages.append(
                    {
                        "section_id": section.id,
                        "template": section.template,
                        "page_index": idx,
                        "page_count": len(facility_pages),
                        "facilities": chunk,
                        "data": data,
                    }
                )
            continue

        # Skip non-always sections that ended up with no usable clamped content
        if not section.always:
            content_keys = [
                k
                for k in ctx.keys()
                if k not in ("section_id", "data", "property") and _is_nonempty(ctx.get(k))
            ]
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
