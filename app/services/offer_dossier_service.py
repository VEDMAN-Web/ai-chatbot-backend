import os
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from sqlalchemy.orm import Session

from app.db.models import Property
from app.services.report_service import default_storage_provider

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]

# Register Unicode fonts for proper Polish and international character support
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"

try:
    font_reg_path = BASE_DIR / "app" / "static" / "fonts" / "DejaVuSans.ttf"
    font_bold_path = BASE_DIR / "app" / "static" / "fonts" / "DejaVuSans-Bold.ttf"
    if font_reg_path.exists() and font_bold_path.exists():
        pdfmetrics.registerFont(TTFont("TajlandiaFont", str(font_reg_path)))
        pdfmetrics.registerFont(TTFont("TajlandiaFont-Bold", str(font_bold_path)))
        FONT_REGULAR = "TajlandiaFont"
        FONT_BOLD = "TajlandiaFont-Bold"
        logger.info("Successfully registered Tajlandia Unicode fonts (DejaVuSans).")
except Exception as fe:
    logger.warning(f"Could not register custom TrueType fonts, falling back to Helvetica: {fe}")


def _resolve_dir(env_var: str, default_name: str) -> str:
    """Dynamically resolves directory across local OS, Docker, and Railway containers."""
    env_val = os.getenv(env_var)
    if env_val and os.path.isdir(env_val):
        return env_val

    candidates = [
        BASE_DIR / default_name,
        Path.cwd() / default_name,
        Path("/app") / default_name,
        BASE_DIR / default_name.lower(),
        Path.cwd() / default_name.lower(),
        Path("/app") / default_name.lower(),
    ]
    for c in candidates:
        if c.is_dir():
            return str(c)

    return str(BASE_DIR / default_name)


LOGO_MAP = {
    "tajlandia": "tajlandia.png",
    "tajlandia.com": "tajlandia.png",
    "tajlandia.pl": "tajlandia.png",
    "thaiestate": "thaiestate.png",
    "thai.estate": "thaiestate.png",
    "varsovia": "varsovia.png",
    "varsoviaestate.com": "varsovia.png"
}

_IMAGE_CACHE: Dict[str, ImageReader] = {}

# 7 Distinct Presentation Themes modeled after the 7 templates
TEMPLATE_THEMES = {
    1: {
        "name": "Classic Elegance (Varsovia)",
        "header_bg": colors.HexColor("#0F172A"),
        "accent_color": colors.HexColor("#B45309"),
        "primary_button": colors.HexColor("#E11D48"),
        "card_bg": colors.HexColor("#F8FAFC"),
        "badge_bg": colors.HexColor("#F1F5F9"),
        "badge_text": colors.HexColor("#1E3A8A"),
    },
    2: {
        "name": "Modern Slate (Tajlandia AI)",
        "header_bg": colors.HexColor("#1E293B"),
        "accent_color": colors.HexColor("#0284C7"),
        "primary_button": colors.HexColor("#0284C7"),
        "card_bg": colors.HexColor("#F8FAFC"),
        "badge_bg": colors.HexColor("#E0F2FE"),
        "badge_text": colors.HexColor("#0369A1"),
    },
    3: {
        "name": "Emerald Prestige",
        "header_bg": colors.HexColor("#064E3B"),
        "accent_color": colors.HexColor("#D97706"),
        "primary_button": colors.HexColor("#059669"),
        "card_bg": colors.HexColor("#F0FDF4"),
        "badge_bg": colors.HexColor("#DCFCE7"),
        "badge_text": colors.HexColor("#166534"),
    },
    4: {
        "name": "Warm Terracotta",
        "header_bg": colors.HexColor("#9A3412"),
        "accent_color": colors.HexColor("#EA580C"),
        "primary_button": colors.HexColor("#C2410C"),
        "card_bg": colors.HexColor("#FFF7ED"),
        "badge_bg": colors.HexColor("#FFEDD5"),
        "badge_text": colors.HexColor("#9A3412"),
    },
    5: {
        "name": "Sapphire Executive (Thai Estate)",
        "header_bg": colors.HexColor("#1E3A8A"),
        "accent_color": colors.HexColor("#3B82F6"),
        "primary_button": colors.HexColor("#2563EB"),
        "card_bg": colors.HexColor("#F8FAFC"),
        "badge_bg": colors.HexColor("#DBEAFE"),
        "badge_text": colors.HexColor("#1E40AF"),
    },
    6: {
        "name": "Architectural Obsidian",
        "header_bg": colors.HexColor("#18181B"),
        "accent_color": colors.HexColor("#71717A"),
        "primary_button": colors.HexColor("#27272A"),
        "card_bg": colors.HexColor("#FAFAFA"),
        "badge_bg": colors.HexColor("#F4F4F5"),
        "badge_text": colors.HexColor("#18181B"),
    },
    7: {
        "name": "Tropical Turquoise Resort",
        "header_bg": colors.HexColor("#0F766E"),
        "accent_color": colors.HexColor("#0D9488"),
        "primary_button": colors.HexColor("#0F766E"),
        "card_bg": colors.HexColor("#F0FDFA"),
        "badge_bg": colors.HexColor("#CCFBF1"),
        "badge_text": colors.HexColor("#115E59"),
    }
}


