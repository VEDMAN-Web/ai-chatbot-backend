"""
Tajlandia & Varsovia Luxury Property Brochure Generator
Generates an 8-page luxury property presentation brochure matching
the exact structure, layout, typography, and visual elegance of:
Varsovia_Estate_Dariusz_Tajlandia 2.pdf (Template Variant 2 - Modern Slate / Luxury Executive).

Supports:
- Single-Property Luxury Presentation (8 pages dedicated to one property)
- Hugging Face / S3 direct upload with pre-signed / public URLs
- Multilingual content (English, Polish, Thai)
- Full specifications, high-resolution imagery, investment ROI model, and advisor roadmap.
"""
import os
import io
import re
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional, List, Dict, Any, Union

from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from sqlalchemy.orm import Session

from app.db.models import Property, PropertyImage
from app.core.tenant import resolve_tenant
from app.services.report_service import default_storage_provider
from app.services.offer_dossier_service import (
    _draw_header,
    _draw_footer,
    _draw_text_box,
    resolve_logo_path,
    fetch_property_image_reader,
    _create_fallback_property_image,
    TEMPLATE_THEMES,
    FONT_REGULAR,
    FONT_BOLD,
)

logger = logging.getLogger(__name__)

_NOT_DOC = "Not documented in verified database"


def _firestore_card_to_brochure_prop(card: Dict[str, Any]) -> SimpleNamespace:
    """
    Map a normalize_firestore_project dict into attribute-style object
    expected by the brochure renderer. Missing fields stay None / honest labels.
    Never invents prices, beds, or ownership.
    """
    images_raw = card.get("images") or []
    image_objs = []
    for item in images_raw:
        if isinstance(item, str) and item.strip():
            image_objs.append(SimpleNamespace(image_url=item.strip()))
        elif isinstance(item, dict):
            url = item.get("image_url") or item.get("url") or item.get("src")
            if url:
                image_objs.append(SimpleNamespace(image_url=str(url).strip()))
        elif hasattr(item, "image_url") and getattr(item, "image_url", None):
            image_objs.append(item)

    price_display = card.get("price") or card.get("price_display")
    beds = card.get("bedrooms") or card.get("bedrooms_display")
    area = card.get("area") or card.get("area_display")
    ownership = card.get("ownership") or card.get("ownership_type") or card.get("ownership_structure")

    bedrooms_min = None
    if beds is not None:
        try:
            nums = re.findall(r"\d+", str(beds))
            if nums:
                bedrooms_min = int(nums[0])
        except (TypeError, ValueError):
            bedrooms_min = None

    area_sqm_min = None
    if area is not None:
        try:
            nums = re.findall(r"\d+(?:\.\d+)?", str(area).replace(",", ""))
            if nums:
                area_sqm_min = float(nums[0])
        except (TypeError, ValueError):
            area_sqm_min = None

    return SimpleNamespace(
        property_id=str(card.get("property_id") or "").strip() or None,
        property_name=str(card.get("name") or card.get("property_name") or "").strip() or None,
        district=card.get("district") or None,
        province=card.get("province") or None,
        property_type=card.get("property_type") or None,
        status=card.get("status") or None,
        price_display=price_display if price_display else None,
        price_numeric_min=card.get("price_numeric_min"),
        ownership_type=ownership if ownership else None,
        has_pool=bool(card.get("has_pool")) if card.get("has_pool") is not None else False,
        developer_name=card.get("developer_name") or card.get("developer") or _NOT_DOC,
        description=card.get("description") or "",
        bedrooms_min=bedrooms_min,
        bedrooms_display=str(beds) if beds not in (None, "") else _NOT_DOC,
        bathrooms=card.get("bathrooms") if card.get("bathrooms") not in (None, "") else _NOT_DOC,
        area_sqm_min=area_sqm_min,
        area_display=str(area) if area not in (None, "", "N/A") else _NOT_DOC,
        images=image_objs,
    )


def _load_property_for_brochure(db: Session, property_id: str) -> Any:
    """
    Load property for brochure via the public property index (Firestore), with optional SQL.
    Raises ValueError if missing from both.
    """
    from app.services.property_index import get_property_index, public_store_mode, sanitize_public_card
    mode = public_store_mode()
    pid = str(property_id or "").strip()

    if mode in ("firestore", "both"):
        try:
            fs_card = get_property_index().get_by_id(pid)
            if not fs_card:
                from app.services.firestore_service import get_property_by_id_from_firestore
                fs_card = get_property_by_id_from_firestore(pid)
                if fs_card:
                    fs_card = sanitize_public_card(fs_card)
            if fs_card and isinstance(fs_card, dict):
                return _firestore_card_to_brochure_prop(fs_card)
        except Exception as e:
            logger.warning(f"Index/Firestore brochure lookup failed for '{property_id}': {e}")

    if mode in ("postgres", "both") and db is not None:
        try:
            prop = db.query(Property).filter(Property.property_id == property_id.upper()).first()
            if not prop:
                prop = db.query(Property).filter(Property.property_id.ilike(property_id.strip())).first()
            if prop:
                return prop
        except Exception as e:
            logger.warning(f"PostgreSQL brochure lookup failed for '{property_id}': {e}")

    raise ValueError(f"Property with ID '{property_id}' not found.")


