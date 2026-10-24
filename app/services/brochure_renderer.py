"""
Deterministic HTML -> PDF brochure renderer (WeasyPrint).

No LLM. Fills Jinja2 templates from BrochureData, embeds fonts and images,
and returns PDF bytes. Missing data sections are skipped upstream.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings
from app.core.tenant import resolve_tenant
from app.services.brochure_data import BrochureData
from app.services.brochure_sections import (
    build_brochure_page_plan,
    prepare_section_context,
    select_sections,
)

logger = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = APP_ROOT / "templates" / "brochure"
FONTS_DIR = APP_ROOT / "assets" / "fonts"
REPO_ROOT = APP_ROOT.parent

IMAGE_TIMEOUT_SEC = 5
IMAGE_MAX_BYTES = 4_000_000
IMAGE_MAX_WIDTH = 1400
IMAGE_JPEG_QUALITY = 78

# Tenant visual themes (layout shared; colours differ by brand)
TENANT_THEMES: Dict[str, Dict[str, str]] = {
    "tajlandia.com": {
        "accent": "#C4785A",
        "sage": "#A8B5A0",
        "cream": "#F3EEE6",
        "paper": "#F7F4EE",
        "ink": "#2C2A26",
        "muted": "#5C574F",
    },
    "thai.estate": {
        "accent": "#B8894A",
        "sage": "#9CAF88",
        "cream": "#F5F0E6",
        "paper": "#FAF8F3",
        "ink": "#1F1C18",
        "muted": "#6A6358",
    },
    "varsoviaestate.com": {
        "accent": "#8B7355",
        "sage": "#8A9A8C",
        "cream": "#F0EBE3",
        "paper": "#F5F2EC",
        "ink": "#1A1A1A",
        "muted": "#5A554C",
    },
}

SECTION_LABELS = {
    "en": {
        "the": "THE",
        "highlights_title": "Key highlights",
        "facilities_label": "Premium facilities",
        "at_a_glance": "At a glance",
        "nearby_title": "Nearby places",
        "unit_types_title": "Unit types",
        "floor_plans_title": "Floor plans",
        "master_plan_title": "Master plan",
        "contact_note": "Start a new conversation with our advisory team",
        "beds": "beds",
        "baths": "baths",
        "units_label": "units",
        "book_viewing": "Book a viewing",
        "introduction": "Introduction",
        "overview": "Property overview",
        "gallery": "Gallery",
        "location_title": "Location",
        "investment_title": "Investment",
        "investment_heading": "Investment & rental information",
        "next_steps": "Next steps",
        "how_to_proceed": "How to proceed",
        "price_from": "Price from",
        "default_next_steps": [
            "Share your budget, timeline, and preferred ownership type.",
            "We confirm availability and arrange a viewing or video tour.",
            "Review contracts with our advisors before reservation.",
        ],
    },
    "pl": {
        "the": "THE",
        "highlights_title": "Najważniejsze atuty",
        "facilities_label": "Udogodnienia",
        "at_a_glance": "W skrócie",
        "nearby_title": "W pobliżu",
        "unit_types_title": "Typy lokali",
        "floor_plans_title": "Rzuty",
        "master_plan_title": "Plan zagospodarowania",
        "contact_note": "Skontaktuj się z naszym zespołem doradców",
        "beds": "sypialnie",
        "baths": "łazienki",
        "units_label": "lokale",
        "book_viewing": "Umów oględziny",
        "introduction": "Wprowadzenie",
        "overview": "Przegląd nieruchomości",
        "gallery": "Galeria",
        "location_title": "Lokalizacja",
        "investment_title": "Inwestycja",
        "investment_heading": "Informacje inwestycyjne i wynajem",
        "next_steps": "Kolejne kroki",
        "how_to_proceed": "Jak zacząć",
        "price_from": "Cena od",
        "default_next_steps": [
            "Podaj budżet, termin oraz preferowaną formę nabycia.",
            "Potwierdzamy dostępność i umawiamy oględziny lub wideoturę.",
            "Sprawdzamy umowę z doradcą przed rezerwacją.",
        ],
    },
    "th": {
        "the": "THE",
        "highlights_title": "จุดเด่นสำคัญ",
        "facilities_label": "สิ่งอำนวยความสะดวก",
        "at_a_glance": "สรุปข้อมูล",
        "nearby_title": "สถานที่ใกล้เคียง",
        "unit_types_title": "แบบยูนิต",
        "floor_plans_title": "แปลนชั้น",
        "master_plan_title": "แผนผังโครงการ",
        "contact_note": "ติดต่อทีมที่ปรึกษาของเรา",
        "beds": "ห้องนอน",
        "baths": "ห้องน้ำ",
        "units_label": "ยูนิต",
        "book_viewing": "นัดชมโครงการ",
        "introduction": "บทนำ",
        "overview": "ภาพรวมโครงการ",
        "gallery": "แกลเลอรี",
        "location_title": "ทำเล",
        "investment_title": "การลงทุน",
        "investment_heading": "ข้อมูลการลงทุนและค่าเช่า",
        "next_steps": "ขั้นตอนถัดไป",
        "how_to_proceed": "วิธีดำเนินการ",
        "price_from": "ราคาเริ่มต้น",
        "default_next_steps": [
            "แจ้งงบประมาณ ระยะเวลา และรูปแบบกรรมสิทธิ์ที่ต้องการ",
            "เรายืนยันความพร้อมและนัดชมโครงการหรือทัวร์วิดีโอ",
            "ตรวจสัญญาพร้อมที่ปรึกษาก่อนจอง",
        ],
    },
}

_FONT_FILES = {
    "serif_regular": "NotoSerif-Regular.ttf",
    "serif_bold": "NotoSerif-Bold.ttf",
    "serif_italic": "NotoSerif-Italic.ttf",
    "sans_regular": "NotoSans-Regular.ttf",
    "sans_bold": "NotoSans-Bold.ttf",
    "thai_regular": "NotoSansThai-Regular.ttf",
    "thai_bold": "NotoSansThai-Bold.ttf",
}


def _theme_for_origin(site_origin: Optional[str]) -> Dict[str, str]:
    tenant = resolve_tenant(site_origin)
    key = getattr(tenant, "site_origin", None) or getattr(tenant, "domain", "") or ""
    return dict(TENANT_THEMES.get(key, TENANT_THEMES["varsoviaestate.com"]))


def _font_file_urls() -> Dict[str, str]:
    urls = {}
    for key, filename in _FONT_FILES.items():
        path = FONTS_DIR / filename
        urls[key] = path.resolve().as_uri() if path.is_file() else ""
    return urls


def _file_to_data_uri(path: Optional[str], mime: str = "image/png") -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > IMAGE_MAX_BYTES:
            return None
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _fetch_and_optimize_image(url: str) -> Optional[str]:
    """
    Fetch remote/local image with timeout and size cap, resize, JPEG-encode,
    return a data URI. Returns None on any failure (never raises).
    """
    if not url or not str(url).strip():
        return None
    raw_url = str(url).strip()

    try:
        if raw_url.startswith("data:"):
            return raw_url
        if raw_url.startswith("file:"):
            from urllib.parse import urlparse, unquote
            path = unquote(urlparse(raw_url).path)
            if os.name == "nt" and path.startswith("/"):
                path = path.lstrip("/")
            return _optimize_image_bytes(Path(path).read_bytes()) if os.path.isfile(path) else None
        if os.path.isfile(raw_url):
            return _optimize_image_bytes(Path(raw_url).read_bytes())

        req = Request(raw_url, headers={"User-Agent": "TajlandiaBrochure/1.0"})
        with urlopen(req, timeout=IMAGE_TIMEOUT_SEC) as resp:
            data = resp.read(IMAGE_MAX_BYTES + 1)
        if len(data) > IMAGE_MAX_BYTES:
            logger.warning("Brochure image too large, skipped: %s", raw_url[:120])
            return None
        return _optimize_image_bytes(data)
    except Exception as e:
        logger.warning("Brochure image fetch failed (%s): %s", raw_url[:120], e)
        return None


def _optimize_image_bytes(data: bytes) -> Optional[str]:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        w, h = img.size
        if w > IMAGE_MAX_WIDTH:
            ratio = IMAGE_MAX_WIDTH / float(w)
            img = img.resize((IMAGE_MAX_WIDTH, max(1, int(h * ratio))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        logger.warning("Brochure image optimize failed: %s", e)
        return None


def _branded_cover_placeholder(theme: Dict[str, str]) -> str:
    """Minimal SVG placeholder used only for cover when no hero image exists."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800">'
        f'<rect width="100%" height="100%" fill="{theme["sage"]}"/>'
        f'<rect x="80" y="80" width="1040" height="640" fill="none" stroke="{theme["accent"]}" stroke-width="2"/>'
        f"</svg>"
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _embed_images_in_data(data: BrochureData, theme: Dict[str, str]) -> BrochureData:
    """Return a shallow-copied BrochureData with image URLs replaced by data URIs where possible."""
    payload = data.model_dump()
    hero = _fetch_and_optimize_image(payload.get("hero_image") or "")
    images = []
    for url in payload.get("images") or []:
        emb = _fetch_and_optimize_image(url)
        if emb:
            images.append(emb)
    if not hero and images:
        hero = images[0]
    if not hero:
        hero = _branded_cover_placeholder(theme)

    payload["hero_image"] = hero
    payload["images"] = images

    floor_plans = []
    for url in payload.get("floor_plans") or []:
        emb = _fetch_and_optimize_image(url)
        if emb:
            floor_plans.append(emb)
    payload["floor_plans"] = floor_plans

    master_plan = []
    for url in payload.get("master_plan") or []:
        emb = _fetch_and_optimize_image(url)
        if emb:
            master_plan.append(emb)
    payload["master_plan"] = master_plan

    unit_types = []
    for ut in payload.get("unit_types") or []:
        ut = dict(ut)
        if ut.get("layout_image"):
            emb = _fetch_and_optimize_image(ut["layout_image"])
            ut["layout_image"] = emb
        unit_types.append(ut)
    payload["unit_types"] = unit_types

    if payload.get("developer_logo"):
        payload["developer_logo"] = _fetch_and_optimize_image(payload["developer_logo"])

    return BrochureData.model_validate(payload)