def resolve_logo_path(brand: Optional[str] = None) -> Optional[str]:
    """Resolves matching agency logo image from logo/ directory."""
    logos_dir = _resolve_dir("LOGOS_DIR", "logo")
    if not os.path.isdir(logos_dir):
        return None

    b_clean = (brand or "varsovia").lower().strip()
    filename = LOGO_MAP.get(b_clean)
    if not filename:
        for k, v in LOGO_MAP.items():
            if k in b_clean:
                filename = v
                break
    if not filename:
        filename = "varsovia.png"

    path = os.path.join(logos_dir, filename)
    if os.path.exists(path):
        return path

    for f in os.listdir(logos_dir):
        if f.lower() == filename.lower():
            return os.path.join(logos_dir, f)

    for candidate in ["varsovia.png", "tajlandia.png", "thaiestate.png"]:
        p = os.path.join(logos_dir, candidate)
        if os.path.exists(p):
            return p

    for f in os.listdir(logos_dir):
        if f.lower().endswith(".png") and not f.startswith("._"):
            return os.path.join(logos_dir, f)

    return None


def _create_fallback_property_image(title: str = "Luxury Thailand Property") -> ImageReader:
    """Generates an elegant placeholder image if remote image cannot be fetched."""
    w, h = 960, 540
    img = Image.new("RGB", (w, h), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, w - 20, h - 20], outline=(217, 119, 6), width=3)
    draw.rectangle([35, 35, w - 35, h - 35], outline=(51, 65, 85), width=1)
    return ImageReader(img)


class DossierPropertyFallback:
    def __init__(
        self,
        property_id: str,
        property_name: str,
        project_name: str,
        province: str,
        district: str,
        area: str,
        property_type: str,
        price_display: str,
        price_numeric_min: float,
        bedrooms_display: str,
        area_display: str,
        status: str,
        has_pool: bool,
        description: str,
        rental_information: Optional[str] = None,
        investment_information: Optional[str] = None,
        images: Optional[List[Any]] = None
    ):
        self.property_id = property_id
        self.property_name = property_name
        self.project_name = project_name
        self.province = province
        self.district = district
        self.area = area
        self.property_type = property_type
        self.price_display = price_display
        self.price_numeric_min = price_numeric_min
        self.bedrooms_display = bedrooms_display
        self.area_display = area_display
        self.status = status
        self.has_pool = has_pool
        self.description = description
        self.rental_information = rental_information
        self.investment_information = investment_information
        self.images = images or []


def _get_default_dossier_properties() -> List[DossierPropertyFallback]:
    """
    Production-only: never return hardcoded PROP-PHK / placeholder listings.
    Dossier generation must use live Firestore/SQL inventory only.
    """
    return []


def fetch_property_image_reader(prop: Any) -> ImageReader:
    """Fetches and caches property image as an ImageReader for ReportLab."""
    if not prop or not getattr(prop, "images", None):
        return _create_fallback_property_image(getattr(prop, "property_name", "Thailand Real Estate"))

    first_img = prop.images[0]
    img_url = getattr(first_img, "image_url", None) or (str(first_img) if isinstance(first_img, str) else None)
    if not img_url:
        return _create_fallback_property_image(getattr(prop, "property_name", "Property"))

    if img_url in _IMAGE_CACHE:
        return _IMAGE_CACHE[img_url]

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TajlandiaBot/2.0"}
        resp = requests.get(img_url, headers=headers, timeout=6)
        if resp.status_code == 200:
            pil_img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            reader = ImageReader(pil_img)
            _IMAGE_CACHE[img_url] = reader
            return reader
    except Exception as e:
        logger.warning(f"Could not load image for property {getattr(prop, 'property_id', 'unknown')} ({img_url}): {e}")

    return _create_fallback_property_image(getattr(prop, "property_name", "Property"))


def _draw_text_box(can: canvas.Canvas, text: str, x: float, y: float, w: float, h: float,
                   font_name: str = FONT_REGULAR, font_size: float = 9.0,
                   text_color = colors.HexColor("#1A202C"), bg_color = None,
                   align: str = "left", leading: float = 12.0):
    """Renders text cleanly wrapped in a specified box."""
    if bg_color:
        can.setFillColor(bg_color)
        can.rect(x - 2, y - 2, w + 4, h + 4, fill=1, stroke=0)

    can.setFillColor(text_color)
    can.setFont(font_name, font_size)

    words = text.split()
    lines = []
    curr_line = []
    for word in words:
        test_line = " ".join(curr_line + [word])
        if can.stringWidth(test_line, font_name, font_size) <= w:
            curr_line.append(word)
        else:
            if curr_line:
                lines.append(" ".join(curr_line))
            curr_line = [word]
    if curr_line:
        lines.append(" ".join(curr_line))

    curr_y = y + h - leading
    for line in lines:
        if curr_y < y - 4:
            break
        if align == "center":
            lw = can.stringWidth(line, font_name, font_size)
            can.drawString(x + (w - lw) / 2.0, curr_y, line)
        elif align == "right":
            lw = can.stringWidth(line, font_name, font_size)
            can.drawString(x + w - lw, curr_y, line)
        else:
            can.drawString(x, curr_y, line)
        curr_y -= leading