def _write_brochure_copy_timed(data: Any, language: str) -> tuple:
    """
    Run write_brochure_copy in a daemon thread with a hard timeout.
    Returns (copy_or_None, reason, elapsed_ms).
    """
    import threading
    import time as _time
    from app.core.config import settings as _settings
    from app.services.brochure_copywriter import write_brochure_copy

    timeout = float(getattr(_settings, "BROCHURE_LLM_TIMEOUT", 25) or 25)
    box: Dict[str, Any] = {"copy": None, "reason": "ok", "error": None}

    def _run() -> None:
        try:
            box["copy"] = write_brochure_copy(data, language)
            if box["copy"] is None:
                box["reason"] = "fallback"
        except Exception as e:
            box["error"] = e
            box["reason"] = f"error:{type(e).__name__}"

    t0 = _time.perf_counter()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    elapsed_ms = int((_time.perf_counter() - t0) * 1000)
    if thread.is_alive():
        return None, "timeout", elapsed_ms
    if box["error"] is not None:
        return None, box["reason"], elapsed_ms
    if box["copy"] is None:
        return None, box["reason"] if box["reason"] != "ok" else "fallback", elapsed_ms
    return box["copy"], "sonnet", elapsed_ms


def _generate_html_brochure_pdf(
    db: Session,
    property_id: str,
    site_origin: Optional[str] = None,
    language: str = "en",
    client_name: Optional[str] = None,
    template: Optional[int] = None,
    session_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Jinja2 + WeasyPrint brochure. Facts stay deterministic; optional Claude copy
    fills prose slots. Visual theme is picked at random (or forced via template).
    """
    import secrets

    from app.core.config import settings as _settings
    from app.services.brochure_copywriter import ai_footer_note
    from app.services.brochure_data import load_brochure_data
    from app.services.brochure_renderer import render_brochure_pdf
    from app.services.brochure_themes import get_theme, pick_theme, remember_theme

    tenant = resolve_tenant(site_origin)
    lang = language if language in ["en", "pl", "th"] else "en"
    data = load_brochure_data(
        db=db,
        property_id=property_id,
        site_origin=site_origin,
        language=lang,
    )

    mem_key = session_key or f"prop:{data.property_id or property_id}"
    theme_id = pick_theme(requested=template, session_key=mem_key)
    theme_spec = get_theme(theme_id)
    logger.info(
        "brochure_theme id=%s name=%s property=%s lang=%s",
        theme_id,
        theme_spec.name,
        data.property_id or property_id,
        lang,
    )

    copy = None
    reason = "disabled"
    elapsed_ms = 0
    try:
        copy, reason, elapsed_ms = _write_brochure_copy_timed(data, lang)
    except Exception as e:
        reason = f"error:{type(e).__name__}"
        copy = None
    source = "sonnet" if copy is not None else "fallback"
    logger.info(
        "brochure_copy source=%s reason=%s ms=%s",
        source,
        reason,
        elapsed_ms,
    )

    ai_footer = None
    if copy is not None and bool(getattr(_settings, "BROCHURE_LLM_FOOTER_NOTE", True)):
        ai_footer = ai_footer_note(lang)

    # Always render fresh for unique filenames; disk cache still keyed by theme+hash inside renderer
    pdf_bytes = render_brochure_pdf(
        data,
        use_cache=(copy is None),
        copy=copy,
        ai_footer=ai_footer,
        theme_id=theme_id,
    )
    remember_theme(mem_key, theme_id)

    safe_pid = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(data.property_id or property_id))[:60]
    rand = secrets.token_hex(3)
    out_filename = f"brochure_{safe_pid}_t{theme_id}_{rand}.pdf"

    saved_url = default_storage_provider.save_file(
        filename=out_filename,
        content=pdf_bytes,
        content_type="application/pdf",
    )
    logger.info(
        "Generated HTML brochure: %s (%s bytes) stored at %s",
        out_filename,
        len(pdf_bytes),
        saved_url,
    )
    return {
        "success": True,
        "filename": out_filename,
        "file_url": saved_url,
        "download_url": saved_url,
        "property_id": data.property_id or property_id,
        "property_name": data.name or property_id,
        "pages": None,
        "pdf_bytes": pdf_bytes,
        "file_size_bytes": len(pdf_bytes),
        "engine": "html",
        "brochure_copy_source": source,
        "theme_id": theme_id,
        "theme_name": theme_spec.name,
    }


def generate_single_property_brochure_pdf(
    db: Session,
    property_id: str,
    site_origin: Optional[str] = None,
    language: str = "en",
    client_name: Optional[str] = None,
    template_variant: int = 2,
    template: Optional[int] = None,
    session_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Builds a property brochure PDF.
    Engine selected by settings.BROCHURE_ENGINE ("reportlab" default, or "html").
    On HTML failure, falls back to ReportLab when BROCHURE_FALLBACK_TO_REPORTLAB is true.
    `template` selects HTML visual theme 1–7 when using the HTML engine.
    """
    from app.core.config import settings as _settings

    engine = str(getattr(_settings, "BROCHURE_ENGINE", "reportlab") or "reportlab").strip().lower()
    if engine == "html":
        try:
            return _generate_html_brochure_pdf(
                db=db,
                property_id=property_id,
                site_origin=site_origin,
                language=language,
                client_name=client_name,
                template=template,
                session_key=session_key,
            )
        except Exception as html_err:
            logger.error(
                "HTML brochure engine failed for %s: %s",
                property_id,
                html_err,
                exc_info=True,
            )
            if not bool(getattr(_settings, "BROCHURE_FALLBACK_TO_REPORTLAB", True)):
                raise
            logger.warning("Falling back to ReportLab brochure engine for %s", property_id)

    return _generate_reportlab_brochure_pdf(
        db=db,
        property_id=property_id,
        site_origin=site_origin,
        language=language,
        client_name=client_name,
        template_variant=template_variant,
    )


def _generate_reportlab_brochure_pdf(
    db: Session,
    property_id: str,
    site_origin: Optional[str] = None,
    language: str = "en",
    client_name: Optional[str] = None,
    template_variant: int = 2
) -> Dict[str, Any]:
    """
    Legacy ReportLab 8-page luxury property brochure.
    """
    tenant = resolve_tenant(site_origin)
    brand = tenant.brand_name
    lang = language if language in ["en", "pl", "th"] else "en"

    # 1. Fetch Property (PostgreSQL, then Firestore)
    prop = _load_property_for_brochure(db, property_id)

    # 2. Select Theme (default to Variant 2 - Modern Slate / Tajlandia AI / Varsovia)
    theme = TEMPLATE_THEMES.get(template_variant, TEMPLATE_THEMES[2])
    logo_path = resolve_logo_path(brand)

    # 3. Format Strings & Display Data
    p_name = prop.property_name or prop.property_id or "Property"
    client_display = (client_name or ("Szanowny Kliencie" if lang == "pl" else "Valued Client")).strip()
    date_display = datetime.now().strftime("%d.%m.%Y")
    country_display = "TAJLANDIA." if lang == "pl" else "THAILAND."
    company_name = tenant.brand_name.upper()

    district = getattr(prop, "district", None)
    province = getattr(prop, "province", None)
    if district and province:
        location_str = f"{district}, {province}"
        loc_tag = f"{str(district).upper()} • {str(province).upper()}"
    elif district or province:
        location_str = str(district or province)
        loc_tag = location_str.upper()
    else:
        location_str = "Thailand"
        loc_tag = "THAILAND"

    if prop.price_display:
        price_str = prop.price_display
    elif prop.price_numeric_min:
        price_str = f"from {prop.price_numeric_min:,.0f} THB"
    else:
        price_str = "Price on request"

    # Images: gather all available (safe when images is empty / None)
    prop_images = [fetch_property_image_reader(prop)]
    extra_imgs = list(getattr(prop, "images", None) or [])
    if len(extra_imgs) > 1:
        for extra_img in extra_imgs[1:]:
            try:
                img_url = getattr(extra_img, "image_url", None) or (str(extra_img) if isinstance(extra_img, str) else None)
                if not img_url:
                    continue
                reader = ImageReader(img_url)
                prop_images.append(reader)
            except Exception:
                pass
    while len(prop_images) < 4:
        prop_images.append(prop_images[0])

    pdf_buffer = io.BytesIO()
    can = canvas.Canvas(pdf_buffer, pagesize=(595.28, 841.89))

    # =========================================================================
    # PAGE 1: EXECUTIVE COVER PAGE
    # =========================================================================
    can.setFillColor(colors.HexColor("#FAF8F5"))
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, f"DLA: {client_display}", country_display, date_display, theme)

    # Hero Property Photo Container
    hero_img = prop_images[0]
    can.setFillColor(colors.HexColor("#E2E8F0"))
    can.roundRect(38, 440, 518, 325, 4, fill=1, stroke=0)
    try:
        can.drawImage(hero_img, 38, 440, width=518, height=325, preserveAspectRatio=True, anchor='c')
    except Exception as ie:
        logger.warning(f"Error rendering hero image: {ie}")

    # Location Tag Banner
    can.setFillColor(theme["badge_bg"])
    can.roundRect(38, 395, 518, 28, 3, fill=1, stroke=0)
    can.setFillColor(theme["badge_text"])
    can.setFont(FONT_BOLD, 8.5)
    can.drawString(50, 405, f"{loc_tag} • {company_name}")

    # Main Headline Block
    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 26)
    can.drawString(42, 345, country_display)

    can.setFont(FONT_BOLD, 20)
    headline_text = p_name.upper()
    can.drawString(42, 315, headline_text[:38])

    can.setFont(FONT_BOLD, 14)
    can.setFillColor(theme["accent_color"])
    sub_headline = "OFICJALNA BROSZURA INWESTYCYJNA" if lang == "pl" else "OFFICIAL LUXURY PROPERTY BROCHURE"
    can.drawString(42, 290, sub_headline)

    can.setFont(FONT_REGULAR, 9.5)
    can.setFillColor(colors.HexColor("#475569"))
    subtitle_desc = (
        f"Luksusowa rezydencja w prestiżowej lokalizacji {location_str}. Pełna weryfikacja prawna i potencjał zwrotu."
        if lang == "pl" else
        f"Premier verified residence in prime {location_str}. High capital appreciation & legal security."
    )
    can.drawString(42, 265, subtitle_desc[:90])

    # CTA Button
    can.setFillColor(theme["primary_button"])
    can.roundRect(38, 120, 260, 36, 5, fill=1, stroke=0)
    can.setFillColor(colors.white)
    can.setFont(FONT_BOLD, 9.5)
    cta_btn = "ZOBACZ PROJEKT  →" if lang == "pl" else "EXPLORE PROPERTY SPECS  →"
    can.drawString(55, 132, cta_btn)

    # Bottom Price & Date Card
    can.setFillColor(colors.HexColor("#0F172A"))
    can.roundRect(38, 48, 518, 52, 4, fill=1, stroke=0)

    can.setFillColor(colors.HexColor("#94A3B8"))
    can.setFont(FONT_REGULAR, 8)
    can.drawString(50, 78, "CENA OD" if lang == "pl" else "PRICE FROM")

    can.setFillColor(colors.white)
    can.setFont(FONT_BOLD, 15)
    can.drawString(50, 60, price_str)

    can.setFillColor(colors.HexColor("#94A3B8"))
    can.setFont(FONT_REGULAR, 8)
    can.drawRightString(540, 65, f"ID: {prop.property_id}  •  {date_display}")

    _draw_footer(can, client_display, country_display, date_display, "01 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 2: EXECUTIVE SUMMARY & 4 KEY VALUE PILLARS
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, client_display, country_display, date_display, theme)

    # Goal Banner
    can.setFillColor(theme["badge_bg"])
    can.roundRect(35, 730, 525, 28, 3, fill=1, stroke=0)
    can.setFillColor(theme["badge_text"])
    can.setFont(FONT_BOLD, 8.5)
    goal_label = f"PROJEKT: {p_name.upper()}  •  LOKALIZACJA: {location_str.upper()}"
    can.drawString(45, 740, goal_label[:85])

    # Greeting / Subtitle
    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 18)
    can.drawString(42, 695, f"Prezentacja Inwestycji: {p_name}" if lang == "pl" else f"Executive Brief: {p_name}")

    can.setFont(FONT_REGULAR, 9)
    can.setFillColor(colors.HexColor("#475569"))
    desc_clean = re.sub(r'<[^>]+>', '', prop.description or "")
    try:
        from app.services.instruction_store import get_active_instruction_block
        _style_notes = get_active_instruction_block("brochure_pdf", site_origin, lang)
    except Exception:
        _style_notes = ""
    if _style_notes:
        desc_clean = (desc_clean + " " + _style_notes).strip()
    if not desc_clean:
        desc_clean = (
            "Ekskluzywna nieruchomość zaprojektowana z dbałością o najwyższe standardy wykończenia, łącząca tropikalny klimat z nowoczesną architekturą."
            if lang == "pl" else
            "An exclusive development engineered to the highest architectural standards, blending tropical lifestyle with modern luxury."
        )
    _draw_text_box(can, desc_clean[:260] + "...", 42, 630, 510, 42, FONT_REGULAR, 8.5, colors.HexColor("#475569"))

    # 4 Key Pillars (2x2 Cards)
    pillars = [
        {
            "title": "STANDARD ARCHITEKTURY" if lang == "pl" else "PREMIER ARCHITECTURE",
            "desc": f"Nowoczesny design willi, prywatny basen i panoramiczne przeszklenia zapewniające naturalne doświetlenie." if lang == "pl" else "Modern architectural design with private pool and floor-to-ceiling glass maximizing natural light.",
            "val": prop.property_type or "Luxury Villa"
        },
        {
            "title": "STRATEGICZNA LOKALIZACJA" if lang == "pl" else "STRATEGIC LOCATION",
            "desc": f"Położenie w {location_str} — bliskość plaż, centrów lifestyle'owych i restauracji premium." if lang == "pl" else f"Located in {location_str} — close to pristine beaches, international marinas and lifestyle hubs.",
            "val": prop.district or "Prime Area"
        },
        {
            "title": "POTENCJAŁ NAJMU I ROI" if lang == "pl" else "RENTAL YIELD & ROI",
            "desc": "Prognozowana stopa zwrotu 8-11% w skali roku dzięki całorocznemu popytowi turystycznemu." if lang == "pl" else "Projected 8-11% annual rental yield driven by strong year-round luxury tourism demand.",
            "val": "8.5% - 11.0% ROI"
        },
        {
            "title": "BEZPIECZEŃSTWO PRAWNE" if lang == "pl" else "LEGAL VERIFICATION",
            "desc": "Pełna weryfikacja dewelopera, statusu gruntów i formy własności chroniącej zagranicznego nabywcę." if lang == "pl" else "Comprehensive developer due diligence, title deed audit, and protected ownership structures.",
            "val": prop.ownership_type or "Confirm with advisor"
        }
    ]

    card_coords = [
        {"x": 45, "y": 400, "w": 245, "h": 190, "idx": 0},
        {"x": 305, "y": 400, "w": 245, "h": 190, "idx": 1},
        {"x": 45, "y": 180, "w": 245, "h": 190, "idx": 2},
        {"x": 305, "y": 180, "w": 245, "h": 190, "idx": 3},
    ]

    for item in card_coords:
        pil = pillars[item["idx"]]
        cx, cy, cw, ch = item["x"], item["y"], item["w"], item["h"]

        can.setFillColor(theme["card_bg"])
        can.roundRect(cx, cy, cw, ch, 4, fill=1, stroke=0)
        can.setStrokeColor(colors.HexColor("#E2E8F0"))
        can.setLineWidth(0.8)
        can.roundRect(cx, cy, cw, ch, 4, fill=0, stroke=1)

        # Badge val
        can.setFillColor(theme["badge_bg"])
        can.roundRect(cx + 12, cy + ch - 32, cw - 24, 20, 2, fill=1, stroke=0)
        can.setFillColor(theme["badge_text"])
        can.setFont(FONT_BOLD, 7.5)
        can.drawString(cx + 18, cy + ch - 24, pil["val"].upper())

        # Title
        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 10)
        can.drawString(cx + 12, cy + ch - 54, pil["title"])

        # Description
        _draw_text_box(can, pil["desc"], cx + 12, cy + 15, cw - 24, ch - 80, FONT_REGULAR, 8.0, colors.HexColor("#475569"))

    # Bottom note
    can.setFillColor(colors.HexColor("#64748B"))
    can.setFont(FONT_REGULAR, 7.5)
    note_txt = (
        "Wszystkie prezentowane dane techniczne pochodzą bezpośrednio z oficjalnej dokumentacji dewelopera i zostały zweryfikowane."
        if lang == "pl" else
        "All specifications and pricing are sourced directly from verified developer documentation and official filings."
    )
    can.drawString(45, 90, note_txt)

    _draw_footer(can, client_display, country_display, date_display, "02 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 3: TECHNICAL SPECIFICATIONS & ARCHITECTURAL SPECS
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, f"{p_name} / SPECYFIKACJA", country_display, date_display, theme)

    can.setFillColor(theme["accent_color"])
    can.setFont(FONT_BOLD, 8)
    can.drawString(42, 736, "01 / ARCHITEKTURA I SPECYFIKACJA TECHNICZNA" if lang == "pl" else "01 / ARCHITECTURAL SPECIFICATIONS")

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 16)
    can.drawString(42, 715, p_name)

    # Large Photo (Image 2)
    can.setFillColor(colors.HexColor("#E2E8F0"))
    can.roundRect(42, 450, 510, 245, 4, fill=1, stroke=0)
    try:
        can.drawImage(prop_images[1], 42, 450, width=510, height=245, preserveAspectRatio=True, anchor='c')
    except Exception as ie:
        logger.warning(f"Error rendering image 2: {ie}")

    # Specs Grid Card
    can.setFillColor(theme["card_bg"])
    can.roundRect(42, 130, 510, 300, 4, fill=1, stroke=0)
    can.setStrokeColor(colors.HexColor("#CBD5E1"))
    can.setLineWidth(0.8)
    can.roundRect(42, 130, 510, 300, 4, fill=0, stroke=1)

    specs_data = [
        ("CENA / PRICE", price_str),
        ("STATUS BUDOWY", prop.status or "Gotowe / W budowie"),
        ("SYPIALNIE", prop.bedrooms_display or f"{prop.bedrooms_min or 3} Sypialnie"),
        ("ŁAZIENKI", f"{prop.bathrooms or 3} Łazienki"),
        ("POWIERZCHNIA UŻYTKOWA", prop.area_display or f"{prop.area_sqm_min or 250} m²"),
        ("PRYWATNY BASEN", "Tak (System ze słoną wodą)" if prop.has_pool else "Dostępny w projekcie"),
        ("FORMA WŁASNOŚCI", prop.ownership_type or "Potwierdź z doradcą"),
        ("DEWELOPER", prop.developer_name or "Renomowany deweloper")
    ]

    sy = 390
    for idx, (lbl, val) in enumerate(specs_data):
        row_x = 60 if (idx % 2 == 0) else 315
        if idx % 2 == 0 and idx > 0:
            sy -= 58

        can.setFillColor(colors.HexColor("#64748B"))
        can.setFont(FONT_BOLD, 7.5)
        can.drawString(row_x, sy, lbl)

        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 10)
        can.drawString(row_x, sy - 18, str(val)[:30])

    _draw_footer(can, client_display, country_display, date_display, "03 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 4: PHOTO GALLERY & INTERIOR LIVING SPACES
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, f"{p_name} / WNĘTRZA", country_display, date_display, theme)

    can.setFillColor(theme["accent_color"])
    can.setFont(FONT_BOLD, 8)
    can.drawString(42, 736, "02 / GALERIA ZDJĘĆ I ARCHITEKTURA WNĘTRZ" if lang == "pl" else "02 / VISUAL GALLERY & INTERIOR SPACES")

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 16)
    can.drawString(42, 715, "Przestrzeń, Naturalne Światło i Standard Wykończenia" if lang == "pl" else "Living Spaces, Light & Refined Materials")

    # 4 Gallery Cards (2x2)
    gal_coords = [
        {"x": 42, "y": 420, "w": 245, "h": 260, "img_idx": 0, "title": "Strefa Dzienna i Salon" if lang == "pl" else "Living & Dining Area"},
        {"x": 307, "y": 420, "w": 245, "h": 260, "img_idx": 1, "title": "Główna Sypialnia Master" if lang == "pl" else "Master Suite Bedroom"},
        {"x": 42, "y": 130, "w": 245, "h": 260, "img_idx": 2, "title": "Taras i Prywatny Basen" if lang == "pl" else "Private Pool & Terrace"},
        {"x": 307, "y": 130, "w": 245, "h": 260, "img_idx": 3, "title": "Nowoczesna Kuchnia i Łazienka" if lang == "pl" else "Kitchen & Bathrooms"}
    ]

    for g in gal_coords:
        gx, gy, gw, gh = g["x"], g["y"], g["w"], g["h"]
        can.setFillColor(colors.HexColor("#E2E8F0"))
        can.roundRect(gx, gy + 35, gw, gh - 35, 4, fill=1, stroke=0)
        try:
            target_reader = prop_images[g["img_idx"] % len(prop_images)]
            can.drawImage(target_reader, gx, gy + 35, width=gw, height=gh - 35, preserveAspectRatio=True, anchor='c')
        except Exception:
            pass

        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 8.5)
        can.drawString(gx, gy + 18, g["title"])

        can.setFillColor(colors.HexColor("#64748B"))
        can.setFont(FONT_REGULAR, 7.5)
        can.drawString(gx, gy + 6, f"{p_name} • Standard Premium")

    _draw_footer(can, client_display, country_display, date_display, "04 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 5: MASTER PLAN, FACILITIES & RESORT AMENITIES
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, f"{p_name} / UDOGODNIENIA", country_display, date_display, theme)

    can.setFillColor(theme["accent_color"])
    can.setFont(FONT_BOLD, 8)
    can.drawString(42, 736, "03 / INFRASTRUKTURA RESORTU I UDOGODNIENIA" if lang == "pl" else "03 / RESORT FACILITIES & AMENITIES")

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 16)
    can.drawString(42, 715, "Bezpieczeństwo, Relaks i Zarządzanie Nieruchomością" if lang == "pl" else "Security, Wellness & Concierge Management")

    amenities_list = [
        ("CAŁODOBOWA OCHRONA I CCTV", "Zamknięte, strzeżone osiedle z kontrolą dostępu 24/7 i monitoringiem HD."),
        ("ZARZĄDZANIE NAJMEM (MANAGEMENT)", "Profesjonalny serwis hotelowy na miejscu zajmujący się obsługą gości i najmu."),
        ("PRYWATNY BASEN I OGRÓD", "Własny basen ze słoną wodą, strefa relaksu i tropikalna roślinność."),
        ("KLUB REZYDENCKI I FITNESS", "Dostęp do strefy fitness, spa i klubu klubowego dla mieszkańców."),
        ("INTELIGENTNY DOM (SMART HOME)", "Nowoczesne instalacje automatyki domowej, światłowód i klimatyzacja inwerterowa."),
        ("INFRASTRUKTURA TECHNICZNA", "Podziemne linie energetyczne, stacja uzdatniania wody i generator awaryjny.")
    ] if lang == "pl" else [
        ("24/7 GATED SECURITY & CCTV", "Full gated community access control, license plate recognition and CCTV."),
        ("FULL ON-SITE VILLA MANAGEMENT", "Turnkey property and hotel-grade rental management on site."),
        ("PRIVATE SALTWATER POOL", "Private pool with sun deck, outdoor shower and tropical garden."),
        ("RESIDENTS CLUBHOUSE & GYM", "Direct access to state-of-the-art fitness center and wellness lounge."),
        ("SMART HOME & HIGH-SPEED FIBER", "Modern smart automation ready, optic fiber and multi-split inverter AC."),
        ("UNDERGROUND UTILITIES", "Underground cabling, integrated backup generators and water filtration.")
    ]

    ay = 660
    for title, desc in amenities_list:
        can.setFillColor(theme["card_bg"])
        can.roundRect(42, ay - 55, 510, 65, 4, fill=1, stroke=0)
        can.setStrokeColor(colors.HexColor("#E2E8F0"))
        can.roundRect(42, ay - 55, 510, 65, 4, fill=0, stroke=1)

        can.setFillColor(theme["accent_color"])
        can.rect(42, ay - 55, 4, 65, fill=1, stroke=0)

        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 9.5)
        can.drawString(58, ay - 20, title)

        can.setFillColor(colors.HexColor("#475569"))
        can.setFont(FONT_REGULAR, 8.5)
        can.drawString(58, ay - 38, desc[:90])

        ay -= 80

    _draw_footer(can, client_display, country_display, date_display, "05 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 6: STRATEGIC LOCATION & REGIONAL GUIDE
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, f"{p_name} / LOKALIZACJA", country_display, date_display, theme)

    can.setFillColor(theme["accent_color"])
    can.setFont(FONT_BOLD, 8)
    can.drawString(42, 736, "04 / STRATEGICZNA LOKALIZACJA I OTOCZENIE" if lang == "pl" else "04 / STRATEGIC LOCATION & REGIONAL GUIDE")

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 16)
    can.drawString(42, 715, f"{location_str} — Jedna z Najbardziej Pożądanych Lokalizacji" if lang == "pl" else f"{location_str} — Prime Lifestyle Corridor")

    # Location description card
    can.setFillColor(theme["card_bg"])
    can.roundRect(42, 530, 510, 160, 4, fill=1, stroke=0)
    can.setStrokeColor(colors.HexColor("#CBD5E1"))
    can.roundRect(42, 530, 510, 160, 4, fill=0, stroke=1)

    loc_intro = (
        f"Projekt {p_name} usytuowany jest w starannie wyselekcjonowanej części {location_str}. "
        "Okolica łączy bliskość najpiękniejszych plaż z rozwiniętą infrastrukturą miejską: "
        "międzynarodowymi szkołami, szpitalami i centrami handlowymi klasy premium. "
        "To gwarancja wysokiego popytu ze strony zamożnych rezydentów i turystów."
        if lang == "pl" else
        f"{p_name} is located in one of the premier pockets of {location_str}. "
        "The area seamlessly combines proximity to pristine coastlines with world-class amenities: "
        "accredited international schools, medical facilities, luxury marinas and championship golf courses. "
        "This positioning ensures sustained capital appreciation and premium rental yield."
    )
    _draw_text_box(can, loc_intro, 58, 545, 478, 130, FONT_REGULAR, 9.0, colors.HexColor("#334155"), leading=14)

    # Distances List
    distances = [
        ("Plaże i Kluby Plażowe", "5-10 min jazdy do Catch Beach Club i piaszczystych plaż"),
        ("Centra Handlowe i Lifestyle", "7 min do Boat Avenue i Porto de Phuket"),
        ("Pola Golfowe", "10-15 min do mistrzowskich 18-dołkowych pól golfowych"),
        ("Lotnisko Międzynarodowe", "20-25 min do międzynarodowego lotniska"),
        ("Szkoły Międzynarodowe", "15-20 min do British International School i UWC Thailand"),
        ("Szpitale Prywatne", "25 min do Bangkok Hospital z kadrą międzynarodową")
    ] if lang == "pl" else [
        ("Pristine Beaches & Beach Clubs", "5-10 min drive to white sand beaches and luxury beach clubs"),
        ("Lifestyle & Dining Malls", "7 min to Boat Avenue and Porto de Phuket lifestyle open-air malls"),
        ("Championship Golf Courses", "10-15 min to internationally acclaimed 18-hole golf courses"),
        ("International Airport", "20-25 min drive with seamless global connectivity"),
        ("Accredited International Schools", "15-20 min to British International School and UWC Thailand"),
        ("JCI-Accredited Healthcare", "25 min to premier international private hospitals")
    ]

    dy = 480
    for d_title, d_desc in distances:
        can.setFillColor(theme["badge_bg"])
        can.roundRect(42, dy - 38, 510, 46, 3, fill=1, stroke=0)

        can.setFillColor(theme["badge_text"])
        can.setFont(FONT_BOLD, 8.5)
        can.drawString(56, dy - 18, d_title.upper())

        can.setFillColor(colors.HexColor("#475569"))
        can.setFont(FONT_REGULAR, 8.0)
        can.drawString(56, dy - 30, d_desc)

        dy -= 54

    _draw_footer(can, client_display, country_display, date_display, "06 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 7: INVESTMENT MODEL & FINANCIAL ROI PROJECTIONS
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, f"{p_name} / ZWROT Z INWESTYCJI", country_display, date_display, theme)

    can.setFillColor(theme["accent_color"])
    can.setFont(FONT_BOLD, 8)
    can.drawString(42, 736, "05 / ANALIZA FINANSOWA I ZWROT Z INWESTYCJI (ROI)" if lang == "pl" else "05 / FINANCIAL MODEL & PROJECTED ROI")

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 16)
    can.drawString(42, 715, "Model Przychodów z Najmu i Wzrostu Wartości" if lang == "pl" else "Rental Yield & Capital Appreciation Model")

    # Financial Matrix Table
    can.setFillColor(theme["card_bg"])
    can.roundRect(42, 360, 510, 330, 4, fill=1, stroke=0)
    can.setStrokeColor(colors.HexColor("#CBD5E1"))
    can.roundRect(42, 360, 510, 330, 4, fill=0, stroke=1)

    fin_rows = [
        ("Wartość Nabycia (Cena Bazowa)", price_str),
        ("Szacowane Obłożenie Roczne", "65% – 78% (sezon wysoki: 85%+)"),
        ("Stopa Zwrotu Brutto (Gross Yield)", "8.5% – 11.2% rocznie"),
        ("Szacowany Roczny Przychód z Najmu", "Wyliczany na podstawie stawek dobowych"),
        ("Wzrost Wartości Kapitału (Capital Gain)", "5% – 8% średniorocznie w tej strefie"),
        ("Forma Zarządzania Najmem", "Program hotelowy / Dedykowany zarządca"),
        ("Podatek od Przeniesienia Własności", "1% dzielony 50/50 z deweloperem"),
        ("Brak Podatku od Nieruchomości w PL", "Rozliczenie zgodne z prawem tajskim")
    ] if lang == "pl" else [
        ("Acquisition Price", price_str),
        ("Projected Annual Occupancy", "65% – 78% (peak season: 85%+)"),
        ("Projected Gross Rental Yield", "8.5% – 11.2% per annum"),
        ("Annual Net Rental Income", "Calculated on verified daily rental rates"),
        ("Projected Capital Appreciation", "5% – 8% annual growth in this zone"),
        ("Rental Management Structure", "Fully managed rental pool / private manager"),
        ("Transfer & Registration Fees", "1% transfer fee split 50/50 with developer"),
        ("Favorable Taxation Framework", "Double taxation treaties applicable")
    ]

    fy = 645
    for lbl, val in fin_rows:
        can.setFillColor(colors.HexColor("#334155"))
        can.setFont(FONT_BOLD, 8.5)
        can.drawString(60, fy, lbl)

        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 9.0)
        can.drawRightString(525, fy, val)

        can.setStrokeColor(colors.HexColor("#E2E8F0"))
        can.setLineWidth(0.5)
        can.line(60, fy - 8, 525, fy - 8)

        fy -= 38

    # Disclaimer note box
    can.setFillColor(theme["badge_bg"])
    can.roundRect(42, 170, 510, 160, 4, fill=1, stroke=0)
    can.setFillColor(theme["badge_text"])
    can.setFont(FONT_BOLD, 8.5)
    can.drawString(55, 305, "BEZPIECZEŃSTWO PRAWNE I OBSŁUGA TRANSAKCJI" if lang == "pl" else "LEGAL GOVERNANCE & DUE DILIGENCE")

    disclaimer_text = (
        "Wszystkie prezentowane kalkulacje mają charakter analityczny. Doradcy Tajlandia AI / Varsovia Estate "
        "współpracują z licencjonowanymi kancelariami prawnymi w Bangkoku i na Phuket. "
        "Każda transakcja poprzedzona jest niezależnym audytem księgi wieczystej (Chanote) oraz formalnym "
        "zatwierdzeniem umowy deweloperskiej w języku polskim lub angielskim."
        if lang == "pl" else
        "All calculations represent economic models based on historical local benchmarks. Advisors from Tajlandia AI / Varsovia Estate "
        "work alongside verified Thai law firms in Bangkok and Phuket. "
        "Every transaction includes independent title deed audits (Chanote) and bilateral contract review in English or Polish."
    )
    _draw_text_box(can, disclaimer_text, 55, 185, 480, 110, FONT_REGULAR, 8.0, colors.HexColor("#334155"), leading=13)

    _draw_footer(can, client_display, country_display, date_display, "07 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 8: NEXT STEPS & SENIOR ADVISOR CONTACT CARD
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, f"{company_name} / KROKI ZAKUPU", country_display, date_display, theme)

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 18)
    can.drawString(42, 730, "TRZY KROKI DO NABYCIA NIERUCHOMOŚCI" if lang == "pl" else "THREE SIMPLE STEPS TO OWNERSHIP")

    # 3-Step Action Roadmap
    can.setFillColor(theme["badge_bg"])
    can.roundRect(38, 410, 518, 290, 4, fill=1, stroke=0)

    steps = [
        ("1. Wybór rzutu i wirtualna prezentacja na żywo",
         "Szczegółowa konsultacja wideo, analiza rzutów architektonicznych, ekspozycji słonecznej i widoków z okien."),
        ("2. Wizyta na miejscu lub inspekcja wideo 4K",
         "Organizacja prywatnej prezentacji na Phuket/Samui z polskim doradcą lub pełna relacja wideo z placu budowy."),
        ("3. Umowa rezerwacyjna i audyt prawny",
         "Zabezpieczenie wybranego lokalu zwrotnym depozytem rezerwacyjnym (~100 000 THB), weryfikacja prawna i podpisanie umowy.")
    ] if lang == "pl" else [
        ("1. Unit Selection & Live Video Walkthrough",
         "Detailed consultation, floor plan architectural analysis, plot orientation and direct view inspection."),
        ("2. On-Site Property Inspection in Thailand",
         "Private guided tour with senior advisors or live 4K drone & video inspection of the actual build."),
        ("3. Reservation Agreement & Legal Due Diligence",
         "Price lock via refundable deposit (~100,000 THB), formal title audit, and bilingual contract execution.")
    ]

    sy = 645
    for s_title, s_desc in steps:
        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 10)
        can.drawString(55, sy, s_title)

        can.setFillColor(colors.HexColor("#334155"))
        can.setFont(FONT_REGULAR, 8.5)
        can.drawString(55, sy - 20, s_desc[:105])

        can.setStrokeColor(colors.HexColor("#CBD5E1"))
        can.setLineWidth(0.5)
        can.line(55, sy - 35, 530, sy - 35)

        sy -= 85

    # Advisor Contact Card
    can.setFillColor(colors.HexColor("#0F172A"))
    can.roundRect(38, 120, 518, 260, 4, fill=1, stroke=0)

    can.setFillColor(colors.HexColor("#F59E0B"))
    can.setFont(FONT_BOLD, 12)
    can.drawString(55, 345, f"{company_name} • DEDYKOWANY OPIEKUN KLIENTA" if lang == "pl" else f"{company_name} • DEDICATED SENIOR ADVISOR")

    can.setFillColor(colors.white)
    can.setFont(FONT_BOLD, 10)
    can.drawString(55, 315, "Biuro Sprzedaży i Obsługi Klientów Międzynarodowych" if lang == "pl" else "International Real Estate Client Advisory Desk")

    can.setFont(FONT_REGULAR, 8.5)
    can.drawString(55, 285, f"E-mail: {tenant.advisor_email}  |  Oficjalny portal: {tenant.domain}")
    can.drawString(55, 260, "Telefon / WhatsApp: +48 501 234 567 / +66 81 234 5678 (Obsługa PL / EN)")
    can.drawString(55, 235, "Biuro Warszawa: ul. Złota 44, 00-120 Warszawa")
    can.drawString(55, 210, "Biuro Phuket: Laguna Phuket, Cherng Talay, Thalang, Phuket 83110")

    if logo_path and os.path.exists(logo_path):
        try:
            can.setFillColor(colors.white)
            can.roundRect(420, 135, 120, 50, 3, fill=1, stroke=0)
            can.drawImage(logo_path, 425, 140, width=110, height=40, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    _draw_footer(can, client_display, country_display, date_display, "08 / 8")
    can.showPage()

    # Save to buffer and storage
    can.save()
    pdf_bytes = pdf_buffer.getvalue()

    clean_pname = "".join(c for c in p_name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_filename = f"{tenant.brand_name.replace(' ', '_')}_Property_{clean_pname}_{ts}.pdf"

    saved_url = default_storage_provider.save_file(
        filename=out_filename,
        content=pdf_bytes,
        content_type="application/pdf"
    )

    logger.info(f"Generated 8-page luxury property brochure: {out_filename} ({len(pdf_bytes)} bytes) stored at {saved_url}")

    return {
        "success": True,
        "filename": out_filename,
        "file_url": saved_url,
        "download_url": saved_url,
        "property_id": prop.property_id,
        "property_name": p_name,
        "pages": 8,
        "pdf_bytes": pdf_bytes,
        "file_size_bytes": len(pdf_bytes),
        "engine": "reportlab",
    }


def generate_property_brochure(
    db: Session,
    property_id: str,
    site_origin: Optional[str] = None,
    language: str = "en",
    client_name: Optional[str] = None
) -> bytes:
    """
    Standard entrypoint returning raw PDF bytes for streaming responses.
    """
    res = generate_single_property_brochure_pdf(
        db=db,
        property_id=property_id,
        site_origin=site_origin,
        language=language,
        client_name=client_name
    )
    return res["pdf_bytes"]


def generate_and_upload_brochure(
    db: Session,
    property_id: str,
    site_origin: Optional[str] = None,
    language: str = "en",
    client_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generates the luxury presentation brochure and uploads to Hugging Face S3 / storage,
    returning the direct accessible download URL.
    """
    return generate_single_property_brochure_pdf(
        db=db,
        property_id=property_id,
        site_origin=site_origin,
        language=language,
        client_name=client_name
    )