def brochure_content_hash(data: BrochureData) -> str:
    """Stable content digest for cache keys (excludes local logo paths)."""
    content = data.model_dump()
    if isinstance(content.get("tenant"), dict):
        content["tenant"].pop("logo_path", None)
    if isinstance(content.get("contact"), dict):
        content["contact"].pop("logo_path", None)
    blob = json.dumps(content, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def brochure_cache_key(data: BrochureData, theme_id: Optional[int] = None) -> str:
    """Cache key: property + language + theme + data hash."""
    tenant_key = ""
    if data.tenant:
        tenant_key = data.tenant.site_origin or data.tenant.domain or ""
    digest = brochure_content_hash(data)
    safe_pid = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(data.property_id or "property"))[:80]
    lang = data.language or "en"
    tenant_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", tenant_key)[:40] or "tenant"
    theme_part = f"t{theme_id}" if theme_id else "t0"
    return f"brochure_{safe_pid}_{lang}_{theme_part}_{tenant_slug}_{digest}.pdf"


def _cache_dir() -> Path:
    base = getattr(settings, "REPORT_STORAGE_DIR", None) or "data/reports"
    path = Path(base)
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cached_brochure_pdf(
    data: BrochureData, theme_id: Optional[int] = None
) -> Optional[Tuple[bytes, str]]:
    key = brochure_cache_key(data, theme_id=theme_id)
    path = _cache_dir() / key
    if path.is_file():
        try:
            return path.read_bytes(), str(path)
        except Exception:
            return None
    return None