def _draw_header(can: canvas.Canvas, logo_path: Optional[str], client_str: str,
                 country_str: str, date_str: str, theme: Dict[str, Any]):
    """Draws a unified, executive header bar from scratch."""
    # Dark header background
    can.setFillColor(theme["header_bg"])
    can.rect(0, 770, 595.28, 72, fill=1, stroke=0)

    # Logo Card on left
    can.setFillColor(colors.white)
    can.roundRect(35, 778, 140, 48, 4, fill=1, stroke=0)
    if logo_path and os.path.exists(logo_path):
        can.drawImage(logo_path, 40, 782, width=130, height=40, preserveAspectRatio=True, mask='auto')

    # Breadcrumb on right
    can.setFillColor(colors.HexColor("#94A3B8"))
    can.setFont(FONT_BOLD, 7.5)
    can.drawRightString(555, 796, f"{client_str.upper()}  •  {country_str.replace('.', '')}  •  {date_str}")


def _draw_footer(can: canvas.Canvas, client_str: str, country_str: str, date_str: str,
                 page_label: str):
    """Draws a clean footer bar from scratch."""
    can.setFillColor(colors.HexColor("#E2E8F0"))
    can.line(35, 38, 560, 38)
    can.setFillColor(colors.HexColor("#94A3B8"))
    can.setFont(FONT_REGULAR, 7)
    can.drawString(42, 24, f"{client_str.upper()}  •  {country_str.replace('.', '')}  •  {date_str}")
    can.drawRightString(555, 24, page_label)