def store_cached_brochure_pdf(
    data: BrochureData, pdf_bytes: bytes, theme_id: Optional[int] = None
) -> str:
    key = brochure_cache_key(data, theme_id=theme_id)
    path = _cache_dir() / key
    path.write_bytes(pdf_bytes)
    return str(path)


def _read_theme_css(filename: str) -> str:
    path = TEMPLATES_DIR / "themes" / filename
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def _header_text(data: BrochureData) -> str:
    brand = ""
    if data.tenant and data.tenant.brand_name:
        brand = str(data.tenant.brand_name).strip()
    elif data.contact and data.contact.brand_name:
        brand = str(data.contact.brand_name).strip()
    loc = (data.location_text or "").strip()
    if brand and loc:
        return f"{brand} | {loc}"
    return brand or loc or "Brochure"


def _resolve_logo_uri(working: BrochureData) -> Optional[str]:
    logo_path = None
    if working.tenant and working.tenant.logo_path:
        logo_path = working.tenant.logo_path
    elif working.contact and working.contact.logo_path:
        logo_path = working.contact.logo_path
    if not logo_path:
        try:
            from app.services.offer_dossier_service import resolve_logo_path

            brand = working.tenant.brand_name if working.tenant else None
            logo_path = resolve_logo_path(brand)
        except Exception:
            logo_path = None
    return _file_to_data_uri(logo_path, "image/png")