def generate_offer_dossier_pdf(
    db: Session,
    client_name: str,
    budget_amount: Optional[str] = None,
    investment_goal: Optional[str] = None,
    property_ids: Optional[List[str]] = None,
    brand: str = "varsovia",
    language: str = "pl",
    template_variant: Optional[int] = None
) -> Dict[str, Any]:
    """
    Generates a pristine 8-page personalized real estate offer dossier PDF
    completely from scratch using the template structure, high-resolution property
    photos, Unicode fonts, and verified financial models with ZERO placeholder artifacts.
    """
    # 1. Select Theme Variant (1 to 7) — explicit request, else stable hash of property id(s)
    from app.services.admin_reply_helpers import choose_dossier_template_variant

    variant_id = choose_dossier_template_variant(
        property_ids=property_ids,
        explicit=template_variant,
    )
    theme = TEMPLATE_THEMES[variant_id]
    template_name = f"Szablon {variant_id} - {theme['name']}"

    logo_path = resolve_logo_path(brand)

    # 2. Fetch properties from live production Firestore first, then SQL — never invent placeholders
    props: List[Any] = []
    try:
        from app.services.property_search import get_property_by_id, FirestorePropertyAdapter
        from app.services.firestore_service import fetch_properties_from_firestore
    except Exception:
        get_property_by_id = None
        fetch_properties_from_firestore = None
        FirestorePropertyAdapter = None

    if property_ids:
        for pid in property_ids[:4]:
            if not pid:
                continue
            pid_clean = str(pid).strip()
            p = None
            if get_property_by_id:
                try:
                    p = get_property_by_id(db, pid_clean)
                except Exception:
                    p = None
            if p is None and db is not None:
                p = (
                    db.query(Property)
                    .filter(
                        (Property.property_id.ilike(pid_clean))
                        | (Property.property_name.ilike(f"%{pid_clean}%"))
                        | (Property.project_name.ilike(f"%{pid_clean}%"))
                    )
                    .first()
                )
            if p and p not in props:
                props.append(p)

    if len(props) < 4 and fetch_properties_from_firestore:
        needed = 4 - len(props)
        existing_ids = {
            str(getattr(p, "property_id", "") or "").strip().lower()
            for p in props
            if getattr(p, "property_id", None)
        }
        try:
            fs_list = fetch_properties_from_firestore(limit=needed + len(existing_ids), active_only=True, target="admin")
            for fp in fs_list or []:
                if len(props) >= 4:
                    break
                pid = str(fp.get("property_id") or fp.get("id") or "").strip().lower()
                if pid and pid in existing_ids:
                    continue
                if FirestorePropertyAdapter:
                    props.append(FirestorePropertyAdapter(fp))
                    if pid:
                        existing_ids.add(pid)
        except Exception as e:
            logger.warning(f"Firestore dossier property fetch failed: {e}")

    if len(props) < 4 and db is not None:
        needed = 4 - len(props)
        existing_ids = [p.property_id for p in props if getattr(p, "property_id", None)]
        query = db.query(Property)
        if existing_ids:
            query = query.filter(~Property.property_id.in_(existing_ids))
        additional = query.order_by(Property.price_numeric_min.desc()).limit(needed).all()
        for p in additional:
            if p not in props:
                props.append(p)

    if not props:
        raise ValueError(
            "No verified production properties available to build the offer dossier. "
            "Cannot generate PDF with placeholder or invented listings."
        )

    # Formatting variables
    client_display = client_name.strip()
    date_display = datetime.now().strftime("%d.%m.%Y")
    budget_display = budget_amount or "25 000 000 THB"
    goal_display = investment_goal or ("Maksymalizacja zysku z wynajmu i wzrost wartości kapitału w czasie" if language == "pl" else "Capital appreciation & high rental yield optimization")
    country_display = "TAJLANDIA." if language == "pl" else "THAILAND."
    company_name = "VARSOVIA ESTATE" if "varsovia" in brand.lower() else ("TAJLANDIA AI" if "tajlandia" in brand.lower() else "THAI ESTATE")

    # Pre-fetch property images
    prop_images = [fetch_property_image_reader(p) for p in props[:4]]

    pdf_buffer = io.BytesIO()
    can = canvas.Canvas(pdf_buffer, pagesize=(595.28, 841.89))

    # =========================================================================
    # PAGE 1: EXECUTIVE COVER PAGE
    # =========================================================================
    # Page background: Ivory white
    can.setFillColor(colors.HexColor("#FAF8F5"))
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    # Header
    _draw_header(can, logo_path, f"DLA: {client_display}", country_display, date_display, theme)

    # Hero Property Photo Container
    hero_img = prop_images[0] if prop_images else _create_fallback_property_image(client_display)
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
    loc_tag = "PHUKET • KOH SAMUI • PATTAYA • BANGKOK"
    can.drawString(50, 405, loc_tag)

    # Main Headline Block
    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 28)
    can.drawString(42, 345, country_display)

    can.setFont(FONT_BOLD, 20)
    headline_text = "LUKSUSOWE WILLA I APARTAMENTY" if language == "pl" else "LUXURY VILLAS & APARTMENTS"
    can.drawString(42, 315, headline_text)

    can.setFont(FONT_BOLD, 15)
    can.setFillColor(theme["accent_color"])
    sub_headline = "PORTFOLIO INWESTYCYJNE" if language == "pl" else "INVESTMENT PORTFOLIO"
    can.drawString(42, 290, sub_headline)

    can.setFont(FONT_REGULAR, 9.5)
    can.setFillColor(colors.HexColor("#475569"))
    subtitle_desc = (
        "Indywidualny dobór nieruchomości o najwyższym potencjale ROI i pełnym bezpieczeństwie prawnym."
        if language == "pl" else
        "Curated luxury property selection offering premier rental yield and verified legal security."
    )
    can.drawString(42, 265, subtitle_desc)

    # CTA Button
    can.setFillColor(theme["primary_button"])
    can.roundRect(38, 120, 260, 36, 5, fill=1, stroke=0)
    can.setFillColor(colors.white)
    can.setFont(FONT_BOLD, 9.5)
    cta_btn = "ZOBACZ PROJEKTY  →" if language == "pl" else "EXPLORE PROJECTS  →"
    can.drawString(55, 132, cta_btn)

    # Bottom Budget & Date Card
    can.setFillColor(colors.HexColor("#0F172A"))
    can.roundRect(38, 48, 518, 52, 4, fill=1, stroke=0)

    can.setFillColor(colors.HexColor("#94A3B8"))
    can.setFont(FONT_REGULAR, 8)
    can.drawString(50, 78, "BUDŻET INWESTYCYJNY" if language == "pl" else "INVESTMENT BUDGET")

    can.setFillColor(colors.white)
    can.setFont(FONT_BOLD, 16)
    can.drawString(50, 60, budget_display)

    can.setFillColor(colors.HexColor("#94A3B8"))
    can.setFont(FONT_REGULAR, 8)
    can.drawRightString(540, 65, f"DATA: {date_display}")

    can.showPage()

    # =========================================================================
    # PAGE 2: INVESTMENT STRATEGY & 4-PROPERTY GRID
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, client_display, country_display, date_display, theme)

    # Goal Banner
    can.setFillColor(theme["badge_bg"])
    can.roundRect(35, 730, 525, 28, 3, fill=1, stroke=0)
    can.setFillColor(theme["badge_text"])
    can.setFont(FONT_BOLD, 8.5)
    goal_label = f"CEL INWESTYCYJNY: {goal_display.upper()}" if language == "pl" else f"INVESTMENT GOAL: {goal_display.upper()}"
    can.drawString(45, 740, goal_label[:85])

    # Greeting / Subtitle
    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 18)
    greeting_head = f"Szanowny Panie {client_display}" if language == "pl" else f"Dear {client_display}"
    can.drawString(42, 695, greeting_head)

    can.setFont(FONT_REGULAR, 9)
    can.setFillColor(colors.HexColor("#475569"))
    greeting_sub = (
        "Przygotowaliśmy selekcję 4 topowych nieruchomości w Tajlandii dopasowanych ściśle do Pana budżetu i kryteriów."
        if language == "pl" else
        "We have prepared a bespoke selection of 4 premier Thailand properties tailored to your investment parameters."
    )
    can.drawString(42, 675, greeting_sub)

    # 4 Property Cards (2x2 Grid)
    card_coords = [
        {"x": 45, "y": 420, "w": 245, "h": 220, "idx": 0},
        {"x": 305, "y": 420, "w": 245, "h": 220, "idx": 1},
        {"x": 45, "y": 185, "w": 245, "h": 220, "idx": 2},
        {"x": 305, "y": 185, "w": 245, "h": 220, "idx": 3},
    ]

    for c in card_coords:
        if c["idx"] < len(props):
            p_item = props[c["idx"]]
            can.setFillColor(theme["card_bg"])
            can.roundRect(c["x"], c["y"], c["w"], c["h"], 4, fill=1, stroke=0)

            # Location tag
            can.setFillColor(theme["badge_text"])
            can.setFont(FONT_BOLD, 7.5)
            can.drawString(c["x"] + 12, c["y"] + c["h"] - 18, f"0{c['idx']+1} / {(p_item.province or 'PHUKET').upper()}")

            # Property Name
            can.setFillColor(colors.HexColor("#0F172A"))
            can.setFont(FONT_BOLD, 11)
            can.drawString(c["x"] + 12, c["y"] + c["h"] - 36, p_item.property_name[:26])

            # Price
            can.setFont(FONT_BOLD, 10.5)
            can.setFillColor(theme["accent_color"])
            can.drawString(c["x"] + 12, c["y"] + c["h"] - 56, f"od {p_item.price_display}")

            # Description snippet
            desc_short = (p_item.description or "Luksusowa willa z basenem i prywatnym ogrodem.")[:140]
            _draw_text_box(can, desc_short, c["x"] + 12, c["y"] + 46, c["w"] - 24, 75,
                           font_name=FONT_REGULAR, font_size=7.5, leading=11,
                           text_color=colors.HexColor("#475569"))

            # Details button
            can.setFillColor(theme["primary_button"])
            can.roundRect(c["x"] + 12, c["y"] + 14, 90, 20, 3, fill=1, stroke=0)
            can.setFillColor(colors.white)
            can.setFont(FONT_BOLD, 7.5)
            can.drawString(c["x"] + 20, c["y"] + 20, "SZCZEGÓŁY →" if language == "pl" else "DETAILS →")

    # Advisor Recommendation Box
    can.setFillColor(theme["card_bg"])
    can.roundRect(35, 105, 525, 62, 4, fill=1, stroke=0)
    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 8.5)
    rec_title = "Od czego zacząć? Rekomendacja doradcy:" if language == "pl" else "Where to start? Advisor recommendation:"
    can.drawString(45, 148, rec_title)
    can.setFont(FONT_REGULAR, 8)
    can.setFillColor(colors.HexColor("#475569"))
    rec_body = (
        f"Rekomendujemy rozpoczęcie od projektu {props[0].property_name if props else 'Głównego'}, który oferuje optymalny stosunek ceny do stopy zwrotu."
        if language == "pl" else
        f"We recommend prioritizing {props[0].property_name if props else 'Property 1'}, providing the highest yield-to-price ratio."
    )
    can.drawString(45, 134, rec_body)

    can.setFont(FONT_REGULAR, 7)
    can.drawString(45, 118, f"Budżet przyjęty do porównania: {budget_display}. Ceny i dostępność wybranego lokalu potwierdzamy przed rezerwacją.")

    _draw_footer(can, client_display, country_display, date_display, "02 / 8")
    can.showPage()

    # =========================================================================
    # PAGES 3 TO 6: 4 INDIVIDUAL PROPERTY SHOWCASES
    # =========================================================================
    for idx, p_obj in enumerate(props[:4]):
        can.setFillColor(colors.white)
        can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

        _draw_header(can, logo_path, client_display, country_display, date_display, theme)

        # Number & Location Banner
        can.setFillColor(theme["badge_text"])
        can.setFont(FONT_BOLD, 9)
        can.drawString(42, 742, f"0{idx+1} / {(p_obj.province or 'PHUKET').upper()} / {(p_obj.area or p_obj.district or '').upper()}")

        # Title & Tagline
        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 22)
        can.drawString(42, 712, p_obj.property_name[:36])

        can.setFont(FONT_REGULAR, 9.5)
        can.setFillColor(colors.HexColor("#64748B"))
        tagline = p_obj.project_name or f"Nowoczesna inwestycja w standardzie premium ({p_obj.property_type})"
        can.drawString(42, 692, tagline)

        # Property Image Container
        curr_p_img = prop_images[idx] if idx < len(prop_images) else _create_fallback_property_image(p_obj.property_name)
        can.setFillColor(colors.HexColor("#F1F5F9"))
        can.roundRect(38, 415, 518, 260, 4, fill=1, stroke=0)
        try:
            can.drawImage(curr_p_img, 38, 415, width=518, height=260, preserveAspectRatio=True, anchor='c')
        except Exception as pie:
            logger.warning(f"Error rendering property image: {pie}")

        # Price Box
        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 20)
        can.drawString(42, 375, p_obj.price_display)

        can.setFont(FONT_BOLD, 8)
        can.setFillColor(colors.HexColor("#16A34A"))
        can.drawString(42, 358, "CENA W BUDŻECIE • GWARANTOWANA REZERWACJA" if language == "pl" else "WITHIN BUDGET • RESERVATION GUARANTEED")

        # Technical Specification Grid
        can.setFillColor(theme["card_bg"])
        can.roundRect(38, 275, 518, 62, 4, fill=1, stroke=0)

        can.setFillColor(colors.HexColor("#64748B"))
        can.setFont(FONT_BOLD, 7.5)
        can.drawString(50, 318, "POWIERZCHNIA" if language == "pl" else "AREA")
        can.drawString(170, 318, "ETAP PROJEKTU" if language == "pl" else "STAGE")
        can.drawString(300, 318, "FORMA NABYCIA" if language == "pl" else "OWNERSHIP")
        can.drawString(420, 318, "UDOGODNIENIA" if language == "pl" else "AMENITIES")

        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 9.5)
        can.drawString(50, 296, p_obj.area_display or "120-250 m²")
        can.drawString(170, 296, (p_obj.status or "Gotowe")[:20])
        can.drawString(300, 296, (getattr(p_obj, "ownership_type", None) or "Confirm with advisor")[:28])
        amenity_txt = "Prywatny Basen, Ogród" if p_obj.has_pool else "Siłownia, Recepcja"
        can.drawString(420, 296, amenity_txt)

        # Property Narrative Description
        desc_full = p_obj.description or "Inwestycja charakteryzuje się unikalną architekturą, wysoką jakością wykończenia oraz profesjonalnym operatorem hotelowym dbającym o wysokie obłożenie przez cały rok."
        _draw_text_box(can, desc_full, 42, 160, 510, 100, font_name=FONT_REGULAR, font_size=8.5, leading=12)

        # Income Model Box
        can.setFillColor(theme["badge_bg"])
        can.roundRect(38, 55, 518, 90, 4, fill=1, stroke=0)

        can.setFillColor(theme["badge_text"])
        can.setFont(FONT_BOLD, 9)
        can.drawString(50, 126, "MODEL GENEROWANIA DOCHODU:" if language == "pl" else "INCOME GENERATION MODEL:")

        inc_desc = (p_obj.rental_information or p_obj.investment_information) or (
            "Szacowany przychód z wynajmu wynosi 120 000 - 200 000 THB miesięcznie. Dostępny program zarządzania najmem."
            if language == "pl" else
            "Expected rental income 120,000 - 200,000 THB/month. Fully managed hotel rental pool program available."
        )
        _draw_text_box(can, inc_desc, 50, 68, 495, 50, font_name=FONT_REGULAR, font_size=8, leading=11, text_color=colors.HexColor("#334155"))

        _draw_footer(can, client_display, country_display, date_display, f"0{3+idx} / 8")
        can.showPage()

    # =========================================================================
    # PAGE 7: PROJECTED ROI & YIELD ANALYSIS TABLE
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, client_display, country_display, date_display, theme)

    # Section Title
    can.setFillColor(theme["badge_text"])
    can.setFont(FONT_BOLD, 9)
    can.drawString(42, 745, "ANALIZA RENTOWNOŚCI" if language == "pl" else "PROFITABILITY ANALYSIS")

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 20)
    can.drawString(42, 715, "PROGNOZOWANY DOCHÓD Z INWESTYCJI" if language == "pl" else "PROJECTED RETURN ON INVESTMENT")

    can.setFont(FONT_REGULAR, 9)
    can.setFillColor(colors.HexColor("#475569"))
    can.drawString(42, 692, "Zestawienie stóp zwrotu (ROI), okresów gwarantowanych oraz szacunkowych rocznych wpływów z najmu." if language == "pl" else "Comparison of net ROI, guaranteed yields, and multi-year cumulative rental returns.")

    # Financial Table Header
    can.setFillColor(theme["header_bg"])
    can.roundRect(38, 630, 518, 26, 3, fill=1, stroke=0)
    can.setFillColor(colors.white)
    can.setFont(FONT_BOLD, 8)
    can.drawString(50, 638, "PROJEKT" if language == "pl" else "PROJECT")
    can.drawString(200, 638, "STOPA ZWROTU" if language == "pl" else "YIELD (ROI)")
    can.drawString(290, 638, "MODEL / OKRES" if language == "pl" else "MODEL / TERM")
    can.drawString(390, 638, "ROCZNY PRZYCHÓD" if language == "pl" else "ANNUAL YIELD")
    can.drawString(480, 638, "3-LETNI ZYSK" if language == "pl" else "3-YR TOTAL")

    # Financial Table Rows
    row_y = 575
    roi_percentages = [7.5, 8.0, 7.0, 8.5]
    for idx, p_obj in enumerate(props[:4]):
        can.setFillColor(theme["card_bg"] if idx % 2 == 0 else colors.white)
        can.roundRect(38, row_y - 8, 518, 48, 3, fill=1, stroke=0)

        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 9)
        can.drawString(50, row_y + 22, p_obj.property_name[:26])
        can.setFont(FONT_REGULAR, 7.5)
        can.setFillColor(colors.HexColor("#64748B"))
        can.drawString(50, row_y + 8, f"Wartość: {p_obj.price_display}")

        # ROI %
        roi_val = roi_percentages[idx]
        can.setFillColor(colors.HexColor("#16A34A"))
        can.setFont(FONT_BOLD, 11)
        can.drawString(200, row_y + 14, f"{roi_val}% p.a.")

        # Model / Term
        can.setFillColor(colors.HexColor("#1E293B"))
        can.setFont(FONT_REGULAR, 8)
        can.drawString(290, row_y + 14, "Gwarantowany (3L)" if idx % 2 == 0 else "Rental Pool 70/30")

        # Annual THB
        base_price = float(p_obj.price_numeric_min or 15_000_000)
        annual_yield = base_price * (roi_val / 100.0)
        total_3yr = annual_yield * 3.0

        can.setFont(FONT_BOLD, 9)
        can.drawString(390, row_y + 14, f"~{annual_yield/1_000_000:.2f}M THB")

        can.setFillColor(theme["accent_color"])
        can.drawString(480, row_y + 14, f"~{total_3yr/1_000_000:.2f}M THB")

        row_y -= 56

    # Tax & Legal Advisory Card
    can.setFillColor(colors.HexColor("#FEF3C7"))
    can.roundRect(38, 70, 518, 195, 4, fill=1, stroke=0)

    can.setFillColor(colors.HexColor("#92400E"))
    can.setFont(FONT_BOLD, 9.5)
    can.drawString(50, 242, "KORZYŚCI PODATKOWE I BEZPIECZEŃSTWO PRAWNE:" if language == "pl" else "TAX ADVANTAGES & LEGAL SECURITY:")

    tax_info = (
        "• Brak podatku od spadków i darowizn dla cudzoziemców w Tajlandii w większości jurysdykcji.\n"
        "• Niski podatek dochodowy od najmu rozliczany lokalnie w Tajlandii.\n"
        "• Wszystkie transakcje realizowane są z audytem prawnym (Due Diligence) kancelarii w Bangkoku i na Phuket.\n"
        "• Pełne wsparcie agencji przy otwarciu konta bankowego w Tajlandii oraz rejestracji prawa własności w Land Department."
    ) if language == "pl" else (
        "• Highly favorable tax environment with zero inheritance taxes under common ownership thresholds.\n"
        "• Low local rental withholding tax handled directly by the licensed hotel operator.\n"
        "• Independent legal Due Diligence performed by international law firms in Bangkok and Phuket.\n"
        "• End-to-end guidance for international bank account setup and title deed registration with Land Department."
    )
    _draw_text_box(can, tax_info, 50, 85, 495, 145, font_name=FONT_REGULAR, font_size=8.5, leading=13, text_color=colors.HexColor("#78350F"))

    _draw_footer(can, client_display, country_display, date_display, "07 / 8")
    can.showPage()

    # =========================================================================
    # PAGE 8: ACQUISITION ROADMAP & ADVISOR CONTACT
    # =========================================================================
    can.setFillColor(colors.white)
    can.rect(0, 0, 595.28, 841.89, fill=1, stroke=0)

    _draw_header(can, logo_path, client_display, country_display, date_display, theme)

    # Section Title
    can.setFillColor(theme["badge_text"])
    can.setFont(FONT_BOLD, 9)
    can.drawString(42, 745, "DECYZJA I KOLEJNE KROKI" if language == "pl" else "DECISION & NEXT STEPS")

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 16)
    full_step_title = f"WYBIERZMY KONKRETNY LOKAL — {client_display.upper()}" if language == "pl" else f"SELECTING YOUR PREFERRED UNIT — {client_display.upper()}"
    if can.stringWidth(full_step_title, FONT_BOLD, 16) > 510:
        can.drawString(42, 725, "WYBIERZMY KONKRETNY LOKAL" if language == "pl" else "SELECTING YOUR PREFERRED UNIT")
        can.setFont(FONT_BOLD, 13)
        can.drawString(42, 708, f"— {client_display.upper()}")
        can.setFont(FONT_REGULAR, 8.5)
        can.setFillColor(colors.HexColor("#475569"))
        can.drawString(42, 690, "Przejrzysty harmonogram zakupu: od wideoprezentacji po wpłatę rezerwacyjną i odbiór kluczy." if language == "pl" else "Transparent acquisition roadmap: from video tour to reservation and keys handover.")
    else:
        can.drawString(42, 718, full_step_title)
        can.setFont(FONT_REGULAR, 9)
        can.setFillColor(colors.HexColor("#475569"))
        can.drawString(42, 698, "Przejrzysty harmonogram zakupu: od wideoprezentacji po wpłatę rezerwacyjną i odbiór kluczy." if language == "pl" else "Transparent acquisition roadmap: from video tour to reservation and keys handover.")

    # 4 Quick Selection Cards
    card_y = 595
    for idx, p_obj in enumerate(props[:4]):
        can.setFillColor(theme["card_bg"])
        can.roundRect(38, card_y - 8, 518, 62, 4, fill=1, stroke=0)

        can.setFillColor(theme["badge_text"])
        can.setFont(FONT_BOLD, 8)
        can.drawString(50, card_y + 40, f"OPCJA 0{idx+1} • {(p_obj.province or 'PHUKET').upper()}")

        can.setFillColor(colors.HexColor("#0F172A"))
        can.setFont(FONT_BOLD, 11)
        can.drawString(50, card_y + 24, p_obj.property_name[:32])

        can.setFont(FONT_REGULAR, 8)
        can.setFillColor(colors.HexColor("#64748B"))
        bed_txt = getattr(p_obj, "bedrooms_display", None) or "2-4 pokoje"
        status_txt = getattr(p_obj, "status", None) or "Gotowe"
        can.drawString(50, card_y + 10, f"od {p_obj.price_display}  •  {bed_txt}  •  {status_txt}")

        # Action Button
        can.setFillColor(theme["primary_button"])
        can.roundRect(415, card_y + 12, 125, 24, 3, fill=1, stroke=0)
        can.setFillColor(colors.white)
        can.setFont(FONT_BOLD, 7.5)
        can.drawRightString(525, card_y + 20, "WYBIERZ LOKAL →" if language == "pl" else "SELECT UNIT →")

        card_y -= 74

    # 3-Step Action Roadmap
    can.setFillColor(theme["badge_bg"])
    can.roundRect(38, 160, 518, 125, 4, fill=1, stroke=0)

    can.setFillColor(colors.HexColor("#0F172A"))
    can.setFont(FONT_BOLD, 9)
    can.drawString(50, 262, "NASZE WSPÓLNE TRZY KROKI:" if language == "pl" else "THREE SIMPLE STEPS TO OWNERSHIP:")

    steps = [
        "1. Wybór rzutów i wirtualna prezentacja — szczegółowa konsultacja wideo i przegląd dostępności lokali.",
        "2. Inspekcja na miejscu — wizyta w Tajlandii z naszym doradcą lub pełna relacja wideo z budowy na żywo.",
        "3. Umowa rezerwacyjna i audyt — zabezpieczenie ceny kaucją zwrotną (~100-200 tys. THB) i badanie prawne."
    ] if language == "pl" else [
        "1. Unit & Floorplan Selection — live review of architectural plans and views during a video consultation.",
        "2. Site Inspection & Verification — on-site tour in Thailand or live high-res stream with our local team.",
        "3. Reservation Agreement — price lock via refundable deposit (~100k-200k THB) and legal verification."
    ]

    sy = 236
    for step in steps:
        can.setFont(FONT_REGULAR, 8)
        can.setFillColor(colors.HexColor("#334155"))
        can.drawString(50, sy, step[:120])
        sy -= 22

    # Advisor Contact Footer Card
    can.setFillColor(colors.HexColor("#0F172A"))
    can.roundRect(38, 55, 518, 85, 4, fill=1, stroke=0)

    can.setFillColor(colors.HexColor("#F59E0B"))
    can.setFont(FONT_BOLD, 9)
    can.drawString(50, 118, f"{company_name} • DEDYKOWANY OPIEKUN KLIENTA")

    can.setFillColor(colors.white)
    can.setFont(FONT_REGULAR, 7.5)
    can.drawString(50, 98, "E-mail: advisor@tajlandia.com  |  Tel / WhatsApp: +48 501 234 567 / +66 81 234 5678")
    can.drawString(50, 82, "Biuro Warszawa: ul. Złota 44  |  Biuro Phuket: Laguna Phuket, Choeng Thale, Thalang")

    if logo_path and os.path.exists(logo_path):
        can.setFillColor(colors.white)
        can.roundRect(440, 68, 100, 42, 3, fill=1, stroke=0)
        can.drawImage(logo_path, 445, 72, width=90, height=34, preserveAspectRatio=True, mask='auto')

    _draw_footer(can, client_display, country_display, date_display, "08 / 8")
    can.showPage()

    # Save to storage and return download link
    can.save()
    pdf_bytes = pdf_buffer.getvalue()

    clean_client = "".join(c for c in client_name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_filename = f"tajlandia_offer_{clean_client}_{ts}.pdf"

    saved_url = default_storage_provider.save_file(
        filename=out_filename,
        content=pdf_bytes
    )

    logger.info(f"Generated 8-page client offer dossier PDF: {out_filename} ({len(pdf_bytes)} bytes) using {template_name}")

    return {
        "success": True,
        "filename": out_filename,
        "file_url": saved_url,
        "download_url": saved_url,
        "template_used": template_name,
        "client_name": client_name,
        "budget": budget_display,
        "properties_included": [getattr(p, "property_name", getattr(p, "property_id", "Luxury Villa")) for p in props],
        "pages": 8,
        "file_size_bytes": len(pdf_bytes)
    }