def build_brochure_html(
    data: BrochureData,
    *,
    embed_remote_images: bool = True,
    copy: Optional[Any] = None,
    ai_footer: Optional[str] = None,
    theme_id: Optional[int] = None,
    use_legacy_base: bool = False,
) -> str:
    """Render the full HTML document for the brochure (useful for tests)."""
    from app.services.brochure_themes import THEMES, get_theme, pick_theme

    site_origin = None
    if data.tenant:
        site_origin = data.tenant.site_origin or data.tenant.domain

    tid = theme_id if theme_id in THEMES else pick_theme(requested=theme_id)
    spec = get_theme(tid)
    # Merge theme palette; keep tenant accents only as soft fallbacks unused by themed CSS
    tenant_fallback = _theme_for_origin(site_origin)
    theme = {**tenant_fallback, **spec.css_vars()}

    lang = (data.language or "en").lower()
    if lang not in SECTION_LABELS:
        lang = "en"
    labels = SECTION_LABELS[lang]

    working = _embed_images_in_data(data, theme) if embed_remote_images else data
    logo_uri = _resolve_logo_uri(working)
    header = _header_text(working)

    plan = build_brochure_page_plan(working, copy=copy)
    total = len(plan) or 1
    pages: List[Dict[str, Any]] = []
    for idx, page in enumerate(plan):
        from app.services.brochure_sections import BROCHURE_SECTIONS

        section = next((s for s in BROCHURE_SECTIONS if s.id == page["section_id"]), None)
        prepared = prepare_section_context(section, working, copy=copy) if section else {}
        prepared = {k: v for k, v in prepared.items() if k not in ("section_id", "data", "property")}
        if page.get("section_id") in ("intro", "overview") and working.hero_image:
            prepared.setdefault("hero_image", working.hero_image)
        page_num = idx + 1
        pages.append(
            {
                "template": page["template"],
                "section_id": page["section_id"],
                "page_index": idx,
                "page_count": total,
                "page_label": f"{page_num:02d} / {total}",
                "header_text": header,
                "prepared": prepared,
            }
        )

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    if use_legacy_base:
        template = env.get_template("base.html")
        return template.render(
            data=working,
            pages=pages,
            theme=theme,
            labels=labels,
            font_urls=_font_file_urls(),
            logo_data_uri=logo_uri,
            copy=copy,
            ai_footer=ai_footer,
            theme_id=tid,
            theme_shape=spec.shape,
            theme_layout=spec.layout,
        )

    template = env.get_template("themes/themed_base.html")
    return template.render(
        data=working,
        pages=pages,
        theme=theme,
        labels=labels,
        font_urls=_font_file_urls(),
        logo_data_uri=logo_uri,
        copy=copy,
        ai_footer=ai_footer,
        theme_id=tid,
        theme_shape=spec.shape,
        theme_layout=spec.layout,
        theme_common_css=_read_theme_css("theme_common.css"),
        theme_css=_read_theme_css(spec.css_file),
    )


def _weasyprint_html_to_pdf(html_str: str) -> bytes:
    try:
        from weasyprint import HTML
    except Exception as e:
        raise RuntimeError(
            "WeasyPrint is not available. Install weasyprint and system libraries "
            "(pango, cairo, gdk-pixbuf, fontconfig). On Debian/Ubuntu these are "
            "installed via the project Dockerfile."
        ) from e

    base_url = TEMPLATES_DIR.as_uri() + "/"
    try:
        pdf_bytes = HTML(string=html_str, base_url=base_url).write_pdf()
    except Exception as e:
        raise RuntimeError(f"WeasyPrint failed to render brochure PDF: {e}") from e
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError("WeasyPrint produced an empty or invalid PDF")
    return pdf_bytes


def render_brochure_pdf(
    data: BrochureData,
    *,
    use_cache: bool = True,
    copy: Optional[Any] = None,
    ai_footer: Optional[str] = None,
    theme_id: Optional[int] = None,
) -> bytes:
    """
    Render BrochureData to PDF bytes via WeasyPrint.
    Cache keyed by property / language / theme / data hash.
    On theme template failure, tries other themes then legacy base.html.
    """
    from app.services.brochure_themes import THEMES, fallback_theme_order, pick_theme

    tid = theme_id if theme_id in THEMES else pick_theme(requested=theme_id)

    if use_cache and copy is None:
        cached = get_cached_brochure_pdf(data, theme_id=tid)
        if cached:
            return cached[0]

    tried: List[int] = []
    last_err: Optional[Exception] = None
    candidates = [tid] + [t for t in fallback_theme_order(tid) if t != tid]

    for candidate in candidates:
        tried.append(candidate)
        try:
            html_str = build_brochure_html(
                data,
                embed_remote_images=True,
                copy=copy,
                ai_footer=ai_footer,
                theme_id=candidate,
            )
            pdf_bytes = _weasyprint_html_to_pdf(html_str)
            if len(pdf_bytes) > 8_000_000:
                logger.warning(
                    "Brochure PDF exceeds 8MB target (%s bytes) for %s theme=%s",
                    len(pdf_bytes),
                    data.property_id,
                    candidate,
                )
            if use_cache and copy is None:
                try:
                    store_cached_brochure_pdf(data, pdf_bytes, theme_id=candidate)
                except Exception as e:
                    logger.warning("Failed to cache brochure PDF: %s", e)
            if candidate != tid:
                logger.warning(
                    "brochure_theme_fallback from=%s to=%s property=%s",
                    tid,
                    candidate,
                    data.property_id,
                )
            return pdf_bytes
        except Exception as e:
            last_err = e
            logger.warning(
                "Brochure theme %s failed for %s: %s",
                candidate,
                data.property_id,
                e,
            )

    # Final fallback: legacy landscape base.html
    try:
        html_str = build_brochure_html(
            data,
            embed_remote_images=True,
            copy=copy,
            ai_footer=ai_footer,
            theme_id=tid,
            use_legacy_base=True,
        )
        pdf_bytes = _weasyprint_html_to_pdf(html_str)
        logger.warning(
            "brochure_theme_fallback to legacy base.html property=%s tried=%s",
            data.property_id,
            tried,
        )
        return pdf_bytes
    except Exception as e:
        last_err = e

    raise RuntimeError(
        f"All brochure themes failed for {data.property_id}: {last_err}"
    ) from last_err


def count_section_markers(html: str) -> Dict[str, int]:
    """Utility for tests: count SECTION:id markers in rendered HTML."""
    found = re.findall(r"SECTION:([a-z_]+)", html)
    counts: Dict[str, int] = {}
    for sid in found:
        counts[sid] = counts.get(sid, 0) + 1
    return counts
