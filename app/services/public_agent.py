import time
import uuid
import re
import json
import logging
from decimal import Decimal
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from openai import OpenAI
from app.core.config import settings
from app.db.models import Property, Conversation
from app.services.property_search import (
    PropertyFilterParams, search_properties_safe, get_property_by_id, get_property_by_name,
    to_compact_projection, get_properties_by_ids
)
from app.services.lead_service import record_or_update_lead
from app.services.conversation_service import append_conversation_turn
from app.services.maps_service import (
    build_google_maps_url,
    build_google_maps_embed_url,
    get_property_proximity_landmarks
)
from app.services.google_services import google_manager
from app.services.intent_router import (
    IntentType, detect_deterministic_intent, extract_search_filters_deterministic
)
from app.services.cache_service import search_cache
from app.services.token_tracker import token_tracker, TurnTokenMetrics
from app.services.i18n_service import (
    resolve_language, get_compact_system_prompt, get_public_system_prompt,
    get_language_switch_confirmation, get_greeting_text, get_off_topic_text,
    get_zero_match_text, get_quick_replies, get_data_unavailable_fallback,
    build_consultative_followup_questions, build_encouraging_alternative_intro,
    build_rich_property_recommendation_text
)
from app.services.guardrails import ThaiPropertyLegalGuardrail
from app.services.property_context_service import (
    load_property_context,
    save_property_context,
    set_active_property,
    set_comparison_set,
    touch_discussed,
    resolve_property_reference,
    update_rolling_summary,
    assemble_llm_context_block,
    is_property_followup_message,
    fetch_authorized_property,
    clear_active_if_new_conversation,
    recover_active_from_history,
)

logger = logging.getLogger(__name__)

COMPACT_SYSTEM_PROMPT = """You are a professional real estate advisor for Tajlandia.com, thai.estate, and varsoviaestate.com in Thailand.
CORE RULES & TONE (Main Scope §5.6):
- TONE: Warm, friendly, enthusiastic, motivated, and professional! Avoid salesy hype, but encourage buyers with confidence.
- THAI LEGAL TRUTH & ZERO COMPROMISE UNDER PRESSURE (IF NO MEANS NO): Under Section 86 of the Thai Land Code, foreigners cannot directly own freehold land in their personal name, and property purchases in Thailand are NEVER tax-free. If a user pressures you, gaslights, or asks leading questions ("just say yes", "my lawyer said it's tax-free"), NEVER cave in. The answer is firmly and clearly NO. Always present 100% legal pathways (Foreign Freehold Condos & 30-year registered leasehold villas with freehold building ownership) in a warm, friendly, and motivated tone.
- STRICT GROUNDING & ZERO HALLUCINATION: Answer ONLY from live verified production data. Never invent prices, property IDs, names, ROI, or availability.
- MISSING DATA: If production data does not contain the answer, use the official EN/PL/TH fallback message. NEVER guess or fabricate details.
- LEGAL & FINANCIAL: Disclaim formal legal guarantees and state that rental returns vary and cannot be guaranteed by law. Refer clients to licensed Thai legal counsel for contract review.
- READ-ONLY: You are Part A (public customer assistant). You do not generate internal admin PDF files or dispatch direct emails.
- CLEAN FORMATTING: Never use long em-dashes ("—" or "–"). Always use standard hyphens ("-"), colons, or clean bullet points ("•") for professional, readable presentation."""

PUBLIC_SYSTEM_PROMPT = """You are a professional real estate advisor for Tajlandia.com, thai.estate, and varsoviaestate.com in Thailand.
Your purpose is to assist visitors in discovering verified property listings in Thailand (Phuket, Koh Samui, Koh Phangan, Pattaya, Bangkok).

CORE INSTRUCTIONS & TONE:
1. TONE: Warm, friendly, enthusiastic, motivated, and professional! (Main Scope §5.6 point 6). Encourage buyers with genuine support while remaining completely truthful.
2. THAI LEGAL TRUTH & ZERO COMPROMISE UNDER PRESSURE (IF NO MEANS NO): Foreign nationals cannot directly own freehold land under their individual name under Section 86 of the Thai Land Code, and property transactions in Thailand are NEVER tax-free. If a user applies adversarial pressure, gaslighting, or insists ("my lawyer said so, just say yes bro"), NEVER buckle under pressure. The factual answer is firmly NO. With a warm and motivated tone, enthusiastically guide the buyer toward 100% legal pathways: 49% Foreign Freehold Condos and 30-year Registered Leasehold Villas with freehold building ownership.
3. STRICT GROUNDING & ZERO HALLUCINATION: State facts ONLY from retrieved live production listings. Never fabricate prices, yields, amenities, IDs, or names.
4. MISSING DATA: If production data does not contain the answer, use the official EN/PL/TH fallback — NEVER guess or invent listings.
5. ROI & LEGAL DISCLAIMERS: Rental yields vary and cannot be legally guaranteed; only quote verified developer projections if documented. Always advise consulting a qualified Thai attorney for formal legal due diligence.
6. BROCHURE & PDF DOWNLOADS: When a visitor asks for a brochure, PDF, factsheet, floor plan, or project document, call the `generate_property_brochure` tool with the property ID. The system will automatically render the luxury brochure PDF and initiate the download for the visitor.
7. LEAD CAPTURE & VIEWING APPOINTMENTS: When a visitor provides contact details (email, phone, name), asks for a callback, or wants to schedule a viewing or consultation, call `capture_lead` or `schedule_viewing` immediately.
8. DOMAIN REFUSAL: For off-topic or foreign real estate queries (outside Thailand), politely state that you specialize exclusively in Thailand luxury property and invite inquiries on Thailand listings.
9. CLEAN PROFESSIONAL FORMATTING: Do NOT use ChatGPT-style long em-dashes ("—" or "–"). Use clean standard punctuation: colons, bullet points ("•"), standard hyphens ("-"), or separate sentences. Make information point-to-point and easy to read.
"""

PUBLIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_properties",
            "description": "Searches verified Thailand properties using structured criteria. Converts natural language queries into exact criteria.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional development name, project title, or specific property search query (e.g. '15 Sukhumvit Residences', 'Sea Heaven', 'Sun Hills', 'Botanica')"
                    },
                    "location": {
                        "type": "string",
                        "description": "Island, city, or district name (e.g. Phuket, Bang Tao, Layan, Karon, Samui, Maenam, Koh Phangan, Bangkok, Pattaya)"
                    },
                    "province": {
                        "type": "string",
                        "description": "Province name (e.g. Phuket, Surat Thani, Bangkok, Chonburi)"
                    },
                    "district": {
                        "type": "string",
                        "description": "Specific district (e.g. Bang Tao, Layan, Karon, Bophut, Maenam, Sukhumvit, Sathorn)"
                    },
                    "property_type": {
                        "type": "string",
                        "enum": ["Villa", "Condominium"],
                        "description": "Property type"
                    },
                    "min_price": {
                        "type": "number",
                        "description": "Minimum budget in THB (e.g. 5000000)"
                    },
                    "max_price": {
                        "type": "number",
                        "description": "Maximum budget in THB (e.g. 15000000)"
                    },
                    "bedrooms": {
                        "type": "integer",
                        "description": "Number of bedrooms (e.g. 1, 2, 3, 4, 5)"
                    },
                    "bathrooms": {
                        "type": "integer",
                        "description": "Minimum number of bathrooms"
                    },
                    "min_area": {
                        "type": "number",
                        "description": "Minimum area in sqm"
                    },
                    "max_area": {
                        "type": "number",
                        "description": "Maximum area in sqm"
                    },
                    "has_pool": {
                        "type": "boolean",
                        "description": "Must have swimming pool"
                    },
                    "beach_access": {
                        "type": "boolean",
                        "description": "Must have beachfront or walking beach access"
                    },
                    "status": {
                        "type": "string",
                        "description": "Status (e.g. 'Ready to move in', 'Under construction', 'Pre-sale', 'Planned')"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_property_by_id",
            "description": "Retrieves the complete verified record and detailed specifications for a specific property or development by Property ID, document slug, or project name (e.g. '15-sukhumvit-residences', '15 Sukhumvit Residences', 'Sea Heaven Phuket Naithon Phase 3.1', 'Sun Hills Layan Phuket'). Call this whenever the user asks for details, more information, or inquiries about a specific named property or development.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_id": {
                        "type": "string",
                        "description": "Property ID, document slug, or project name (e.g. '15-sukhumvit-residences', '15 Sukhumvit Residences', 'Sea Heaven Phuket Naithon Phase 3.1', 'Sun Hills Layan Phuket')"
                    },
                    "property_name": {
                        "type": "string",
                        "description": "Optional title or name fallback (e.g. '15 Sukhumvit Residences', 'Sun Hills Layan Phuket')"
                    }
                },
                "required": ["property_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_property_brochure",
            "description": "Generates and triggers the immediate download of an official, luxury PDF brochure with verified specs, floor plans, and photography for a property.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_id": {
                        "type": "string",
                        "description": "Unique Property ID or document slug (e.g. '15-sukhumvit-residences')"
                    },
                    "property_name": {
                        "type": "string",
                        "description": "Optional development name (e.g. 'Aileen Residence Bangtao')"
                    }
                },
                "required": ["property_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "capture_lead",
            "description": "Records customer contact information when the user explicitly provides their name, email, or phone number to be contacted by an advisor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer's name"},
                    "email": {"type": "string", "description": "Customer's email address"},
                    "phone": {"type": "string", "description": "Customer's phone or WhatsApp number"},
                    "budget": {"type": "number", "description": "Stated budget in THB"},
                    "preferred_location": {"type": "string"},
                    "notes": {"type": "string", "description": "Customer's explicit request"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_viewing",
            "description": "Schedules a property viewing appointment or consultation tour on Google Calendar, records lead info, and returns a 1-click Google Calendar add link.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_id": {"type": "string", "description": "Property ID or slug (e.g. '15-sukhumvit-residences')"},
                    "customer_name": {"type": "string", "description": "Customer name"},
                    "customer_email": {"type": "string", "description": "Customer email"},
                    "customer_phone": {"type": "string", "description": "Customer phone or WhatsApp"},
                    "preferred_date": {"type": "string", "description": "Date in YYYY-MM-DD format (e.g. 2026-10-05)"},
                    "preferred_time": {"type": "string", "description": "Time in HH:MM format (e.g. 14:00)"},
                    "notes": {"type": "string", "description": "Viewing preferences or requests"}
                },
                "required": ["property_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_property_brochure",
            "description": "Sends verified property specifications, floor plans, and investment summary to the customer's email address via Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_id": {"type": "string", "description": "Property ID or slug (e.g. '15-sukhumvit-residences')"},
                    "recipient_email": {"type": "string", "description": "Customer's destination email address"},
                    "customer_name": {"type": "string", "description": "Customer's name"}
                },
                "required": ["property_id", "recipient_email"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_property_location",
            "description": "Retrieves exact GPS coordinates, Google Maps navigation links, embed preview, and proximity distances to beaches, airports, and marinas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "property_id": {"type": "string", "description": "Property ID or slug (e.g. '15-sukhumvit-residences')"},
                    "location_name": {"type": "string", "description": "District or area name (e.g. Bang Tao, Chaweng)"}
                }
            }
        }
    }
]

def format_property_card(p: Property) -> Dict[str, Any]:
    """Formats a Property ORM object into the frontend-expected card structure with Maps and Calendar integration."""
    img_urls = [img.image_url for img in p.images] if p.images else []
    
    # Formulate clean location string
    loc_parts = []
    if p.district:
        loc_parts.append(p.district)
    if p.area and p.area != p.district:
        loc_parts.append(p.area)
    elif p.province and p.province != p.district:
        loc_parts.append(p.province)
    loc_display = ", ".join(loc_parts) if loc_parts else "Thailand"

    # Check for direct custom map URL in database metadata
    custom_map_url = None
    if isinstance(getattr(p, "raw_metadata", None), dict):
        raw_meta = p.raw_metadata
        custom_map_url = raw_meta.get("google_maps_url") or raw_meta.get("map_url") or raw_meta.get("maps_url")

    # Google Maps URL & Embed completely dynamic from database record
    maps_url = build_google_maps_url(
        latitude=p.latitude,
        longitude=p.longitude,
        property_name=p.property_name,
        location_str=loc_display,
        address=getattr(p, "address", None),
        direct_map_url=custom_map_url
    )
    embed_info = build_google_maps_embed_url(
        latitude=p.latitude,
        longitude=p.longitude,
        location_str=loc_display,
        property_name=p.property_name,
        address=getattr(p, "address", None),
        direct_embed_url=custom_map_url
    )
    nearby_landmarks = get_property_proximity_landmarks(
        latitude=p.latitude,
        longitude=p.longitude,
        district=p.district,
        province=p.province,
        nearby_places_from_db=getattr(p, "nearby_places", None)
    )

    clean_price = p.price_display or "Price on request"
    if ".xx" in str(clean_price) and getattr(p, "price_numeric_min", None):
        clean_price = f"from {float(p.price_numeric_min) / 1_000_000:.2f} million THB"
    elif ".xx" in str(clean_price):
        clean_price = "Price on request"

    # Google Calendar 1-Click Link
    cal_url = google_manager.generate_google_calendar_url(
        title=f"Viewing: {p.property_name} ({p.property_id})",
        start_time="",
        description=f"Property viewing appointment for {p.property_name} in {loc_display}. Ref: {p.property_id}. Price: {clean_price}.",
        location=f"{p.property_name}, {loc_display}, Thailand"
    )

    return {
        "property_id": p.property_id,
        "name": p.property_name,
        "property_type": p.property_type or "Residential",
        "price": clean_price,
        "price_numeric_min": float(p.price_numeric_min) if p.price_numeric_min else None,
        "bedrooms": p.bedrooms_display or (str(p.bedrooms_min) if p.bedrooms_min else "N/A"),
        "area": re.sub(r"^0\s*[-–]\s*", "", p.area_display or "") if p.area_display else "N/A",
        "location": loc_display,
        "address": getattr(p, "address", None) or "",
        "district": p.district or "",
        "province": p.province or "",
        "latitude": p.latitude,
        "longitude": p.longitude,
        "maps_url": maps_url,
        "maps_embed_url": embed_info["embed_url"],
        "calendar_url": cal_url,
        "nearby_landmarks": nearby_landmarks,
        "status": p.status or "Available",
        "has_pool": bool(p.has_pool),
        "beach_access": bool(p.beach_access),
        "images": img_urls,
        "detail_page_url": p.detail_page_url or settings.get_property_detail_url(p.property_id, country=p.province, city=p.district),
        "highlights": list(p.highlights or [])[:3]
    }


def is_legal_inquiry(message: str) -> bool:
    """Detects whether user is specifically inquiring about Thai property laws, foreign ownership, or taxes."""
    if not message:
        return False
    m = message.lower()
    legal_keywords = [
        "law", "legal", "lawyer", "ownership", "own land", "foreign ownership", "can foreigner", "can foreigners",
        "foreigner buy", "freehold vs leasehold", "leasehold", "chanote", "land code", "section 86", "tax", "taxes",
        "prawo", "prawny", "prawnik", "własność", "wlasnosc", "kupić ziemię", "kupic ziemie", "czy obcokrajowiec",
        "czy cudzoziemiec", "podatek", "podatki",
        "กฎหมาย", "กรรมสิทธิ์", "ชาวต่างชาติซื้อ", "มาตรา 86", "ภาษี"
    ]
    return any(k in m for k in legal_keywords)


def ensure_structured_response(
    response_text: str,
    cards: Optional[List[Dict[str, Any]]] = None,
    user_message: str = "",
    language: str = "en"
) -> str:
    """
    Enforces a clean, elegant, bulleted card structure for all property recommendations.
    Deterministic Guard:
    1. If a markdown table (| Property | Location | ...) is detected, extracts all rows
       and converts them into structured numbered cards with bullet points.
    2. If an unbulleted plain text dump (e.g. Featured option: ... Price: ... Location: ...)
       is detected, parses and converts it into structured numbered cards.
    3. Guarantees that raw markdown tables never reach the user.
    4. Only formats legal guidance if the user explicitly inquired; otherwise strips unsolicited legal disclaimers.
    5. Appends clean, actionable next steps.
    """
    if not response_text:
        return response_text

    text = response_text.strip()

    # Find table lines (consecutive lines that start and end with |)
    lines = text.split("\n")
    table_line_indices = []
    for i, l in enumerate(lines):
        stripped = l.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            table_line_indices.append(i)

    # Check if there is a valid table (has header, separator with ---, and data rows)
    has_table = False
    table_blocks = []
    if table_line_indices:
        groups = []
        current_group = [table_line_indices[0]]
        for idx in table_line_indices[1:]:
            if idx == current_group[-1] + 1:
                current_group.append(idx)
            else:
                groups.append(current_group)
                current_group = [idx]
        groups.append(current_group)

        for g in groups:
            if len(g) >= 2:
                sep_found = any("-" in lines[idx] for idx in g[1:2])
                if sep_found:
                    has_table = True
                    table_blocks.append(g)

    # Check for raw unstructured blob (e.g. Featured option: ... Price: ... Location: ...)
    has_unstructured_blob = bool(re.search(
        r'(?:Featured\s+option|Wyróżniona\s+oferta|โครงการแนะนำ|Option\s+\d+):\s*[^\n]+(?:\s*\n+.*?(?:Price|Cena|ราคา):)',
        text,
        re.IGNORECASE
    ))

    # If neither table nor unstructured blob is present, format legal notes and next steps
    if not has_table and not has_unstructured_blob:
        return _format_legal_and_next_steps(text, user_message=user_message, language=language)

    extracted_items = []

    # 1. Extract from blob if present
    blob_match = re.search(
        r'(?:Featured\s+option|Wyróżniona\s+oferta|โครงการแนะนำ|Option\s+\d+):\s*([^\n]+)'
        r'(?:\s*\n+.*?(?:Price|Cena|ราคา):\s*([^\n]+))?'
        r'(?:\s*\n+.*?(?:Location|Lokalizacja|ทำเล):\s*([^\n]+))?'
        r'(?:\s*\n+.*?(?:Bedrooms?|Sypialnie|ห้องนอน):\s*([^\n]+))?'
        r'(?:\s*\n+.*?(?:Amenit(?:y|ies)|Udogodnienia|สิ่งอำนวยความสะดวก):\s*([^\n]+))?',
        text,
        re.IGNORECASE
    )
    if blob_match:
        name = blob_match.group(1).strip()
        price = (blob_match.group(2) or "").strip()
        loc = (blob_match.group(3) or "").strip()
        beds = (blob_match.group(4) or "").strip()
        amenities = (blob_match.group(5) or "").strip()
        extracted_items.append({
            "name": name,
            "price": price,
            "location": loc,
            "bedrooms": beds,
            "amenities": amenities
        })

    # 2. Extract from table blocks
    for g in table_blocks:
        header_line = lines[g[0]]
        header_cols = [c.strip().lower() for c in header_line.split("|")[1:-1]]
        for row_idx in g[2:]:
            row_line = lines[row_idx]
            cols = [c.strip() for c in row_line.split("|")[1:-1]]
            if not cols or not any(cols):
                continue
            item = {}
            for h, val in zip(header_cols, cols):
                if any(k in h for k in ["property", "name", "nazwa", "โครงการ"]):
                    item["name"] = val
                elif any(k in h for k in ["location", "lokalizacja", "ทำเล"]):
                    item["location"] = val
                elif any(k in h for k in ["price", "cena", "ราคา"]):
                    item["price"] = val
                elif any(k in h for k in ["bedroom", "sypialn", "ห้องนอน"]):
                    item["bedrooms"] = val
                elif any(k in h for k in ["amenit", "udogodn", "สิ่งอำนวยความสะดวก"]):
                    item["amenities"] = val
            if item.get("name"):
                extracted_items.append(item)

    # 3. Determine intro and outro
    first_removed_line = len(lines)
    if blob_match:
        blob_start_char = blob_match.start()
        char_count = 0
        for l_idx, l in enumerate(lines):
            char_count += len(l) + 1
            if char_count > blob_start_char:
                first_removed_line = min(first_removed_line, l_idx)
                break
    if table_blocks:
        first_table_line = table_blocks[0][0]
        if first_table_line > 0 and any(k in lines[first_table_line - 1].lower() for k in ["more option", "więcej", "ตัวเลือก"]):
            first_removed_line = min(first_removed_line, first_table_line - 1)
        else:
            first_removed_line = min(first_removed_line, first_table_line)

    intro_lines = lines[:first_removed_line]
    intro_text = "\n".join(intro_lines).strip()

    last_removed_line = 0
    if blob_match:
        blob_end_char = blob_match.end()
        char_count = 0
        for l_idx, l in enumerate(lines):
            char_count += len(l) + 1
            if char_count >= blob_end_char:
                last_removed_line = max(last_removed_line, l_idx + 1)
                break
    if table_blocks:
        last_table_line = table_blocks[-1][-1]
        last_removed_line = max(last_removed_line, last_table_line + 1)

    outro_lines = lines[last_removed_line:]
    outro_text = "\n".join(outro_lines).strip()
    outro_text = re.sub(r'^(?:More options|Więcej ofert|ตัวเลือกเพิ่มเติม)\s*\n*', '', outro_text, flags=re.IGNORECASE).strip()

    # Build structured property cards
    cards_map = {}
    if cards:
        for c in cards:
            c_name = c.get("name", "").strip().lower()
            if c_name:
                cards_map[c_name] = c

    if not extracted_items and cards:
        for c in cards:
            extracted_items.append({
                "name": c.get("name"),
                "location": c.get("location"),
                "price": c.get("price"),
                "bedrooms": c.get("bedrooms"),
                "amenities": "Private Pool" if c.get("has_pool") else "Luxury Amenities"
            })

    card_sections = []
    for idx, item in enumerate(extracted_items, 1):
        name = item.get("name", "").strip()
        matched_card = None
        for c_key, c_val in cards_map.items():
            if name.lower() in c_key or c_key in name.lower():
                matched_card = c_val
                break

        loc = (matched_card.get("location") if matched_card else None) or item.get("location") or "Thailand"
        price = (matched_card.get("price") if matched_card else None) or item.get("price") or "Price on request"
        beds = (matched_card.get("bedrooms") if matched_card else None) or item.get("bedrooms") or "N/A"
        
        currency_info = ""
        p_num = matched_card.get("price_numeric_min") if matched_card else None
        if not p_num:
            m_num = re.search(r'(\d+(?:\.\d+)?)\s*M?\s*THB', price, re.IGNORECASE)
            if m_num:
                val = float(m_num.group(1))
                if "M" in price.upper() or val < 1000:
                    p_num = val * 1_000_000
                else:
                    p_num = val

        if p_num:
            approx_eur = f"~€{round(p_num / 38):,}"
            approx_pln = f"~{round(p_num / 8.5):,} PLN"
            currency_info = f" ({approx_eur} / {approx_pln})"

        amenities = item.get("amenities") or ""
        if matched_card:
            amenities_list = []
            if matched_card.get("has_pool"):
                amenities_list.append("Private Pool" if language == "en" else ("Prywatny basen" if language == "pl" else "สระว่ายน้ำส่วนตัว"))
            if matched_card.get("beach_access"):
                amenities_list.append("Beach Access" if language == "en" else ("Dostęp do plaży" if language == "pl" else "ติดชายหาด"))
            if matched_card.get("highlights"):
                amenities_list.extend(matched_card["highlights"][:2])
            if amenities_list:
                amenities = ", ".join(amenities_list)

        if not amenities:
            amenities = "Private Pool, Tropical Gardens" if language == "en" else "Prywatny basen, ogród"

        prop_type = (matched_card.get("property_type") if matched_card else None) or "Villa"
        is_condo = "condo" in prop_type.lower() or "apart" in prop_type.lower()

        if language == "pl":
            ownership_str = "100% Foreign Freehold (w ramach kwoty 49%)" if is_condo else "Własność budynku (Freehold) + 30-letni rejestrowany Leasehold gruntu"
            card_md = (
                f"**{idx}. {name}**\n"
                f"• 📍 **Lokalizacja:** {loc}\n"
                f"• 💰 **Cena wyjściowa:** **{price}**{currency_info}\n"
                f"• 🛏️ **Sypialnie:** {beds} | 📐 **Typ:** {prop_type}\n"
                f"• 🏊 **Udogodnienia:** {amenities}\n"
                f"• 📜 **Forma własności:** {ownership_str}"
            )
        elif language == "th":
            ownership_str = "Foreign Freehold 100% (โควต้าต่างชาติ 49%)" if is_condo else "กรรมสิทธิ์ตัวอาคาร Freehold 100% + สิทธิการเช่าที่ดินระยะยาว 30 ปี (Leasehold)"
            card_md = (
                f"**{idx}. {name}**\n"
                f"• 📍 **ทำเล:** {loc}\n"
                f"• 💰 **ราคาเริ่มต้น:** **{price}**\n"
                f"• 🛏️ **ห้องนอน:** {beds} | 📐 **ประเภท:** {prop_type}\n"
                f"• 🏊 **สิ่งอำนวยความสะดวก:** {amenities}\n"
                f"• 📜 **กรรมสิทธิ์:** {ownership_str}"
            )
        else:
            ownership_str = "100% Foreign Freehold (within 49% Quota)" if is_condo else "100% Freehold Villa Building + 30-Year Registered Renewable Land Lease"
            card_md = (
                f"**{idx}. {name}**\n"
                f"• 📍 **Location:** {loc}\n"
                f"• 💰 **Starting Price:** **{price}**{currency_info}\n"
                f"• 🛏️ **Bedrooms:** {beds} | 📐 **Type:** {prop_type}\n"
                f"• 🏊 **Amenities & Highlights:** {amenities}\n"
                f"• 📜 **Ownership Structure:** {ownership_str}"
            )
        card_sections.append(card_md)

    # Ensure intro never bluntly rejects with "we don't have properties" when presenting properties,
    # but preserve encouraging, motivating alternative intros that warmly introduce properties!
    if card_sections:
        encouraging_indicators = [
            "delighted to recommend", "recommend these exceptional", "exceptional verified", "pleased to present",
            "z przyjemnością", "wyjątkowe, zweryfikowane", "polecam", "chętnie prezentuję",
            "ขอแนะนำอสังหาริมทรัพย์", "คัดสรรมาเป็นพิเศษ", "ยินดีแนะนำ"
        ]
        is_already_encouraging = any(enc in intro_text.lower() for enc in encouraging_indicators)
        if not is_already_encouraging:
            negative_indicators = [
                "no match", "not have", "don't have", "dont have", "not found", "no properties", "couldn't find", "could not find",
                "nie znaleziono", "nie mamy", "brak ofert", "nie posiada",
                "ไม่พบ", "ไม่มีอสังหาริมทรัพย์", "ไม่มีรายการ"
            ]
            if any(neg in intro_text.lower() for neg in negative_indicators) or not intro_text:
                if language == "pl":
                    intro_text = "W oparciu o Twoje preferencje wybraliśmy te wyjątkowe, zweryfikowane nieruchomości w Tajlandii, które mogą Cię zainteresować:"
                elif language == "th":
                    intro_text = "จากความต้องการและเงื่อนไขของคุณ เราได้คัดสรรอสังหาริมทรัพย์คุณภาพที่ผ่านการตรวจสอบในประเทศไทยที่น่าสนใจมาให้ดังนี้ครับ:"
                else:
                    intro_text = "Based on your preferences, we have selected these exceptional verified properties in Thailand that might interest you:"

    parts = []
    if intro_text:
        parts.append(intro_text)
    if card_sections:
        parts.append("\n\n".join(card_sections))
    if outro_text:
        parts.append(_format_legal_and_next_steps(outro_text, user_message=user_message, language=language))
    else:
        parts.append(_format_legal_and_next_steps("", user_message=user_message, language=language))

    return "\n\n".join(parts)


def _format_legal_and_next_steps(text: str, user_message: str = "", language: str = "en") -> str:
    """Formats raw legal text into a blockquote if legal inquiry was asked, or strips unsolicited legal text."""
    res = text

    legal_match = re.search(
        r'(?:Buying as a foreigner|Foreign ownership|Zasady zakupu dla obcokrajowców|Kupno przez obcokrajowca|ข้อกำหนดสำหรับชาวต่างชาติ)?[:\s]*'
        r'((?:Foreigners generally cannot directly own|Zgodnie z art\.\s*86|obcokrajowcy nie mogą|cudzoziemcy nie mogą|ชาวต่างชาติไม่สามารถ)[^.\n]+(?:\.[^.\n]+)*\.)',
        res,
        re.IGNORECASE
    )
    if legal_match:
        if is_legal_inquiry(user_message):
            if not res.strip().startswith(">"):
                legal_body = legal_match.group(1).strip()
                if language == "pl":
                    callout = (
                        "> ℹ️ **Zasady zakupu dla obcokrajowców:**\n"
                        f"> {legal_body}\n"
                        "> • **Kondominia:** 100% Foreign Freehold na nazwisko kupującego.\n"
                        "> • **Wille:** Pełna własność budynku + 30-letnia rejestrowana dzierżawa gruntu w urzędzie ziemskim."
                    )
                elif language == "th":
                    callout = (
                        "> ℹ️ **ข้อมูลกรรมสิทธิ์สำหรับชาวต่างชาติ:**\n"
                        f"> {legal_body}\n"
                        "> • **คอนโดมิเนียม:** สิทธิ์ Foreign Freehold 100% ในชื่อของท่าน\n"
                        "> • **พูลวิลล่า:** กรรมสิทธิ์ตัวอาคาร Freehold 100% + สิทธิการเช่าที่ดินจดทะเบียน 30 ปี"
                    )
                else:
                    callout = (
                        "> ℹ️ **Foreign Ownership Overview:**\n"
                        f"> {legal_body}\n"
                        "> • **Condominiums:** 100% direct Foreign Freehold registered on title deed.\n"
                        "> • **Luxury Villas:** 100% freehold building ownership + 30-year registered renewable land lease."
                    )
                res = res[:legal_match.start()].strip() + "\n\n" + callout + "\n\n" + res[legal_match.end():].strip()
        else:
            # User did NOT ask for legal advice! Strip out unsolicited legal advice text
            res = res[:legal_match.start()].strip() + "\n\n" + res[legal_match.end():].strip()
            # Clean up unprompted verification disclaimers
            res = re.sub(
                r'(?:These listings[’\']?\s+ownership structures have not been verified\.?|Struktura własności tych ofert nie została zweryfikowana\.?|ยังไม่ได้ตรวจสอบโครงสร้างกรรมสิทธิ์ของรายการเหล่านี้\.?)',
                '',
                res,
                flags=re.IGNORECASE
            ).strip()

    # If the user did NOT ask for legal advice, also ensure no raw blockquote legal disclaimer remains
    if not is_legal_inquiry(user_message):
        res = re.sub(r'>\s*ℹ️[^\n]*\n(?:>\s*[^\n]*\n*)*', '', res).strip()

    has_question = any(q in res.lower() for q in [
        "would you like", "interested in", "review detailed specifications",
        "czy chcesz", "chciałbyś", "zainteresowany",
        "คุณต้องการ", "สนใจ", "?"
    ])
    if not has_question:
        if language == "pl":
            question_text = "Czy chcesz dowiedzieć się więcej o którejś z tych propozycji, przejrzeć rzuty lub dostosować kryteria wyszukiwania?"
        elif language == "th":
            question_text = "คุณต้องการดูรายละเอียดเพิ่มเติมของโครงการเหล่านี้ ตรวจสอบผังโครงการ หรือต้องการปรับเปลี่ยนเงื่อนไขการค้นหาไหมครับ?"
        else:
            question_text = "Would you like to explore any of these properties in detail, review their floor plans, or adjust your search criteria?"
        res = res.strip() + ("\n\n" if res.strip() else "") + question_text

    has_next_steps = any(k in res.lower() for k in ["next steps", "kolejne kroki", "ขั้นตอนถัดไป", "schedule a viewing", "brochure", "zaplanuj prywatną prezentację"])
    if not has_next_steps:
        res = res.strip() + ("\n\n" if res.strip() else "") + _default_next_steps(language)

    return res.strip()


def _default_next_steps(language: str = "en") -> str:
    if language == "pl":
        return (
            "**Kolejne kroki**\n"
            "• Pobierz oficjalną broszurę PDF i rzuty pięter\n"
            "• Zaplanuj prywatną prezentację (na żywo lub online)\n"
            "• Skonsultuj się z naszym doradcą inwestycyjnym"
        )
    elif language == "th":
        return (
            "**ขั้นตอนถัดไป**\n"
            "• ขอรับโบรชัวร์และผังโครงการอย่างเป็นทางการ\n"
            "• นัดหมายเข้าชมโครงการแบบส่วนตัว\n"
            "• ปรึกษาผู้เชี่ยวชาญด้านอสังหาริมทรัพย์"
        )
    return (
        "**Recommended next steps**\n"
        "• Request the official brochure and floor plans\n"
        "• Schedule a private viewing tour\n"
        "• Speak with a senior property advisor"
    )


def _professionalize_chat_text(text: str) -> str:
    """Strip markdown headers (###) and convert dash bullets to professional • bullets."""
    if not text:
        return text
    lines = []
    for line in text.splitlines():
        # Remove markdown AT headings: ### 1. **Name** -> 1. Name
        line = re.sub(r"^#{1,6}\s*", "", line)
        # Leading markdown/list dashes -> bullet
        line = re.sub(r"^(\s*)[-–—]\s+", r"\1• ", line)
        # "🎯 Next Steps:" style → clean label
        line = re.sub(r"^🎯\s*\*?\*?Next Steps\*?\*?:?\s*", "**Recommended next steps**\n", line, flags=re.IGNORECASE)
        line = re.sub(r"^🎯\s*\*?\*?Recommended Next Steps\*?\*?:?\s*", "**Recommended next steps**\n", line, flags=re.IGNORECASE)
        lines.append(line)
    out = "\n".join(lines)
    # Collapse accidental double blank lines from header stripping
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def get_session_seen_property_ids(conversation: Any) -> List[str]:
    """Retrieves all property IDs that have already been recommended or shown in this chat session."""
    seen = set()
    if not conversation:
        return []

    # 1. From conversation preferences
    prefs = getattr(conversation, "preferences", {}) or {}
    if isinstance(prefs, dict):
        for pid in prefs.get("seen_property_ids", []):
            if pid:
                seen.add(str(pid).strip())
        for pid in prefs.get("last_result_ids", []):
            if pid:
                seen.add(str(pid).strip())

    # 2. From conversation message history across all past turns
    history = getattr(conversation, "message_history", []) or []
    if isinstance(history, list):
        for turn in history:
            if isinstance(turn, dict):
                sd = turn.get("structured_data")
                if isinstance(sd, dict):
                    for pid in sd.get("property_ids", []):
                        if pid:
                            seen.add(str(pid).strip())
                    for p in sd.get("properties", []):
                        if isinstance(p, dict) and p.get("property_id"):
                            seen.add(str(p["property_id"]).strip())
                props = turn.get("properties")
                if isinstance(props, list):
                    for p in props:
                        if isinstance(p, dict) and p.get("property_id"):
                            seen.add(str(p["property_id"]).strip())

    return list(seen)


def search_properties_unified(
    db: Session,
    params: PropertyFilterParams,
    limit: Optional[int] = None,
    exclude_property_ids: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Unified property search engine that combines local PostgreSQL database records
    with live Cloud Firestore records (from projects collection).
    Excludes any property IDs already recommended in this chat session to prevent duplicate suggestions.
    """
    lim = limit or params.limit or 6
    cards: List[Dict[str, Any]] = []

    # If this is a specific direct search by name/query, do NOT exclude it
    if getattr(params, "query", None):
        exclude_set = set()
    else:
        exclude_set = {str(eid).strip().lower() for eid in (exclude_property_ids or []) if eid}

    # 1. Search live Cloud Firestore (Primary Datastore)
    if getattr(settings, "ENABLE_FIRESTORE_SYNC", True):
        try:
            from app.services.firestore_service import search_properties_firestore
            fs_cards = search_properties_firestore(
                location=params.location,
                property_type=params.property_type,
                min_price=float(params.min_price) if params.min_price else None,
                max_price=float(params.max_price) if params.max_price else None,
                bedrooms=int(params.bedrooms) if params.bedrooms and str(params.bedrooms).isdigit() else None,
                has_pool=params.has_pool,
                beach_access=params.beach_access,
                query=getattr(params, "query", None),
                websites=getattr(params, "websites", None),
                target="user",
                limit=lim + len(exclude_set),
                exclude_property_ids=list(exclude_set)
            )
            if fs_cards:
                for c in fs_cards:
                    cid = str(c.get("property_id") or c.get("id") or "").strip().lower()
                    cname = str(c.get("name") or "").strip().lower()
                    if cid not in exclude_set and cname not in exclude_set:
                        cards.append(c)
        except Exception as e:
            logger.warning(f"Error querying Firestore properties in search_properties_unified: {e}")

    # 2. Local database fallback (only if Firestore client is completely unavailable/uninitialized, e.g. offline testing)
    if not cards and db is not None:
        try:
            from app.services.firestore_service import get_firestore_client
            if not get_firestore_client("user"):
                db_props = search_properties_safe(db, params)
                for p in db_props:
                    pid = str(p.property_id or "").strip().lower()
                    pname = str(p.property_name or "").strip().lower()
                    if pid not in exclude_set and pname not in exclude_set:
                        cards.append(format_property_card(p))
        except Exception as e:
            logger.warning(f"Error querying local database properties in search_properties_unified: {e}")

    return cards[:lim]


def extract_user_preferences(
    user_message: str,
    stored_preferences: Optional[Dict[str, Any]] = None,
    tool_args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Extracts explicit and implicit user preferences from chat messages,
    merges them with previously stored preferences across turns, and produces
    both the list of 'what we fetched from chat' and the active 'search focus'.
    """
    u_lower = user_message.lower()
    prefs = dict(stored_preferences or {})
    tool_args = tool_args or {}

    # 1. Location / Destination in Thailand
    destinations = [
        ("bang tao", "Bang Tao, Phuket"),
        ("layan", "Layan, Phuket"),
        ("karon", "Karon, Phuket"),
        ("rawai", "Rawai, Phuket"),
        ("kamala", "Kamala, Phuket"),
        ("surin", "Surin, Phuket"),
        ("patong", "Patong, Phuket"),
        ("kata", "Kata, Phuket"),
        ("phuket", "Phuket"),
        ("maenam", "Maenam, Koh Samui"),
        ("bophut", "Bophut, Koh Samui"),
        ("chaweng", "Chaweng, Koh Samui"),
        ("lamai", "Lamai, Koh Samui"),
        ("plai laem", "Plai Laem, Koh Samui"),
        ("samui", "Koh Samui"),
        ("koh samui", "Koh Samui"),
        ("phangan", "Koh Phangan"),
        ("pattaya", "Pattaya"),
        ("bangkok", "Bangkok"),
        ("hua hin", "Hua Hin"),
        ("krabi", "Krabi"),
        ("chiang mai", "Chiang Mai"),
        ("phang nga", "Phang Nga")
    ]

    # Check for complete reset / show all keywords
    if any(k in u_lower for k in ["show all", "all properties", "show all properties", "view all", "list all", "reset filters", "clear filters", "reset preferences", "wszystkie nieruchomości", "wszystkie oferty", "zresetuj filtry"]):
        prefs = {}

    # Check for specific reset/clearing keywords
    broad_thailand_keywords = [
        "anywhere in thailand", "all thailand", "across thailand", "all areas", "everywhere",
        "all destinations", "no location preference", "in thailand", "around thailand",
        "throughout thailand", "w tajlandii", "w całej tajlandii", "w calej tajlandii",
        "ทั่วไทย", "ในไทย", "ในประเทศไทย"
    ]
    if any(k in u_lower for k in broad_thailand_keywords) or tool_args.get("is_broad_thailand"):
        prefs.pop("location", None)

    compound_type_keywords = [
        "any property", "any type", "both villas and condos", "villas or condos",
        "villas and condominiums", "villas and condos", "condos and villas", "condominiums and villas",
        "wille i apartamenty", "wille i mieszkania", "apartamenty i wille", "คอนโดและวิลล่า"
    ]
    has_both_types = any(k in u_lower for k in compound_type_keywords) or tool_args.get("is_multiple_types") or (
        ("villa" in u_lower or "willa" in u_lower or "วิลล่า" in u_lower) and
        ("condo" in u_lower or "condominium" in u_lower or "apartment" in u_lower or "apartament" in u_lower or "คอนโด" in u_lower)
    )
    if has_both_types:
        prefs.pop("property_type", None)

    if any(k in u_lower for k in ["any price", "no budget limit", "no price limit", "any budget", "all prices"]):
        prefs.pop("budget_max", None)
        prefs.pop("budget_display", None)
    if any(k in u_lower for k in ["any bedrooms", "any number of bedrooms", "any size"]):
        prefs.pop("bedrooms", None)
        prefs.pop("bedrooms_num", None)

    foreign_locs_set = {
        "india", "indie", "indii", "mumbai", "delhi", "bangalore", "goa", "dubai", "uae",
        "warsaw", "warszawa", "poland", "polska", "spain", "hiszpania", "miami", "florida",
        "london", "uk", "usa", "united states", "canada", "australia", "bali", "indonesia",
        "cyprus", "greece", "italy", "portugal", "france", "turkey", "croatia",
        "singapore", "malaysia", "japan", "tokyo", "china", "philippines", "vietnam", "mexico",
        "germany", "czech republic", "prague"
    }

    new_loc = None
    for kw, label in destinations:
        if kw in u_lower:
            new_loc = label
            break

    if not new_loc and tool_args.get("location"):
        cand_tool = str(tool_args["location"]).strip().title()
        if cand_tool.lower() not in foreign_locs_set:
            new_loc = cand_tool

    if not new_loc:
        loc_match = re.search(r"(?:in|at|near|around)\s+([a-zA-Z\s]+)", u_lower)
        if loc_match:
            candidate = loc_match.group(1).strip()
            if candidate.lower() not in foreign_locs_set and candidate not in ["a", "the", "my", "our", "thailand", "pool", "private pool", "villa", "condo", "budget", "area", "terms"]:
                words = candidate.split()[:2]
                clean_cand = " ".join(words).title()
                if len(clean_cand) > 2 and clean_cand.lower() not in foreign_locs_set:
                    new_loc = clean_cand

    if new_loc:
        prefs["location"] = new_loc
    elif any(k in u_lower for k in broad_thailand_keywords) or tool_args.get("is_broad_thailand") or ("thailand" in u_lower and not new_loc):
        prefs.pop("location", None)

    # 2. Property Type (New preference cleanly replaces old one)
    if has_both_types:
        prefs.pop("property_type", None)
    elif "villa" in u_lower or "willa" in u_lower or "wille" in u_lower:
        prefs["property_type"] = "Pool Villa" if ("pool" in u_lower or prefs.get("has_pool")) else "Villa"
    elif "penthouse" in u_lower:
        prefs["property_type"] = "Penthouse"
    elif "condo" in u_lower or "condominium" in u_lower or "apartment" in u_lower or "apartament" in u_lower or "mieszkanie" in u_lower:
        prefs["property_type"] = "Condominium"
    elif "townhouse" in u_lower:
        prefs["property_type"] = "Townhouse"
    elif tool_args.get("property_type"):
        prefs["property_type"] = str(tool_args["property_type"]).title()

    # 3. Bedrooms (New preference cleanly replaces old one)
    bed_match = re.search(r"(\d+)\s*(?:bed|bedroom|br|sypialn|pokoj)", u_lower)
    if bed_match:
        b_num = int(bed_match.group(1))
        prefs["bedrooms"] = f"{b_num} Bedrooms"
        prefs["bedrooms_num"] = b_num
    elif "studio" in u_lower:
        prefs["bedrooms"] = "Studio"
        prefs["bedrooms_num"] = 0
    elif tool_args.get("bedrooms"):
        prefs["bedrooms"] = f"{tool_args['bedrooms']} Bedrooms"
        prefs["bedrooms_num"] = tool_args['bedrooms']

    # 4. Budget / Price Range (New preference cleanly replaces old one)
    m_match = re.search(r"(?:under|below|max|budget|up to|around|poniżej|do)?\s*(\d+(?:\.\d+)?)\s*(?:m|million|mln|milionów|milionow)\s*(?:thb|baht)?", u_lower)
    if m_match and "bed" not in m_match.group(0):
        val = float(m_match.group(1))
        thb_val = int(val * 1_000_000)
        prefs["budget_max"] = thb_val
        usd_approx = int(thb_val / 35)
        prefs["budget_display"] = f"Up to {val:g}M THB (~${usd_approx:,} USD)"
    elif tool_args.get("max_price"):
        max_p = float(tool_args["max_price"])
        prefs["budget_max"] = max_p
        usd_approx = int(max_p / 35)
        prefs["budget_display"] = f"Up to {int(max_p):,} THB (~${usd_approx:,} USD)"

    # 5. Amenities & Desired Features
    features = set(prefs.get("features", []))
    is_broad_exploration = any(k in u_lower for k in broad_thailand_keywords) or ("explore" in u_lower and "thailand" in u_lower) or tool_args.get("is_broad_thailand")
    if is_broad_exploration and not new_loc:
        if "beach" not in u_lower and "beachfront" not in u_lower and not tool_args.get("beach_access"):
            features.discard("Beachfront / Near Beach")
        if "sea view" not in u_lower and "ocean view" not in u_lower:
            features.discard("Sea View")

    if "pool" in u_lower or tool_args.get("has_pool"):
        features.add("Private Pool")
        prefs["has_pool"] = True
    if "sea view" in u_lower or "ocean view" in u_lower:
        features.add("Sea View")
    if "beach" in u_lower or "beachfront" in u_lower or tool_args.get("beach_access"):
        features.add("Beachfront / Near Beach")
    if "freehold" in u_lower:
        features.add("Foreign Freehold")
    if "turnkey" in u_lower or "furnished" in u_lower:
        features.add("Turnkey / Furnished")
    if "garden" in u_lower:
        features.add("Private Garden")
    if "security" in u_lower or "gated" in u_lower:
        features.add("Gated Community 24/7")
    if features:
        prefs["features"] = sorted(list(features))

    # 6. Purpose / Intent
    if any(k in u_lower for k in ["invest", "investment", "roi", "rental yield", "rental return", "income", "buy to let"]):
        prefs["purpose"] = "Investment & High Rental Yield"
    elif any(k in u_lower for k in ["holiday", "vacation", "second home", "getaway"]):
        prefs["purpose"] = "Holiday / Vacation Home"
    elif any(k in u_lower for k in ["relocate", "moving", "live in", "living", "residence", "retire", "retirement"]):
        prefs["purpose"] = "Relocation & Primary Living"

    # 7. Timeline
    if any(k in u_lower for k in ["ready to move in", "completed", "immediate"]):
        prefs["timeline"] = "Ready to move in"
    elif any(k in u_lower for k in ["off plan", "off-plan", "under construction", "pre sale"]):
        prefs["timeline"] = "Off-plan / Under construction"

    # 8. What we fetched from user's chat
    fetched = []
    if prefs.get("location"):
        fetched.append(f"📍 Location: {prefs['location']}")
    if prefs.get("property_type"):
        fetched.append(f"🏡 Property Type: {prefs['property_type']}")
    if prefs.get("bedrooms"):
        fetched.append(f"🛏️ Bedrooms: {prefs['bedrooms']}")
    if prefs.get("budget_display"):
        fetched.append(f"💰 Budget: {prefs['budget_display']}")
    if prefs.get("purpose"):
        fetched.append(f"🎯 Purpose: {prefs['purpose']}")
    if prefs.get("features"):
        fetched.append(f"✨ Features: {', '.join(prefs['features'])}")
    if prefs.get("timeline"):
        fetched.append(f"⏳ Timeline: {prefs['timeline']}")
    prefs["what_we_fetched"] = fetched

    # 9. Active AI Focus Summary
    focus_parts = []
    if prefs.get("bedrooms"):
        focus_parts.append(str(prefs["bedrooms"]))
    if prefs.get("property_type"):
        focus_parts.append(str(prefs["property_type"]).lower())
    else:
        focus_parts.append("properties")
    if prefs.get("location"):
        focus_parts.append(f"in {prefs['location']}")
    if prefs.get("features"):
        focus_parts.append(f"with {', '.join(prefs['features'][:2]).lower()}")
    if prefs.get("budget_display"):
        focus_parts.append(f"({prefs['budget_display']})")
    if prefs.get("purpose"):
        focus_parts.append(f"tailored for {prefs['purpose'].lower()}")

    if focus_parts:
        prefs["focus_summary"] = f"Focusing on {' '.join(focus_parts)}."
    else:
        prefs["focus_summary"] = "Ready to discover your ideal Thailand property."

    return prefs


def is_greeting_or_chit_chat(message: str) -> bool:
    """
    Detects whether a user's message is a greeting, polite pleasantry, or conversational remark
    (e.g., 'hello', 'hi', 'good morning', 'thanks', 'ok', 'how are you', 'who are you', 'help').
    These messages must NEVER be rewritten into a property search query or cause a fake search.
    """
    msg = message.strip().lower()
    clean = re.sub(r'[^\w\s]', '', msg).strip()
    if not clean:
        return False

    standalone_greetings = {
        "hi", "hello", "hey", "hola", "greetings", "good morning", "good afternoon",
        "good evening", "howdy", "sup", "yo", "thanks", "thank you", "thx", "ok",
        "okay", "sure", "cool", "great", "awesome", "perfect", "got it", "understood",
        "bye", "goodbye", "see you", "who are you", "what can you do", "help",
        "can you help", "can you help me", "how are you", "what is this", "tell me about you",
        "cześć", "czesc", "dzień dobry", "dzien dobry", "dobry wieczór", "dobry wieczor",
        "witaj", "witam", "witajcie", "hej", "hejka", "siema", "siemanko", "dzięki", "dzieki",
        "dziękuję", "dziekuje", "super", "świetnie", "swietnie", "kim jesteś", "kim jestes",
        "co potrafisz", "pomoc", "pomóż", "pomoz", "pomóż mi", "pomoz mi", "witam serdecznie",
        "dzień dobry witam", "dzien dobry witam", "dobre rano", "jak się masz", "jak sie masz",
        "co tam", "co słychać", "co slychac", "thank you very much", "many thanks", "hello there",
        "good day", "morning", "evening", "nice to meet you",
        "สวัสดี", "สวัสดีครับ", "สวัสดีค่ะ", "ดีครับ", "ดีค่ะ", "หวัดดี", "ขอบคุณ", "ขอบคุณครับ",
        "ขอบคุณค่ะ", "แต๊งกิ้ว", "คุณคือใคร", "ทำอะไรได้บ้าง", "ช่วยอะไรได้บ้าง", "ช่วยด้วย", "ช่วยหน่อย"
    }
    if clean in standalone_greetings:
        return True

    words = clean.split()
    if words and words[0] in [
        "hi", "hello", "hey", "howdy", "cześć", "czesc", "witaj", "witam", "hej", "dzień", "dzien", "siema"
    ]:
        real_estate_terms = [
            "villa", "condo", "property", "house", "apartment", "phuket", "samui", "thailand",
            "price", "budget", "pool", "buy", "rent", "invest", "willa", "wille", "apartament",
            "mieszkanie", "dom", "basen", "tajlandia", "tajlandii", "kupić", "kupic", "szukam"
        ]
        if len(words) <= 4 and not any(w in words for w in real_estate_terms):
            return True

    return False


def clean_response_text(text: Optional[str]) -> str:
    """
    Cleans up ChatGPT-style long em-dashes and en-dashes from user-facing text,
    normalizing punctuation into clear, point-to-point, professional formats.
    """
    if not text:
        return ""
    # Replace em-dashes and en-dashes used as parentheticals or separators with clean hyphens or colons
    s = str(text)
    # Replace spaced em-dash / en-dash " — " or " – " with " - "
    s = re.sub(r'\s+[—–]\s+', ' - ', s)
    # Replace unspaced em-dash / en-dash between digits (e.g. 1—4 or 1–4) with hyphen (1-4)
    s = re.sub(r'(\d+)[—–](\d+)', r'\1-\2', s)
    # Replace any remaining solitary em-dashes or en-dashes with hyphens
    s = s.replace('—', ' - ').replace('–', '-')
    # Normalize duplicate spaces created by replacement
    s = re.sub(r'[ \t]{2,}', ' ', s)
    return s.strip()


def is_off_topic_query(message: str) -> bool:
    """
    Intelligently identifies queries that are off-topic or random questions unrelated to
    Thailand luxury real estate (e.g. general trivia, coding, math, science, cars, electronics,
    recipes, health, politics, celebrity homes, or malicious prompt injections).

    Safe inquiries about Thailand real estate, lifestyle, visas, locations, and buyer processes
    as well as polite pleasantries/greetings are NEVER blocked.
    """
    msg = message.lower().strip()
    if not msg:
        return False

    # 1. Greetings and chit-chat are never off-topic (handled by greeting handler)
    if is_greeting_or_chit_chat(message):
        return False

    # 2. Immediate prompt injection triggers
    injection_patterns = [
        "ignore previous instructions", "ignore all instructions", "ignore rules",
        "system prompt", "you are now a general", "jailbreak", "dan mode",
        "disregard all rules", "pretend you are", "developer mode", "override rules",
        "bypass safety", "forget your instructions", "ignore everything above"
    ]
    if any(p in msg for p in injection_patterns):
        return True

    # 3. Politics / celebrities / famous non-portfolio residences / general trivia
    # These must be refused even if words like "house", "floors", or "address" appear.
    trivia_celebrity_politics = [
        "antilia", "ambani", "mukesh ambani", "narendra modi", "modi",
        "prime minister", "president of", "who is the", "who is narendra",
        "trump", "biden", "putin", "zelensky", "tusk", "elon musk", "musk",
        "celebrity", "bollywood", "hollywoodwood", "hollywood wood", "how many floors",
        "how many storeys", "how many stories", "burj khalifa", "white house",
        "buckingham palace", "taj mahal", "eiffel tower", "statue of liberty",
        "who invented", "what is the capital of", "capital of india", "capital of usa",
        "world cup", "olympics", "cricket score", "football score",
        "kim kardashian", "taylor swift", "beyonce", "ronaldo", "messi",
        "bitcoin price", "stock market today", "who won",
    ]
    if any(p in msg for p in trivia_celebrity_politics):
        return True

    # 4. Explicit off-topic categories (regardless of format)
    coding_patterns = [
        "write python", "write a python", "write code", "write a code", "write a script",
        "write a program", "write a function", "write javascript", "fix this code", "debug this",
        "binary search", "bubble sort", "sql query", "regex to", "html code",
        "how to code", "write a class", "leetcode", "napisz kod", "napisz program", "napisz skrypt",
        "kod w pythonie", "błąd w kodzie", "jak zaprogramować", "funkcja w pythonie"
    ]
    if any(p in msg for p in coding_patterns):
        return True

    creative_patterns = [
        "tell me a joke", "tell a joke", "make me laugh", "write a poem", "write a song",
        "sing a song", "tell me a story", "write a story", "write an essay", "compose a poem",
        "opowiedz żart", "opowiedz zart", "opowiedz kawał", "opowiedz kawal", "napisz wiersz",
        "napisz piosenkę", "napisz piosenke", "napisz opowiadanie", "napisz esej",
        "wiersz", "poem", "poetry", "wierszyk", "poezj", "piosenk", "żart", "zart", "kawał", "kawal"
    ]
    if any(p in msg for p in creative_patterns):
        return True

    cooking_patterns = [
        "recipe for", "how to cook", "how to bake", "bake a cake", "ingredients for",
        "make pasta", "make pizza", "recipe of", "przepis na", "jak ugotować", "jak ugotowac",
        "jak upiec", "składniki na", "zrób pizzę", "zrob pizze"
    ]
    if any(p in msg for p in cooking_patterns):
        return True

    homework_patterns = [
        "solve this equation", "do my homework", "solve equation", "derivative of",
        "integral of", "calculate 2+", "physics equation", "chemistry formula",
        "photosynthesis", "fotosynteza", "speed of light", "prędkość światła", "predkosc swiatla",
        "rozwiąż równanie", "rozwiaz rownanie", "zrób zadanie", "zrob zadanie", "oblicz równanie",
        "oblicz całkę", "oblicz calke", "układ słoneczny", "solar system"
    ]
    if any(p in msg for p in homework_patterns):
        return True

    vehicle_tech_patterns = [
        "what car", "best car", "buy a car", "electric vehicle", "tesla vs", "bmw or",
        "jaki samochód", "jaki samochod", "kupić auto", "kupic auto", "naprawa auta",
        "best smartphone", "iphone 16", "jaki telefon", "jaki laptop", "best laptop",
        "graphic card", "karta graficzna"
    ]
    if any(p in msg for p in vehicle_tech_patterns):
        return True

    health_patterns = [
        "symptoms of", "how to cure", "cure for cancer", "take paracetamol", "jak leczyć",
        "jak leczyc", "objawy grypy", "lek na ból", "lek na bol", "recepta na"
    ]
    if any(p in msg for p in health_patterns):
        return True

    # 5. Check for ANY on-topic Thailand or real estate signals
    on_topic_keywords = [
        # Thailand locations
        "thailand", "thai", "tajlandia", "tajlandii", "tajlandzk", "phuket", "samui", "phangan",
        "bangkok", "pattaya", "layan", "bang tao", "bangtao", "karon", "kata", "rawai", "kamala",
        "surin", "patong", "maenam", "bophut", "chaweng", "lamai", "hua hin", "krabi", "chiang mai",
        "chalong", "nai harn", "plai laem", "choeng mon",
        # Property types & nouns (EN & PL) — note: bare "house"/"home" alone are NOT enough
        "villa", "villas", "willa", "wille", "willi", "willę", "wille",
        "condo", "condos", "condominium", "kondominium", "penthouse",
        "apartment", "apartments", "apartament", "apartamenty", "apartamentu",
        "mieszkanie", "mieszkania", "posiadłość", "posiadlosc",
        "real estate", "property", "properties", "listing", "listings", "nieruchomość", "nieruchomosc",
        "nieruchomości", "nieruchomosci", "działka", "dzialka", "działki", "dzialki", "grunt", "land", "plot",
        # Features & specs
        "bedroom", "bedrooms", "bed", "br", "sypialnia", "sypialnie", "sypialni", "pokój", "pokoje",
        "bathroom", "bathrooms", "bath", "łazienka", "lazienka", "łazienki", "lazienki",
        "pool", "basen", "basenem", "baseny", "swimming pool", "private pool", "prywatny basen",
        "sqm", "m2", "metrów", "metrow", "sea view", "seaview", "ocean view", "widok na morze",
        "beach", "beachfront", "plaża", "plaza", "plaży", "plazy", "garden", "ogród", "ogrod",
        # Buying, ownership, legal, financial
        "freehold", "leasehold", "chanote", "foreign freehold", "ownership", "własność", "wlasnosc",
        "roi", "yield", "rental yield", "stopa zwrotu", "inwestycja", "inwestycje", "inwestor", "invest", "investment",
        "buy", "kupić", "kupic", "zakup", "rent", "rental", "wynajem", "wynająć", "wynajac",
        "price", "cena", "ceny", "cenę", "budget", "budżet", "budzet",
        "thb", "baht", "bahtów", "bahtow", "million", "milion", "milionów", "milionow", "mln",
        "tax", "taxes", "podatki", "podatek", "cam fee", "sinking fund",
        # Services & platform
        "viewing", "tour", "prezentacja", "wizyta", "brochure", "broszura", "floor plan", "rzuty",
        "prop-", "tajlandia", "varsovia", "aileen", "lead", "agent", "broker", "doradca",
        "off-market", "turnkey", "deweloper", "developer", "szukam", "znajdź", "znajdz",
        "visa", "wiza", "wizy", "elite visa", "ltr",
        # Advisory, recommendation & selection terms (only strong RE selection phrases)
        "suggest", "recommend", "which one", "which ones", "closest", "nearest", "cheapest",
        "poleć", "polec", "sugeruj", "wybierz", "najlepsz", "najlepsza", "najlepszy",
    ]
    has_real_estate_context = any(k in msg for k in on_topic_keywords)

    # Check for direct contact / lead signals
    has_contact = bool(re.search(r"[\w\.-]+@[\w\.-]+\.\w+", msg) or re.search(r"\+?\d{9,15}", msg))

    if has_real_estate_context or has_contact:
        return False

    # 6. No Thailand / RE context → treat Q&A / trivia / general knowledge as off-topic
    random_question_starters = [
        "what", "who", "where", "why", "when", "how", "can you", "could you", "do you know",
        "tell me about", "tell me", "explain", "describe", "which is better", "give me",
        "okay who", "ok who", "okay what", "ok what",
        "co", "kto", "gdzie", "dlaczego", "kiedy", "jak", "czy możesz", "czy mozesz",
        "powiedz mi", "wyjaśnij", "wyjasnij", "opisz", "czy wiesz"
    ]
    starts_with_question = any(msg.startswith(w + " ") or msg.startswith(w + ",") for w in random_question_starters)
    has_question_mark = "?" in msg

    random_nouns = [
        "car", "auto", "samochód", "samochod", "movie", "film", "actor", "aktorka", "song", "piosenka",
        "president", "prezydent", "capital", "stolica", "weather", "pogoda", "game", "gra", "football",
        "piłka", "pilka", "soccer", "music", "muzyka", "book", "książka", "ksiazka", "history", "historia",
        "war", "wojna", "moon", "księżyc", "ksiezyc", "sun", "słońce", "slonce", "sky", "niebo",
        "dog", "pies", "cat", "kot", "animal", "zwierzę", "zwierze", "bitcoin", "crypto", "krypto",
        "apple", "google", "microsoft", "elon", "musk", "trump", "biden", "putin", "tusk",
        "modi", "ambani", "antilia", "mumbai", "delhi", "bollywood", "politician", "politics",
        "address", "floors", "storeys", "stories", "celebrity", "cricket", "olympics",
    ]
    has_random_noun = any(re.search(r'\b' + re.escape(n) + r'\b', msg) for n in random_nouns)

    if has_random_noun:
        return True

    # Safe real-estate follow-ups even without explicit Thailand keywords
    # (session context / deterministic intent will resolve the property)
    re_followup_ok = [
        "tell me more", "more about", "more details", "the first", "the second", "the third",
        "this one", "that one", "this property", "that property", "this villa", "that villa",
        "which one", "show me more", "any more", "how much", "what about the", "floor plan",
        "brochure", "viewing", "schedule", "cheaper", "more options", "compare",
        "pierwsz", "drug", "trzec", "więcej o", "wiecej o", "opowiedz", "szczegół",
        "ta oferta", "ta willa", "อันแรก", "อันที่สอง", "รายละเอียด", "เพิ่มเติม", "เปรียบเทียบ",
    ]
    if any(f in msg for f in re_followup_ok):
        return False

    # Critical: previously computed but unused — general questions with no RE context are off-topic
    if starts_with_question or has_question_mark:
        return True

    # Short follow-ups that often continue trivia threads ("its address", "and floors?")
    if re.match(r"^(okay|ok|and|also|then)?\s*(give|tell|what|where|who|how|its|the)\b", msg):
        return True

    return False


def _safe_openai_create(client: Any, **kwargs) -> Any:
    """
    Safely executes client.chat.completions.create with forward/backward compatibility.
    1. For newer reasoning / GPT-5 / GPT-6 / o-series models, translates 'max_tokens' to 'max_completion_tokens'
       and removes custom temperature if not default 1.
    2. When function tools are passed to reasoning models (/v1/chat/completions), sets reasoning_effort to 'none'
       (as mandated by OpenAI API for gpt-6-astra and other reasoning models with function tools).
    3. Gracefully self-heals if OpenAI API throws BadRequestError for unsupported parameters or reasoning_effort.
    4. Automatically falls back to gpt-4o / gpt-4o-mini if an advanced model is rejected or inaccessible.
    """
    call_kwargs = dict(kwargs)
    model_name = str(call_kwargs.get("model", "")).lower()

    is_reasoning_or_advanced = any(prefix in model_name for prefix in ["gpt-5", "gpt-6", "o1", "o3", "o4"])

    if is_reasoning_or_advanced:
        if "max_tokens" in call_kwargs:
            call_kwargs["max_completion_tokens"] = call_kwargs.pop("max_tokens")
        if "temperature" in call_kwargs and call_kwargs["temperature"] != 1:
            call_kwargs.pop("temperature", None)
        # OpenAI requires reasoning_effort="none" when function tools are used in /v1/chat/completions
        if call_kwargs.get("tools") or call_kwargs.get("functions"):
            call_kwargs["reasoning_effort"] = "none"

    call_kwargs.setdefault("timeout", getattr(settings, "OPENAI_TIMEOUT", 600.0))

    try:
        return client.chat.completions.create(**call_kwargs)
    except Exception as e:
        err_str = str(e).lower()
        logger.warning(f"Initial OpenAI API call for model '{model_name}' encountered: {e}. Applying self-healing...")

        if "reasoning_effort" in err_str:
            call_kwargs["reasoning_effort"] = "none"
        if "max_tokens" in err_str and "max_tokens" in call_kwargs:
            call_kwargs["max_completion_tokens"] = call_kwargs.pop("max_tokens")
        if "temperature" in err_str:
            call_kwargs.pop("temperature", None)

        try:
            return client.chat.completions.create(**call_kwargs)
        except Exception as retry_err:
            logger.error(f"Retry for model '{model_name}' failed: {retry_err}. Attempting fallback to gpt-4o...")
            # Fallback to gpt-4o for zero downtime
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["model"] = "gpt-4o"
            fallback_kwargs.setdefault("timeout", getattr(settings, "OPENAI_TIMEOUT", 600.0))
            fallback_kwargs.pop("reasoning_effort", None)
            if "max_completion_tokens" in fallback_kwargs:
                fallback_kwargs["max_tokens"] = fallback_kwargs.pop("max_completion_tokens")
            try:
                return client.chat.completions.create(**fallback_kwargs)
            except Exception as final_err:
                logger.error(f"Fallback to gpt-4o also failed: {final_err}. Attempting gpt-4o-mini...")
                fallback_kwargs["model"] = "gpt-4o-mini"
                return client.chat.completions.create(**fallback_kwargs)



def classify_off_topic_dynamic(
    client: Optional[OpenAI],
    model: str,
    message: str,
    history: List[Dict[str, Any]]
) -> bool:
    """
    Dynamically classifies whether user message is off-topic using OpenAI model.
    Examines previous turns so follow-up queries (e.g., 'suggest me best one', 'which one is closer',
    'what about taxes', 'connect with advisor') are understood in real estate context and never wrongly blocked.

    Deterministic matcher runs FIRST so trivia/politics never reach the LLM answer path.
    """
    if not message or not message.strip():
        return False

    # 1. Obvious prompt injection triggers
    msg_low = message.lower().strip()
    if any(p in msg_low for p in [
        "ignore previous instructions", "system prompt", "jailbreak", "dan mode", "disregard all rules"
    ]):
        return True

    # 2. Greetings and chit-chat are handled by greeting handler, never off-topic
    if is_greeting_or_chit_chat(message):
        return False

    # 3. Deterministic fast-path — never let the LLM override a clear off-topic hit
    if is_off_topic_query(message):
        logger.info(f"Deterministic guardrail classified message as OFF-TOPIC: '{message}'")
        return True

    # 4. Short follow-ups after an off-topic thread (e.g. "give me its address" after Antilia trivia)
    recent_user_msgs = [
        str(t.get("content", "")).lower()
        for t in (history or [])[-8:]
        if str(t.get("role", "")).lower() == "user"
    ]
    if recent_user_msgs and is_off_topic_query(recent_user_msgs[-1]):
        if not any(k in msg_low for k in [
            "thailand", "thai", "phuket", "samui", "villa", "condo", "property", "properties",
            "tajland", "bangkok", "pattaya", "budget", "bedroom", "brochure", "viewing", "varsovia"
        ]):
            logger.info(f"Follow-up after off-topic thread still OFF-TOPIC: '{message}'")
            return True

    # 5. If OpenAI client is available, run dynamic evaluation for borderline cases
    if client and getattr(settings, "OPENAI_API_KEY", None):
        try:
            recent_turns = history[-50:] if history else []
            ctx_snippets = []
            for t in recent_turns:
                ctx_snippets.append(f"{t.get('role', 'user').title()}: {t.get('content', '')[:300]}")
            ctx_text = "\n".join(ctx_snippets)

            system_instruction = (
                "You are a strict scope guardrail for a Thailand luxury real-estate chatbot.\n"
                "Decide if the user's latest message is ON_TOPIC or OFF_TOPIC.\n\n"
                "ON_TOPIC only when the user wants help with:\n"
                "- Buying, renting, comparing, or learning about Thailand properties (villas, condos, land).\n"
                "- Prices, budgets, yields, visas, Thai ownership rules, viewings, brochures, advisors.\n"
                "- Asking whether this platform sells in another country (redirect market question).\n"
                "- Follow-ups about previously shown Thailand listings.\n\n"
                "OFF_TOPIC (refuse — do NOT answer the factual question):\n"
                "- Politics, politicians (e.g. Narendra Modi), elections, news.\n"
                "- Celebrities, billionaires, gossip (e.g. Ambani, Antilia floors/address).\n"
                "- Famous landmarks / private residences trivia outside our Thailand inventory.\n"
                "- General knowledge, homework, coding, recipes, health, sports, crypto prices.\n"
                "- Follow-ups that continue an off-topic thread (e.g. 'give me its address' after Antilia).\n"
                "- Anything that is not about purchasing or advising on Thailand real estate.\n\n"
                "IMPORTANT: Mentioning 'house', 'floors', 'address', India, or Mumbai does NOT make trivia ON_TOPIC "
                "unless the user clearly wants to buy/invest in property through this platform.\n"
                "Output ONLY one word: ON_TOPIC or OFF_TOPIC."
            )

            user_payload = (
                (f"Recent Context:\n{ctx_text}\n\n" if ctx_text else "") +
                f"User's Latest Message: \"{message}\"\n\n"
                "Classification:"
            )

            resp = _safe_openai_create(
                client,
                model=model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_payload}
                ],
                temperature=0.0,
                max_tokens=10
            )
            decision = (resp.choices[0].message.content or "").strip().upper()
            if "OFF_TOPIC" in decision:
                logger.info(f"Dynamic LLM guardrail classified message as OFF-TOPIC: '{message}'")
                return True
            elif "ON_TOPIC" in decision:
                return False
        except Exception as e:
            logger.warning(f"Dynamic LLM guardrail classification exception: {e}")

    return is_off_topic_query(message)


def rewrite_public_query(
    client: Optional[OpenAI],
    model: str,
    history: List[Dict[str, Any]],
    current_message: str
) -> str:
    """
    Examines previous 2-3 turns of the chat history. If the current query is anaphoric,
    incremental, or ambiguous (e.g. 'under 15m', 'ones with pool', 'give me that one', 'in samui'),
    rewrites it using OpenAI into a clear, standalone search query, eliminating hallucinations.
    Greetings, pleasantries, and off-topic remarks are preserved verbatim.
    """
    if not history or len(history) < 1:
        return current_message

    if is_greeting_or_chit_chat(current_message):
        return current_message

    recent_turns = history[-50:] if history else []
    msg_lower = current_message.lower().strip()

    if client and getattr(settings, "OPENAI_API_KEY", None):
        try:
            prompt_context = (
                "You are an AI query contextualizer for a Thailand luxury real estate platform.\n"
                "Here are the recent conversation turns:\n"
            )
            for t in recent_turns:
                prompt_context += f"{t.get('role', 'user').title()}: {t.get('content', '')}\n"
            prompt_context += (
                f"\nUser's latest follow-up: \"{current_message}\"\n\n"
                "Rewrite the user's latest follow-up into a standalone, explicit search/action query.\n"
                "CRITICAL RULES:\n"
                "1. If the user mentions a specific property name, project, or development (e.g. '15 Sukhumvit Residences', 'Sea Heaven', 'Sun Hills', 'Botanica'), "
                "do NOT append, attach, or assume previous locations, cities, or budget constraints from past turns! Keep the property inquiry clean and focused on that specific property.\n"
                "2. ONLY incorporate active location (Phuket, Samui, Bang Tao, etc.), property type, or budget constraints if the user's follow-up is anaphoric, incremental, or relative "
                "(e.g. 'under 10m', 'with private pool', 'show me more options', 'any 2-bedroom ones?').\n"
                "3. If the user asks about availability in another country or location (e.g. 'anything in india ?', 'do you have villas in dubai ?'), keep that exact location inquiry intact.\n"
                "4. If the user's message is a greeting or pleasantry, return it verbatim without rewriting.\n"
                "5. If it's already complete and self-contained, return it verbatim. Output ONLY the rewritten sentence without explanation."
            )
            resp = _safe_openai_create(
                client,
                model=model,
                messages=[{"role": "user", "content": prompt_context}],
                temperature=0.0,
                max_tokens=150
            )
            rewritten = resp.choices[0].message.content.strip().strip('"')
            if rewritten and len(rewritten) > 3:
                logger.info(f"Public query rewritten: '{current_message}' -> '{rewritten}'")
                return rewritten
        except Exception as e:
            logger.warning(f"OpenAI query rewrite error: {e}")

    return current_message


class PublicAgentService:
    def __init__(self):
        self.model = settings.OPENAI_MODEL
        self.temperature = settings.OPENAI_TEMPERATURE
        self.api_key = settings.OPENAI_API_KEY
        self.timeout = getattr(settings, "OPENAI_TIMEOUT", 600.0)
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key, timeout=self.timeout)
        else:
            self.client = None

    def execute_tool(
        self,
        db: Session,
        conversation: Conversation,
        tool_name: str,
        args: Dict[str, Any]
    ) -> (Dict[str, Any], List[Dict[str, Any]], bool):
        """Safely executes structured tool calls. Returns (tool_result_dict, cards_list, lead_captured_bool)."""
        matched_cards = []
        lead_captured = False

        if tool_name == "search_properties":
            # 1. Direct query / project name check (e.g. '15 Sukhumvit Residences', 'Sea Heaven')
            query_term = args.get("query") or args.get("property_name") or args.get("project_name")
            if query_term:
                from app.services.firestore_service import get_property_by_id_from_firestore
                fs_single = get_property_by_id_from_firestore(str(query_term))
                if fs_single:
                    matched_cards = [fs_single]
                    return {
                        "total_matches": 1,
                        "properties": [
                            {
                                "property_id": fs_single["property_id"],
                                "name": fs_single["name"],
                                "type": fs_single["property_type"],
                                "price": fs_single["price"],
                                "bedrooms": fs_single["bedrooms"],
                                "area": fs_single["area"],
                                "location": fs_single["location"],
                                "status": fs_single["status"]
                            }
                        ]
                    }, matched_cards, False

            # Combine stored preferences with current parameters if not overridden
            stored_prefs = dict(conversation.preferences or {})
            
            # If searching for a specific project/development, do NOT force old stored location!
            loc = args.get("location")
            if not loc and not query_term:
                loc = stored_prefs.get("location")

            p_type = args.get("property_type") or stored_prefs.get("property_type")
            beds = args.get("bedrooms") or stored_prefs.get("bedrooms")
            
            min_p = None
            if "min_price" in args and args["min_price"] is not None:
                min_p = Decimal(str(args["min_price"]))
            elif "min_price" in stored_prefs and stored_prefs["min_price"] is not None:
                min_p = Decimal(str(stored_prefs["min_price"]))

            max_p = None
            if "max_price" in args and args["max_price"] is not None:
                max_p = Decimal(str(args["max_price"]))
            elif "max_price" in stored_prefs and stored_prefs["max_price"] is not None:
                max_p = Decimal(str(stored_prefs["max_price"]))

            params = PropertyFilterParams(
                location=loc,
                province=args.get("province"),
                district=args.get("district"),
                property_type=p_type,
                min_price=min_p,
                max_price=max_p,
                bedrooms=beds,
                bathrooms=args.get("bathrooms"),
                min_area=Decimal(str(args["min_area"])) if args.get("min_area") else None,
                max_area=Decimal(str(args["max_area"])) if args.get("max_area") else None,
                has_pool=args.get("has_pool"),
                beach_access=args.get("beach_access"),
                query=query_term,
                status=args.get("status"),
                limit=6
            )
            seen_ids = get_session_seen_property_ids(conversation)
            # Do not exclude seen IDs on a fresh tool search — that caused unrelated new cards
            matched_cards = search_properties_unified(db, params, limit=6, exclude_property_ids=None)
            if not matched_cards and not query_term:
                has_strict_criteria = bool(args.get("bedrooms") or args.get("min_price") or args.get("max_price"))
                if not has_strict_criteria:
                    fallback_params = PropertyFilterParams(
                        property_type=p_type,
                        limit=4
                    )
                    matched_cards = search_properties_unified(db, fallback_params, limit=4, exclude_property_ids=None)
                    if not matched_cards:
                        matched_cards = search_properties_unified(db, PropertyFilterParams(limit=4), limit=4)

            # Save explicitly stated new preferences
            new_prefs = {
                "seen_property_ids": list(set(seen_ids + [c["property_id"] for c in matched_cards]))
            }
            if args.get("location"):
                new_prefs["location"] = args["location"]
            if args.get("property_type"):
                new_prefs["property_type"] = args["property_type"]
            if args.get("bedrooms"):
                new_prefs["bedrooms"] = args["bedrooms"]
            if min_p is not None:
                new_prefs["min_price"] = float(min_p)
            if max_p is not None:
                new_prefs["max_price"] = float(max_p)

            if new_prefs:
                stored = dict(conversation.preferences or {})
                stored.update(new_prefs)
                conversation.preferences = stored
                record_or_update_lead(
                    db=db,
                    conversation=conversation,
                    preferred_location=new_prefs.get("location"),
                    preferred_type=new_prefs.get("property_type"),
                    budget_min=min_p,
                    budget_max=max_p,
                    buying_signal=f"Searched properties with filters: {args}"
                )

            tool_summary = {
                "total_matches": len(matched_cards),
                "properties": [
                    {
                        "property_id": c["property_id"],
                        "name": c["name"],
                        "type": c["property_type"],
                        "price": c["price"],
                        "bedrooms": c["bedrooms"],
                        "area": c["area"],
                        "location": c["location"],
                        "status": c["status"]
                    }
                    for c in matched_cards
                ]
            }
            return tool_summary, matched_cards, False

        elif tool_name == "get_property_by_id":
            pid = args.get("property_id") or args.get("property_name")
            
            # 1. Primary Datastore: Cloud Firestore
            if getattr(settings, "ENABLE_FIRESTORE_SYNC", True):
                from app.services.firestore_service import get_property_by_id_from_firestore
                fs_prop = None
                for candidate in [pid, args.get("property_name"), args.get("property_id")]:
                    if candidate:
                        fs_prop = get_property_by_id_from_firestore(str(candidate))
                        if fs_prop:
                            break
                if fs_prop:
                    matched_cards = [fs_prop]
                    return {
                        "found": True,
                        "property_id": fs_prop.get("property_id"),
                        "name": fs_prop.get("name"),
                        "price": fs_prop.get("price"),
                        "bedrooms": fs_prop.get("bedrooms"),
                        "area": fs_prop.get("area"),
                        "location": fs_prop.get("location"),
                        "status": fs_prop.get("status"),
                        "description": fs_prop.get("description") or fs_prop.get("name"),
                        "ownership": "Freehold / Leasehold",
                        "amenities": fs_prop.get("highlights", []),
                        "investment_info": f"Expected ROI: {fs_prop.get('expected_roi') or 'N/A'}",
                        "detail_page_url": fs_prop.get("detail_page_url")
                    }, matched_cards, False

            # 2. Fallback: Local SQL Database
            prop = get_property_by_id(db, pid) if (db and pid) else None
            if not prop and args.get("property_name") and db:
                prop = get_property_by_name(db, args["property_name"])

            if prop:
                card = format_property_card(prop)
                matched_cards = [card]
                record_or_update_lead(
                    db=db,
                    conversation=conversation,
                    buying_signal=f"Requested details for {prop.property_id} ({prop.property_name})"
                )
                return {
                    "found": True,
                    "property_id": prop.property_id,
                    "name": prop.property_name,
                    "price": prop.price_display,
                    "bedrooms": prop.bedrooms_display,
                    "area": prop.area_display,
                    "status": prop.status,
                    "description": prop.description,
                    "ownership": prop.ownership_type,
                    "amenities": prop.amenities,
                    "investment_info": prop.investment_information
                }, matched_cards, False
            else:
                target_name = args.get("property_name") or pid or "requested property"
                return {
                    "found": False,
                    "message": f"No property found in database with ID or name '{target_name}'."
                }, [], False

        elif tool_name == "generate_property_brochure":
            pid = args.get("property_id")
            prop = get_property_by_id(db, pid) if pid else None
            if not prop and pid:
                prop = get_property_by_name(db, pid)
            if not prop and args.get("property_name"):
                prop = get_property_by_name(db, args["property_name"])

            if not prop:
                stored_prefs = dict(conversation.preferences or {})
                last_ids = stored_prefs.get("last_result_ids", [])
                if len(last_ids) == 1:
                    prop = get_property_by_id(db, last_ids[0])

            if prop:
                from app.core.tenant import resolve_tenant
                tenant = resolve_tenant(conversation.site_origin)
                card = format_property_card(prop)
                record_or_update_lead(
                    db=db,
                    conversation=conversation,
                    preferred_location=f"{prop.district}, {prop.province}" if prop.district else prop.province,
                    preferred_type=prop.property_type,
                    buying_signal=f"Requested brochure for {prop.property_id} ({prop.property_name})",
                    notes=f"Brochure requested for {prop.property_id} on {tenant.brand_name}"
                )
                stored_prefs = dict(conversation.preferences or {})
                requested = list(stored_prefs.get("brochures_requested") or [])
                if prop.property_id not in requested:
                    requested.append(prop.property_id)
                stored_prefs["brochures_requested"] = requested
                conversation.preferences = stored_prefs
                if hasattr(conversation, "__table__"):
                    try:
                        db.commit()
                    except Exception:
                        try:
                            db.rollback()
                        except Exception:
                            pass

                from app.services.brochure_service import generate_and_upload_brochure
                client_n = (conversation.preferences or {}).get("contact_name") if conversation else None
                try:
                    b_res = generate_and_upload_brochure(
                        db=db,
                        property_id=prop.property_id,
                        site_origin=conversation.site_origin if conversation else None,
                        client_name=client_n
                    )
                    b_url = b_res.get("file_url") or b_res.get("download_url")
                except Exception as ex:
                    logger.warning(f"Could not upload brochure to S3 in execute_tool: {ex}")
                    b_url = f"/api/v1/chat/brochure/{prop.property_id}"

                return {
                    "status": "ready",
                    "brochure_property_id": prop.property_id,
                    "property_name": prop.property_name,
                    "download_url": b_url,
                    "pdf_url": b_url,
                    "message": f"Official luxury brochure for {prop.property_name} ({prop.property_id}) generated. Link: {b_url}"
                }, [card], True
            else:
                return {
                    "status": "needs_clarification",
                    "message": f"Could not locate property '{pid}'. Please specify a valid property name or ID."
                }, [], False

        elif tool_name == "capture_lead":
            b_val = Decimal(str(args["budget"])) if args.get("budget") else None
            name = args.get("name")
            email = args.get("email")
            phone = args.get("phone")
            record_or_update_lead(
                db=db,
                conversation=conversation,
                contact_name=name,
                contact_email=email,
                contact_phone=phone,
                preferred_location=args.get("preferred_location"),
                budget_max=b_val,
                notes=args.get("notes"),
                buying_signal=f"Direct customer lead capture: {email or phone or name}"
            )
            details = []
            if name: details.append(f"Name: {name}")
            if email: details.append(f"Email: {email}")
            if phone: details.append(f"Phone: {phone}")
            contact_str = f" ({', '.join(details)})" if details else ""
            return {
                "status": "success",
                "message": f"Thank you! I have recorded your contact details{contact_str}. A senior property advisor will reach out shortly to assist with your search and confirm your viewing schedule."
            }, [], True

        elif tool_name == "schedule_viewing":
            pid = args.get("property_id")
            prop = get_property_by_id(db, pid) if pid else None
            p_name = prop.property_name if prop else (pid or "Thailand Property")
            
            p_date = args.get("preferred_date", "2026-10-01")
            p_time = args.get("preferred_time", "14:00")
            start_iso = f"{p_date}T{p_time}:00Z"
            
            cal_res = google_manager.create_calendar_event(
                title=f"Viewing: {p_name}",
                start_time=start_iso,
                attendees=[args.get("customer_email")] if args.get("customer_email") else [],
                description=f"Property viewing appointment for {p_name} ({pid}). Notes: {args.get('notes', 'None')}",
                related_property=p_name,
                location=prop.district if prop else "Thailand"
            )
            
            record_or_update_lead(
                db=db,
                conversation=conversation,
                contact_name=args.get("customer_name"),
                contact_email=args.get("customer_email"),
                contact_phone=args.get("customer_phone"),
                notes=f"Viewing scheduled for {p_name} on {p_date} at {p_time}. {args.get('notes', '')}",
                buying_signal=f"Booked viewing for {pid} on {p_date}"
            )
            
            if args.get("customer_email"):
                google_manager.send_email(
                    to=args["customer_email"],
                    subject=f"Tajlandia.com Viewing Confirmation: {p_name}",
                    body=f"Dear {args.get('customer_name') or 'Valued Client'},\n\nYour viewing appointment for {p_name} has been confirmed for {p_date} at {p_time}.\n\nAdd to your Google Calendar: {cal_res.get('google_calendar_url')}\n\nOur local property consultant will greet you.\n\nWarm regards,\nTajlandia.com Luxury Real Estate Team"
                )
            
            matched_cards = [format_property_card(prop)] if prop else []
            return {
                "status": "success",
                "appointment_confirmed": True,
                "property": p_name,
                "date": p_date,
                "time": p_time,
                "google_calendar_url": cal_res.get("google_calendar_url"),
                "event_id": cal_res.get("event_id"),
                "message": f"Viewing confirmed for {p_name} on {p_date} at {p_time}! You can add it to your Google Calendar with one click."
            }, matched_cards, True

        elif tool_name == "send_property_brochure":
            pid = args.get("property_id")
            prop = get_property_by_id(db, pid) if pid else None
            p_name = prop.property_name if prop else (pid or "Thailand Property")
            to_email = args.get("recipient_email")
            
            email_body = (
                f"Hello {args.get('customer_name') or 'Valued Client'},\n\n"
                f"Thank you for your interest in {p_name} ({pid}).\n\n"
                f"Location: {prop.district or ''}, {prop.province or 'Thailand' if prop else 'Thailand'}\n"
                f"Price: {prop.price_display if prop else 'On Request'}\n"
                f"Bedrooms: {prop.bedrooms_display if prop else 'N/A'}\n"
                f"Area: {prop.area_display if prop else 'N/A'}\n\n"
                f"View verified listing & photos online: {prop.detail_page_url if prop else settings.BASE_WEBSITE_URL}\n\n"
                f"Warm regards,\n{settings.BRAND_NAME} Client Advisory Team"
            )
            email_res = google_manager.send_email(
                to=to_email,
                subject=f"{settings.BRAND_NAME} Property Brochure: {p_name}",
                body=email_body
            )
            record_or_update_lead(
                db=db,
                conversation=conversation,
                contact_name=args.get("customer_name"),
                contact_email=to_email,
                buying_signal=f"Requested brochure for {pid}"
            )
            matched_cards = [format_property_card(prop)] if prop else []
            return {
                "status": "success",
                "sent_to": to_email,
                "property": p_name,
                "message_id": email_res.get("message_id"),
                "message": f"Property brochure and investment specifications for {p_name} have been emailed to {to_email} via Gmail."
            }, matched_cards, True

        elif tool_name == "get_property_location":
            pid = args.get("property_id")
            prop = get_property_by_id(db, pid) if pid else None
            if not prop and args.get("location_name"):
                prop = get_property_by_name(db, args["location_name"])
            
            if prop:
                card = format_property_card(prop)
                return {
                    "property_id": prop.property_id,
                    "name": prop.property_name,
                    "location": card["location"],
                    "latitude": prop.latitude,
                    "longitude": prop.longitude,
                    "maps_url": card["maps_url"],
                    "maps_embed_url": card["maps_embed_url"],
                    "nearby_landmarks": card["nearby_landmarks"]
                }, [card], False
            else:
                return {
                    "found": False,
                    "message": f"Could not find exact location data for property '{pid}'."
                }, [], False

        return {"error": f"Unknown tool: {tool_name}"}, [], False

    def _post_process_response(self, resp: Dict[str, Any], user_message: Optional[str] = None) -> Dict[str, Any]:
        """Ensures all public chatbot text is checked for legal safety/anti-pressure, cleaned of long dashes, and formatted professionally."""
        if not isinstance(resp, dict):
            return resp
        if "text" in resp and isinstance(resp["text"], str):
            lang = resp.get("language", "en")
            if user_message:
                resp["text"] = ThaiPropertyLegalGuardrail.sanitize_response(
                    user_message=user_message,
                    response_text=resp["text"],
                    language=lang
                )
            resp["text"] = _professionalize_chat_text(clean_response_text(resp["text"]))
        if "preferences" in resp and isinstance(resp["preferences"], dict):
            focus = resp["preferences"].get("focus_summary")
            if focus and isinstance(focus, str):
                resp["preferences"]["focus_summary"] = clean_response_text(focus)
            fetched = resp["preferences"].get("what_we_fetched")
            if fetched and isinstance(fetched, list):
                resp["preferences"]["what_we_fetched"] = [clean_response_text(item) for item in fetched]
        if "properties" in resp and isinstance(resp["properties"], list):
            for p in resp["properties"]:
                if isinstance(p, dict):
                    if p.get("bedrooms") and isinstance(p["bedrooms"], str):
                        p["bedrooms"] = clean_response_text(p["bedrooms"])
                    if p.get("area") and isinstance(p["area"], str):
                        p["area"] = clean_response_text(p["area"])
                    if p.get("price") and isinstance(p["price"], str):
                        p["price"] = clean_response_text(p["price"])
        return resp

    def run_conversation(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        requested_language: Optional[str] = None
    ) -> Dict[str, Any]:
        """Runs one conversational turn using deterministic intent routing and token-efficient synthesis."""
        res = self._run_conversation_internal(
            db=db,
            conversation=conversation,
            user_message=user_message,
            requested_language=requested_language
        )
        return self._post_process_response(res, user_message=user_message)

    def _run_conversation_internal(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        requested_language: Optional[str] = None
    ) -> Dict[str, Any]:
        start_time = time.time()

        # 0. Resolve active turn language ('en' | 'pl')
        active_lang, is_switch = resolve_language(
            requested_language=requested_language,
            user_message=user_message,
            stored_preferences=conversation.preferences or {}
        )
        prefs = dict(conversation.preferences or {})
        prefs["language"] = active_lang
        conversation.preferences = prefs
        if hasattr(conversation, "__table__"):
            try:
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        if is_switch:
            reply_text = get_language_switch_confirmation(active_lang, is_admin=False)
            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=user_message,
                assistant_message=reply_text
            )
            token_tracker.log_turn(
                TurnTokenMetrics(
                    request_id=f"req_{uuid.uuid4().hex[:8]}",
                    session_id=conversation.session_id,
                    conversation_id=str(conversation.id),
                    intent="language_switch",
                    model=self.model,
                    latency_sec=round(time.time() - start_time, 3)
                ),
                db=db
            )
            return {
                "type": "text",
                "text": reply_text,
                "properties": [],
                "quick_replies": self._generate_quick_replies(user_message, [], language=active_lang),
                "lead_captured": False,
                "preferences": conversation.preferences or {},
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": active_lang
            }

        # 1. Greeting and pleasantry handling (Fast path - never triggers search or off-topic)
        if is_greeting_or_chit_chat(user_message):
            res = self._handle_greeting(db, conversation, user_message, language=active_lang)
            token_tracker.log_turn(
                TurnTokenMetrics(
                    request_id=f"req_{uuid.uuid4().hex[:8]}",
                    session_id=conversation.session_id,
                    conversation_id=str(conversation.id),
                    intent="greeting",
                    model=self.model,
                    latency_sec=round(time.time() - start_time, 3)
                ),
                db=db
            )
            return res

        # 2. Load persistent property context (DB-backed via preferences)
        pctx = load_property_context(conversation)
        pctx = clear_active_if_new_conversation(pctx, str(getattr(conversation, "id", "") or ""))
        save_property_context(conversation, pctx)

        # 3. Deterministic intent routing (0-token fast path for real estate commands)
        intent, parsed_args = detect_deterministic_intent(
            message=user_message,
            stored_state=conversation.preferences or {}
        )

        # Resolve follow-up references via persistent context when intent is weak/general
        resolved = resolve_property_reference(user_message, pctx, language=active_lang)
        if resolved.ambiguous:
            clarify = resolved.clarification or (
                "Which property are you referring to? Please name it or say 'the first/second property'."
            )
            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=user_message,
                assistant_message=clarify,
                new_preferences=save_property_context(conversation, pctx),
            )
            return {
                "type": "text",
                "text": clarify,
                "properties": [],
                "quick_replies": self._generate_quick_replies(user_message, [], language=active_lang),
                "lead_captured": False,
                "preferences": conversation.preferences or {},
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": active_lang,
            }

        if intent in (None, IntentType.GENERAL_QUESTION) or intent == IntentType.GENERAL_QUESTION:
            if resolved.property_id and is_property_followup_message(user_message, pctx):
                intent = IntentType.PROPERTY_DETAILS
                parsed_args = {
                    "property_id": resolved.property_id,
                    "focus_question": user_message,
                    "from_property_context": True,
                }
            elif resolved.property_id and not parsed_args.get("property_id"):
                # Bare pronoun / fact follow-up with active context
                if resolved.used_active_context or resolved.source in ("active", "ordinal", "name"):
                    intent = IntentType.PROPERTY_DETAILS
                    parsed_args = {
                        "property_id": resolved.property_id,
                        "focus_question": user_message,
                        "from_property_context": True,
                    }

        # If details intent lacks id but context resolves one, inject it
        if intent == IntentType.PROPERTY_DETAILS and not parsed_args.get("property_id") and resolved.property_id:
            parsed_args["property_id"] = resolved.property_id
            parsed_args["from_property_context"] = True

        history_list = conversation.message_history or []
        is_off_topic = False
        # Known real-estate intents skip off-topic LLM (avoid blocking "tell me more about the first one")
        _re_intents = {
            IntentType.PROPERTY_SEARCH,
            IntentType.PROPERTY_DETAILS,
            IntentType.PROPERTY_COMPARISON,
            IntentType.SHOW_MORE,
            IntentType.REFINE_FILTER,
            IntentType.BROCHURE_REQUEST,
            IntentType.LEAD_CAPTURE,
            IntentType.GREETING,
        }
        # Property follow-ups with active context are never off-topic
        if is_property_followup_message(user_message, pctx) and (pctx.active_property_id or resolved.property_id):
            is_off_topic = False
        elif intent == IntentType.OFF_TOPIC:
            is_off_topic = True
        elif intent in _re_intents:
            is_off_topic = False
        else:
            is_off_topic = classify_off_topic_dynamic(
                client=self.client,
                model=self.model,
                message=user_message,
                history=history_list
            )

        if is_off_topic:
            reply_text = get_off_topic_text(active_lang, site_origin=conversation.site_origin, user_message=user_message)
            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=user_message,
                assistant_message=reply_text
            )
            token_tracker.log_turn(
                TurnTokenMetrics(
                    request_id=f"req_{uuid.uuid4().hex[:8]}",
                    session_id=conversation.session_id,
                    conversation_id=str(conversation.id),
                    intent="off_topic",
                    model=self.model,
                    latency_sec=round(time.time() - start_time, 3)
                ),
                db=db
            )
            safe_prefs = dict(conversation.preferences or {})
            safe_prefs.pop("focus_summary", None)
            safe_prefs.pop("what_we_fetched", None)
            # Keep off-topic soft: invite Thailand search, no brochure/viewing hard CTAs
            off_topic_replies = {
                "en": ["Show villas in Phuket", "Condo in Koh Samui", "Budget under 15M THB"],
                "pl": ["Wille w Phuket", "Apartament na Samui", "Budżet do 15 mln THB"],
                "th": ["วิลล่าภูเก็ต", "คอนโดสมุย", "งบไม่เกิน 15 ล้าน"],
            }
            return {
                "type": "text",
                "text": reply_text,
                "properties": [],
                "quick_replies": off_topic_replies.get(active_lang, off_topic_replies["en"]),
                "lead_captured": False,
                "preferences": safe_prefs,
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": active_lang
            }

        if intent == IntentType.LEAD_CAPTURE:
            return self._handle_lead_capture(db, conversation, parsed_args, user_message, start_time, language=active_lang)

        if intent == IntentType.SHOW_MORE:
            return self._handle_show_more(db, conversation, parsed_args, user_message, start_time, language=active_lang)

        if intent == IntentType.PROPERTY_DETAILS:
            return self._handle_property_details(db, conversation, parsed_args, user_message, start_time, language=active_lang)

        if intent == IntentType.PROPERTY_COMPARISON:
            return self._handle_property_comparison(db, conversation, parsed_args, user_message, start_time, language=active_lang)

        if intent == IntentType.BROCHURE_REQUEST:
            return self._handle_brochure_request(db, conversation, parsed_args, user_message, start_time, language=active_lang)

        if intent == IntentType.REFINE_FILTER:
            return self._handle_refine_filter(db, conversation, parsed_args, user_message, start_time, language=active_lang)

        if intent == IntentType.PROPERTY_SEARCH:
            return self._handle_property_search(db, conversation, user_message, start_time, language=active_lang)

        if intent == IntentType.GENERAL_QUESTION:
            return self._handle_general_question(db, conversation, user_message, start_time, language=active_lang)

        # 4. Contextual query rewriting using previous 2 turns via OpenAI (for ambiguous follow-ups)
        effective_message = rewrite_public_query(
            client=self.client,
            model=self.model,
            history=conversation.message_history or [],
            current_message=user_message
        )

        # 5. External tools fallback (lead capture, viewing scheduling, brochures, locations)
        if self.client:
            return self._call_openai(db, conversation, effective_message, raw_user_message=user_message, start_time=start_time, language=active_lang)
        else:
            return self._run_deterministic_retrieval(db, conversation, effective_message, raw_user_message=user_message, start_time=start_time, language=active_lang)

    def _handle_lead_capture(
        self,
        db: Session,
        conversation: Conversation,
        args: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """
        Records client lead contact details / private viewing booking and presents matching properties.
        """
        from app.core.tenant import resolve_tenant
        tenant = resolve_tenant(conversation.site_origin)

        c_name = args.get("name")
        c_email = args.get("email")
        c_phone = args.get("phone")
        pref_loc = args.get("location")
        budget = args.get("budget")
        budget_dec = Decimal(str(budget)) if budget else None
        is_viewing = args.get("is_viewing", False)

        lead = record_or_update_lead(
            db=db,
            conversation=conversation,
            contact_name=c_name,
            contact_email=c_email,
            contact_phone=c_phone,
            preferred_location=pref_loc,
            budget_max=budget_dec,
            buying_signal=f"{'Private viewing request' if is_viewing else 'Contact details submitted'}: {c_email or c_phone or c_name}",
            notes=f"Client requested viewing/contact: {user_message}"
        )

        # Search for luxury properties matching location & budget to present alongside confirmation
        params = PropertyFilterParams(
            location=pref_loc,
            property_type=args.get("property_type", "Villa"),
            max_price=budget_dec,
            offset=0,
            limit=4
        )
        cards = search_properties_unified(db, params, limit=4)
        if not cards and pref_loc:
            # Fallback to broader Phuket / Samui search with the budget
            is_phuket = any(k in (pref_loc or "").lower() for k in ["layan", "bang tao", "karon", "rawai", "kamala", "surin"])
            broad_loc = "Phuket" if is_phuket else None
            if broad_loc:
                params_broad = PropertyFilterParams(
                    location=broad_loc,
                    property_type=args.get("property_type", "Villa"),
                    max_price=budget_dec,
                    offset=0,
                    limit=4
                )
                cards = search_properties_unified(db, params_broad, limit=4)
        if not cards:
            fallback_params = PropertyFilterParams(offset=0, limit=3)
            cards = search_properties_unified(db, fallback_params, limit=3)

        # Localized confirmation
        if language == "th":
            opening = (
                f"ขอบคุณครับ{', คุณ' + c_name if c_name else ''}! "
                f"ระบบได้ลงทะเบียนการนัดหมายเข้าชมโครงการส่วนตัวสำหรับวิลล่าใน{pref_loc or 'ภูเก็ต'}เรียบร้อยแล้วครับ\n\n"
                f"ที่ปรึกษาอาวุโสของ {tenant.brand_name} ({tenant.advisor_email}) จะติดต่อกลับผ่านทาง "
                f"**{c_phone or c_email or 'ข้อมูลที่ระบุ'}** ภายใน 15-30 นาทีเพื่อยืนยันกำหนดการและอำนวยความสะดวกเรื่องการเดินทางครับ\n\n"
                f"ระหว่างนี้ นี่คือโครงการพูลวิลล่าระดับพรีเมียมที่ผ่านการตรวจสอบตรงตามงบประมาณของคุณ:"
            )
        elif language == "pl":
            opening = (
                f"Dziękujemy{', ' + c_name if c_name else ''}! "
                f"Twoje zgłoszenie na prywatną prezentację willi w rejonie **{pref_loc or 'Phuket'}** zostało pomyślnie zarejestrowane.\n\n"
                f"Dedykowany doradca {tenant.brand_name} ({tenant.advisor_email}) skontaktuje się z Tobą pod adresem "
                f"**{c_phone or c_email or 'Twoim kontaktem'}** w celu potwierdzenia dogodnego terminu wizyty i organizacji transportu.\n\n"
                f"Poniżej przygotowaliśmy wyselekcjonowane luksusowe wille spełniające Twoje kryteria i budżet:"
            )
        else:
            opening = (
                f"Thank you{', ' + c_name if c_name else ''}! "
                f"Your request for a private property viewing in **{pref_loc or 'Phuket'}** has been successfully registered.\n\n"
                f"A senior advisor from {tenant.brand_name} ({tenant.advisor_email}) will contact you directly via "
                f"**{c_phone or c_email or 'your contact details'}** to confirm your schedule, arrange private VIP transport, and prepare property access.\n\n"
                f"In the meantime, here are verified luxury properties matching your criteria and budget:"
            )

        stored_prefs = dict(conversation.preferences or {})
        if cards:
            stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
        stored_prefs["location"] = pref_loc or stored_prefs.get("location")
        if budget_dec:
            stored_prefs["budget_max"] = float(budget_dec)
        conversation.preferences = stored_prefs
        if hasattr(conversation, "__table__"):
            try:
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=opening,
            structured_data={"properties": cards, "lead_id": str(lead.id)}
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="lead_capture",
                model="deterministic",
                latency_sec=round(time.time() - start_time, 3)
            ),
            db=db
        )

        return {
            "type": "property_cards" if cards else "text",
            "text": opening,
            "properties": cards,
            "quick_replies": self._generate_quick_replies(user_message, cards, language=language),
            "lead_captured": True,
            "lead_id": str(lead.id),
            "preferences": stored_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _handle_show_more(
        self,
        db: Session,
        conversation: Conversation,
        args: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Handles pagination and 'show more' requests deterministically, strictly excluding any properties already shown in this chat."""
        stored_prefs = dict(conversation.preferences or {})
        seen_ids = get_session_seen_property_ids(conversation)
        cache_hit = False

        active_filters = dict(stored_prefs.get("active_filters", {}))
        loc = active_filters.get("location") or stored_prefs.get("location")
        p_type = active_filters.get("property_type") or stored_prefs.get("property_type")
        bedrooms = active_filters.get("bedrooms") or stored_prefs.get("bedrooms_num")
        max_p = Decimal(str(active_filters["max_price"])) if active_filters.get("max_price") else (Decimal(str(stored_prefs["budget_max"])) if stored_prefs.get("budget_max") else None)
        has_pool = active_filters.get("has_pool", stored_prefs.get("has_pool"))

        params = PropertyFilterParams(
            location=loc,
            property_type=p_type,
            bedrooms=bedrooms,
            max_price=max_p,
            has_pool=has_pool,
            limit=4
        )
        cards = search_properties_unified(db, params, limit=4, exclude_property_ids=seen_ids)

        # If 0 new cards found for the narrow location, broaden to wider province / Thailand without shown properties
        if not cards and loc in ["Bang Tao", "Choeng Thale", "Layan", "Kamala", "Surin", "Rawai", "Karon", "Patong"]:
            broad_params = PropertyFilterParams(
                location="Phuket",
                property_type=p_type,
                bedrooms=bedrooms,
                max_price=max_p,
                has_pool=has_pool,
                limit=4
            )
            cards = search_properties_unified(db, broad_params, limit=4, exclude_property_ids=seen_ids)

        if not cards:
            # Fallback across Thailand luxury properties excluding already shown
            fallback_params = PropertyFilterParams(
                property_type=p_type,
                bedrooms=bedrooms,
                max_price=max_p,
                has_pool=has_pool,
                limit=4
            )
            cards = search_properties_unified(db, fallback_params, limit=4, exclude_property_ids=seen_ids)

        if not cards:
            # Proactively recommend other verified luxury properties across Thailand
            general_params = PropertyFilterParams(limit=4)
            cards = search_properties_unified(db, general_params, limit=4, exclude_property_ids=seen_ids)

        if cards:
            reply_text = build_rich_property_recommendation_text(
                cards=cards,
                language=language,
                is_alternative=False,
                is_sparse=False,
                searched_location=loc,
                searched_bedrooms=bedrooms,
                searched_budget=float(max_p) if max_p else None,
                site_origin=conversation.site_origin
            )

            # Enforce clean structured cards formatting
            reply_text = ensure_structured_response(reply_text, cards=cards, user_message=user_message, language=language)

            # Update seen_property_ids in stored preferences
            new_seen = list(set(seen_ids + [c["property_id"] for c in cards]))
            stored_prefs["seen_property_ids"] = new_seen
            stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
        else:
            if language == "th":
                reply_text = (
                    "จากความต้องการของคุณ เราได้นำเสนอรายการอสังหาริมทรัพย์ที่ผ่านการตรวจสอบทั้งหมดในพอร์ตโฟลิโอปัจจุบันเรียบร้อยแล้วครับ\n\n"
                    f"คุณต้องการปรับเปลี่ยนเงื่อนไขการค้นหา หรือสนใจให้ทีมที่ปรึกษาของเรา ({settings.ADVISOR_EMAIL}) ช่วยจัดหาโครงการนอกตลาด (Off-Market) เพิ่มเติมไหมครับ?"
                )
            elif language == "pl":
                reply_text = (
                    "W oparciu o Twoje preferencje zaprezentowaliśmy wszystkie aktualnie dostępne zweryfikowane nieruchomości z naszego portfolio.\n\n"
                    f"Czy chcesz dostosować kryteria wyszukiwania lub skontaktować się z naszym doradcą ({settings.ADVISOR_EMAIL}) w sprawie nowych ofert off-market?"
                )
            else:
                reply_text = (
                    "Based on your preferences, we have presented all currently available verified listings from our active portfolio.\n\n"
                    f"Would you like to adjust your search criteria, or connect with our advisory team at {settings.ADVISOR_EMAIL} to explore upcoming off-market opportunities?"
                )

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text,
            structured_data={"properties": cards, "property_ids": [c["property_id"] for c in cards]},
            new_preferences=stored_prefs
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="show_more",
                model="deterministic",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                llm_calls_count=0,
                latency_sec=round(time.time() - start_time, 3),
                cache_hit=cache_hit
            ),
            db=db
        )

        return {
            "type": "property_cards" if cards else "text",
            "text": reply_text,
            "properties": cards,
            "quick_replies": self._generate_quick_replies(user_message, cards, language=language),
            "lead_captured": False,
            "preferences": stored_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _handle_property_details(
        self,
        db: Session,
        conversation: Conversation,
        args: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Resolve property reference, then fetch CURRENT authorized facts (never chat memory)."""
        pctx = load_property_context(conversation)

        # Prefer explicit args, else resolve from persistent context
        pid = args.get("property_id") or args.get("property_name")
        if not pid or args.get("from_property_context"):
            resolved = resolve_property_reference(user_message, pctx, language=language)
            if resolved.ambiguous:
                clarify = resolved.clarification or "Which property do you mean?"
                pctx = update_rolling_summary(pctx, user_message, clarify, None)
                prefs = save_property_context(conversation, pctx)
                append_conversation_turn(
                    db=db, conversation=conversation,
                    user_message=user_message, assistant_message=clarify, new_preferences=prefs
                )
                return {
                    "type": "text", "text": clarify, "properties": [],
                    "quick_replies": self._generate_quick_replies(user_message, [], language=language),
                    "lead_captured": False, "preferences": prefs,
                    "session_id": conversation.session_id,
                    "conversation_id": str(conversation.id), "language": language,
                }
            if resolved.property_id:
                pid = resolved.property_id
            elif pctx.active_property_id and args.get("from_property_context"):
                pid = pctx.active_property_id

        pname = args.get("property_name") or pid
        card = None
        p_name = None
        p_id = None
        p_loc = None
        p_price = None
        p_beds = None
        p_area = None
        p_status = None
        p_owner = None
        p_high = []
        p_desc = ""

        # Live authorized fetch only — never use prior assistant text as factual source
        _rec, live_card, _src = fetch_authorized_property(db, str(pid) if pid else "")
        if live_card and isinstance(live_card, dict):
            card = live_card
            p_name = live_card.get("name") or live_card.get("property_name")
            p_id = live_card.get("property_id") or pid
            p_loc = live_card.get("location")
            p_price = live_card.get("price") or live_card.get("price_display")
            p_beds = live_card.get("bedrooms") or live_card.get("bedrooms_display")
            p_area = live_card.get("area") or live_card.get("area_display")
            p_status = live_card.get("status") or "Available"
            p_owner = live_card.get("ownership") or live_card.get("ownership_type") or "Foreign Freehold / Leasehold available"
            p_high = live_card.get("highlights") or []
            p_desc = live_card.get("description") or ""
        elif _rec is not None and not isinstance(_rec, dict):
            card = format_property_card(_rec)
            p_name = getattr(_rec, "property_name", None)
            p_id = getattr(_rec, "property_id", pid)
            p_loc = card.get("location")
            p_price = getattr(_rec, "price_display", None) or "Price on request"
            p_beds = getattr(_rec, "bedrooms_display", None) or "N/A"
            p_area = getattr(_rec, "area_display", None) or "N/A"
            p_status = getattr(_rec, "status", None) or "Available"
            p_owner = getattr(_rec, "ownership_type", None) or "Foreign Freehold / Leasehold available"
            p_high = getattr(_rec, "highlights", None) or []
            p_desc = getattr(_rec, "description", None) or ""
        else:
            # Legacy dual-path fallback (ID then name)
            if getattr(settings, "ENABLE_FIRESTORE_SYNC", True) and (pid or pname):
                from app.services.firestore_service import get_property_by_id_from_firestore
                fs_prop = None
                for candidate in [pid, pname]:
                    if not candidate:
                        continue
                    fs_prop = get_property_by_id_from_firestore(str(candidate))
                    if fs_prop:
                        break
                if fs_prop:
                    card = fs_prop
                    p_name = fs_prop.get("name")
                    p_id = fs_prop.get("property_id")
                    p_loc = fs_prop.get("location")
                    p_price = fs_prop.get("price")
                    p_beds = fs_prop.get("bedrooms")
                    p_area = fs_prop.get("area")
                    p_status = fs_prop.get("status") or "Available"
                    p_owner = "Foreign Freehold / Leasehold available"
                    p_high = fs_prop.get("highlights") or []
                    p_desc = fs_prop.get("description") or ""
            if not card:
                prop = get_property_by_id(db, pid) if (db and pid) else None
                if not prop and args.get("property_name") and db:
                    prop = get_property_by_name(db, args["property_name"])
                if prop:
                    card = format_property_card(prop)
                    p_name = prop.property_name
                    p_id = prop.property_id
                    p_loc = card["location"]
                    p_price = prop.price_display or "Price on request"
                    p_beds = prop.bedrooms_display or "N/A"
                    p_area = prop.area_display or "N/A"
                    p_status = prop.status or "Available"
                    p_owner = prop.ownership_type or "Foreign Freehold / Leasehold available"
                    p_high = prop.highlights or []
                    p_desc = prop.description or ""

        if card:
            focus_q = (args.get("focus_question") or user_message or "").lower()
            has_pool = bool(card.get("has_pool")) if isinstance(card, dict) else False
            beach = bool(card.get("beach_access")) if isinstance(card, dict) else False

            # Focused amenity/price answers from verified fields only — never invent
            if any(k in focus_q for k in ["pool", "swimming", "basen", "สระ"]):
                if language == "th":
                    reply_text = (
                        f"**{p_name}**: {'มีสระว่ายน้ำส่วนตัวในข้อมูลที่ตรวจสอบแล้วครับ' if has_pool else 'ในฐานข้อมูลที่ตรวจสอบแล้วยังไม่มีข้อมูลว่ามีสระว่ายน้ำส่วนตัว'}\n\n"
                        f"• **ราคา:** {p_price}\n• **ทำเล:** {p_loc}\n• **ห้องนอน:** {p_beds}"
                    )
                elif language == "pl":
                    reply_text = (
                        f"**{p_name}**: {'Tak — według zweryfikowanych danych posiada prywatny basen.' if has_pool else 'W zweryfikowanej bazie nie ma potwierdzenia prywatnego basenu.'}\n\n"
                        f"• **Cena:** {p_price}\n• **Lokalizacja:** {p_loc}\n• **Sypialnie:** {p_beds}"
                    )
                else:
                    reply_text = (
                        f"**{p_name}**: {'Yes — verified data confirms a private pool.' if has_pool else 'Verified data does not confirm a private pool for this listing.'}\n\n"
                        f"• **Price:** {p_price}\n• **Location:** {p_loc}\n• **Bedrooms:** {p_beds}"
                    )
            elif any(k in focus_q for k in ["parking", "garage", "ที่จอด", "garaż", "garaz"]):
                # Only confirm parking when the authorized record documents it — never invent
                parking_val = None
                if isinstance(card, dict):
                    for k in ("has_parking", "parking", "parking_spaces", "amenities"):
                        if k in card and card.get(k) not in (None, "", []):
                            parking_val = card.get(k)
                            break
                if parking_val is True or (isinstance(parking_val, (int, float)) and parking_val > 0):
                    reply_text = f"**{p_name}**: Verified data confirms parking ({parking_val})."
                elif isinstance(parking_val, str) and parking_val.strip():
                    reply_text = f"**{p_name}**: Verified parking info: {parking_val}."
                elif isinstance(parking_val, list) and any("park" in str(x).lower() for x in parking_val):
                    reply_text = f"**{p_name}**: Parking is listed among verified amenities."
                else:
                    reply_text = (
                        f"**{p_name}**: Parking is not documented in the verified database.\n\n"
                        f"• **Price:** {p_price}\n• **Location:** {p_loc}"
                    )
            elif any(k in focus_q for k in ["price", "cost", "how much", "cena", "ราคา"]):
                if language == "th":
                    reply_text = f"**{p_name}** มีราคาในฐานข้อมูลที่ตรวจสอบแล้ว: **{p_price}**\n\n• **ทำเล:** {p_loc}\n• **ห้องนอน / พื้นที่:** {p_beds} | {p_area}"
                elif language == "pl":
                    reply_text = f"**{p_name}** — zweryfikowana cena: **{p_price}**\n\n• **Lokalizacja:** {p_loc}\n• **Sypialnie / Powierzchnia:** {p_beds} | {p_area}"
                else:
                    reply_text = f"**{p_name}** — verified price: **{p_price}**\n\n• **Location:** {p_loc}\n• **Bedrooms / Area:** {p_beds} | {p_area}"
            elif language == "th":
                reply_text = (
                    f"ข้อมูลจำเพาะที่ผ่านการตรวจสอบสำหรับ **{p_name}** ({p_id}):\n\n"
                    f"• **ทำเล:** {p_loc}\n"
                    f"• **ราคา:** {p_price}\n"
                    f"• **ห้องนอน / พื้นที่ใช้สอย:** {p_beds} | {p_area}\n"
                    f"• **สถานะ:** {p_status}\n"
                    f"• **สระว่ายน้ำ:** {'มี' if has_pool else 'ไม่ระบุในฐานข้อมูล'}\n"
                    f"• **ใกล้ชายหาด:** {'มี' if beach else 'ไม่ระบุในฐานข้อมูล'}\n"
                    f"• **รูปแบบกรรมสิทธิ์:** {p_owner}\n"
                    f"• **จุดเด่น:** {', '.join(p_high) if p_high else 'Not documented in verified database'}\n\n"
                    f"{p_desc}\n\n"
                    f"คุณต้องการดูผังอาคาร (Floor Plan) เพิ่มเติม หรือต้องการให้นัดหมายเข้าชมโครงการไหมครับ?"
                )
            elif language == "pl":
                reply_text = (
                    f"Oto zweryfikowane parametry dla **{p_name}** ({p_id}):\n\n"
                    f"• **Lokalizacja:** {p_loc}\n"
                    f"• **Cena:** {p_price}\n"
                    f"• **Sypialnie / Powierzchnia:** {p_beds} | {p_area}\n"
                    f"• **Status:** {p_status}\n"
                    f"• **Prywatny basen:** {'Tak' if has_pool else 'Brak w bazie'}\n"
                    f"• **Dostęp do plaży:** {'Tak' if beach else 'Brak w bazie'}\n"
                    f"• **Forma własności:** {p_owner}\n"
                    f"• **Główne atuty:** {', '.join(p_high) if p_high else 'Brak w bazie'}\n\n"
                    f"{p_desc}\n\n"
                    f"Czy chcesz uzyskać więcej szczegółów na temat rzutów pięter lub umówić termin prezentacji z naszym doradcą?"
                )
            else:
                reply_text = (
                    f"Here are the verified specifications for **{p_name}** ({p_id}):\n\n"
                    f"• **Location:** {p_loc}\n"
                    f"• **Price:** {p_price}\n"
                    f"• **Bedrooms / Area:** {p_beds} | {p_area}\n"
                    f"• **Status:** {p_status}\n"
                    f"• **Private pool:** {'Yes' if has_pool else 'Not documented in verified database'}\n"
                    f"• **Beach access:** {'Yes' if beach else 'Not documented in verified database'}\n"
                    f"• **Ownership:** {p_owner}\n"
                    f"• **Key Highlights:** {', '.join(p_high) if p_high else 'Not documented in verified database'}\n\n"
                    f"{p_desc}\n\n"
                    f"Would you like more details regarding floor plans or assistance scheduling a viewing tour?"
                )
            cards = [card]
            record_or_update_lead(
                db=db,
                conversation=conversation,
                buying_signal=f"Requested details for {p_id} ({p_name})"
            )
        else:
            if language == "th":
                reply_text = (
                    f"ไม่พบอสังหาริมทรัพย์รหัส '{pid}' ในฐานข้อมูลที่ตรวจสอบแล้วของเราครับ\n\n"
                    f"ทีมที่ปรึกษาของเรา ({settings.ADVISOR_EMAIL}) สามารถตรวจสอบโครงการเฉพาะหรือห้องว่างจากผู้พัฒนาโครงการให้คุณได้ "
                    f"คุณสนใจเลือกดูรายการที่เปิดขายในภูเก็ตหรือเกาะสมุยแทนไหมครับ?"
                )
            elif language == "pl":
                reply_text = (
                    f"Nie znaleziono nieruchomości o identyfikatorze '{pid}' w naszej zweryfikowanej bazie danych.\n\n"
                    f"Nasz zespół doradców ({settings.ADVISOR_EMAIL}) może zweryfikować dostępność konkretnych projektów lub sprawdzić oferty deweloperskie. "
                    f"Czy chciałbyś przejrzeć nasze dostępne oferty na Phuket lub Koh Samui?"
                )
            else:
                reply_text = (
                    f"Property ID '{pid}' was not found in our verified database.\n\n"
                    f"If you are looking for a specific development, our advisory team ({settings.ADVISOR_EMAIL}) can verify current availability for you. "
                    f"Would you like to explore our active verified listings in Phuket or Koh Samui instead?"
                )
            cards = []

        # Persist active property context (reference memory) — facts always re-fetched next turn
        resolved_id = str(p_id or pid or "").strip() or None
        if resolved_id and cards:
            pctx = set_active_property(pctx, resolved_id, reference=p_name)
            pctx = update_rolling_summary(pctx, user_message, reply_text, resolved_id)
        else:
            pctx = update_rolling_summary(pctx, user_message, reply_text, None)
        stored_prefs = save_property_context(conversation, pctx)

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text,
            structured_data={"properties": cards, "property_ids": [c.get("property_id") for c in cards if isinstance(c, dict) and c.get("property_id")]},
            new_preferences=stored_prefs
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="property_details",
                model="deterministic",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                llm_calls_count=0,
                latency_sec=round(time.time() - start_time, 3)
            ),
            db=db
        )

        if language == "pl":
            q_replies = [
                f"Umów prezentację dla {resolved_id or pid}",
                "Pokaż wille na Phuket",
                "Czy obcokrajowiec może kupić freehold?"
            ] if cards else ["Pokaż wille na Phuket", "Pokaż wille na Samui", "Apartamenty poniżej 10 mln THB"]
        else:
            q_replies = [
                f"Schedule viewing for {pid}",
                "Show villas in Phuket",
                "Can foreigners buy freehold?"
            ] if cards else ["Show villas in Phuket", "Show villas in Samui", "Condos under 10M THB"]

        return {
            "type": "property_cards" if cards else "text",
            "text": reply_text,
            "properties": cards,
            "quick_replies": q_replies,
            "lead_captured": False,
            "preferences": stored_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _handle_property_comparison(
        self,
        db: Session,
        conversation: Conversation,
        args: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Compares properties using compact projections and a structured Markdown comparison table."""
        stored_prefs = dict(conversation.preferences or {})
        prop_ids = args.get("property_ids", [])
        if not prop_ids:
            prop_ids = stored_prefs.get("last_result_ids", [])[:2]

        props = get_properties_by_ids(db, prop_ids)
        if len(props) < 2:
            if language == "th":
                reply_text = "สำหรับการเปรียบเทียบ กรุณาระบุชื่ออสังหาริมทรัพย์อย่างน้อย 2 รายการ (เช่น 'เปรียบเทียบ 15 Sukhumvit Residences กับ Sea Heaven') หรือค้นหารายการที่สนใจก่อนได้ครับ"
            elif language == "pl":
                reply_text = "Aby porównać nieruchomości, wskaż co najmniej dwie oferty (np. 'Porównaj 15 Sukhumvit Residences i Sea Heaven') lub najpierw wyszukaj interesujące Cię wille!"
            else:
                reply_text = "To compare properties, please specify at least two listings (e.g. 'Compare 15 Sukhumvit Residences and Sea Heaven') or perform a search first!"
            append_conversation_turn(db, conversation, user_message, reply_text)
            return {
                "type": "text",
                "text": reply_text,
                "properties": [],
                "quick_replies": self._generate_quick_replies(user_message, [], language=language),
                "lead_captured": False,
                "preferences": stored_prefs,
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": language
            }

        cards = [format_property_card(p) for p in props]
        projections = [to_compact_projection(p, intent="comparison") for p in props]

        prompt_tokens = 0
        comp_tokens = 0
        llm_calls = 0
        reply_text = ""

        if self.client:
            try:
                system_prompt = get_compact_system_prompt(language, site_origin=conversation.site_origin)
                if language == "th":
                    user_prompt = (
                        f"คำถามของผู้ใช้: \"{user_message}\"\n\n"
                        f"เปรียบเทียบ {len(props)} อสังหาริมทรัพย์ที่ผ่านการตรวจสอบในประเทศไทยอย่างกระชับและเป็นกลาง "
                        f"ข้อกำหนดบังคับ: ต้องจัดทำตาราง Markdown เปรียบเทียบ: "
                        f"รหัสอสังหาฯ, ทำเล, ราคาเริ่มต้น, ประเภท, จำนวนห้องนอน, พื้นที่ใช้สอย, สระว่ายน้ำส่วนตัว, สถานะ, รูปแบบกรรมสิทธิ์ "
                        f"และตามด้วยสรุปสั้นๆ 2 ประโยค:\n"
                        f"{json.dumps(projections, indent=2, ensure_ascii=False)}"
                    )
                elif language == "pl":
                    user_prompt = (
                        f"Pytanie użytkownika: \"{user_message}\"\n\n"
                        f"Porównaj zwięźle poniższe {len(props)} zweryfikowane nieruchomości w Tajlandii. "
                        f"WYMAGANIE FORMALNE: Twoja odpowiedź MUSI zawierać czytelną tabelę Markdown porównującą: "
                        f"Identyfikator, Lokalizacja, Cena wyjściowa, Typ nieruchomości, Liczba sypialni, Powierzchnia, Prywatny basen, Status i Forma własności. "
                        f"Pod tabelą dodaj 2 zwięzłe zdania obiektywnego podsumowania:\n"
                        f"{json.dumps(projections, indent=2)}"
                    )
                else:
                    user_prompt = (
                        f"User asked: \"{user_message}\"\n\n"
                        f"Compare the following {len(props)} verified Thailand properties concisely for a buyer. "
                        f"MANDATORY REQUIREMENT: Format the comparison using a clear Markdown table comparing: "
                        f"Property ID, Location, Starting Price, Property Type, Bedrooms, Built Area, Private Pool, Status, and Ownership. "
                        f"Follow the table with 2 concise sentences of objective summary:\n"
                        f"{json.dumps(projections, indent=2)}"
                    )
                resp = _safe_openai_create(
                    self.client,
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.0,
                    max_tokens=500
                )
                reply_text = resp.choices[0].message.content or ""
                llm_calls = 1
                if resp.usage:
                    prompt_tokens = resp.usage.prompt_tokens
                    comp_tokens = resp.usage.completion_tokens
            except Exception as e:
                logger.error(f"Error generating comparison: {e}")
                reply_text = ""

        if not reply_text:
            p1, p2 = props[0], props[1]
            if language == "th":
                reply_text = (
                    f"ตารางเปรียบเทียบระหว่าง **{p1.property_name}** และ **{p2.property_name}**:\n\n"
                    f"| หัวข้อ | {p1.property_name} | {p2.property_name} |\n"
                    f"| :--- | :--- | :--- |\n"
                    f"| **รหัสอสังหาริมทรัพย์** | `{p1.property_id}` | `{p2.property_id}` |\n"
                    f"| **ทำเล** | {p1.district or p1.province} | {p2.district or p2.province} |\n"
                    f"| **ราคาเริ่มต้น** | {p1.price_display or 'ราคาตามสอบถาม'} | {p2.price_display or 'ราคาตามสอบถาม'} |\n"
                    f"| **ประเภท** | {p1.property_type} | {p2.property_type} |\n"
                    f"| **ห้องนอน** | {p1.bedrooms_display or 'ไม่ระบุ'} | {p2.bedrooms_display or 'ไม่ระบุ'} |\n"
                    f"| **พื้นที่ใช้สอย** | {p1.area_display or 'ไม่ระบุ'} | {p2.area_display or 'ไม่ระบุ'} |\n"
                    f"| **สระว่ายน้ำส่วนตัว** | {'มี' if p1.has_pool else 'ไม่มี'} | {'มี' if p2.has_pool else 'ไม่มี'} |\n"
                    f"| **สถานะ** | {p1.status or 'พร้อมขาย'} | {p2.status or 'พร้อมขาย'} |\n"
                    f"| **รูปแบบกรรมสิทธิ์** | {p1.ownership_type or 'Freehold / Leasehold'} | {p2.ownership_type or 'Freehold / Leasehold'} |\n\n"
                    f"ทั้งสองโครงการสร้างตามมาตรฐานคุณภาพในทำเลยอดนิยม "
                    f"คุณต้องการดูผังอาคารเพิ่มเติมหรือนัดหมายเข้าชมโครงการไหมครับ?"
                )
            elif language == "pl":
                reply_text = (
                    f"Oto zestawienie porównawcze dla **{p1.property_name}** oraz **{p2.property_name}**:\n\n"
                    f"| Parametr | {p1.property_name} | {p2.property_name} |\n"
                    f"| :--- | :--- | :--- |\n"
                    f"| **Identyfikator** | `{p1.property_id}` | `{p2.property_id}` |\n"
                    f"| **Lokalizacja** | {p1.district or p1.province} | {p2.district or p2.province} |\n"
                    f"| **Cena wyjściowa** | {p1.price_display or 'Cena na zapytanie'} | {p2.price_display or 'Cena na zapytanie'} |\n"
                    f"| **Typ nieruchomości** | {p1.property_type} | {p2.property_type} |\n"
                    f"| **Sypialnie** | {p1.bedrooms_display or 'Brak danych'} | {p2.bedrooms_display or 'Brak danych'} |\n"
                    f"| **Powierzchnia** | {p1.area_display or 'Brak danych'} | {p2.area_display or 'Brak danych'} |\n"
                    f"| **Prywatny basen** | {'Tak' if p1.has_pool else 'Nie'} | {'Tak' if p2.has_pool else 'Nie'} |\n"
                    f"| **Status** | {p1.status or 'Dostępne'} | {p2.status or 'Dostępne'} |\n"
                    f"| **Forma własności** | {p1.ownership_type or 'Freehold / Leasehold'} | {p2.ownership_type or 'Freehold / Leasehold'} |\n\n"
                    f"Obie inwestycje reprezentują zweryfikowane standardy budowlane w prestiżowych lokalizacjach. "
                    f"Czy chciałbyś zapoznać się ze szczegółowymi rzutami architektonicznymi lub umówić prezentację?"
                )
            else:
                reply_text = (
                    f"Here is a side-by-side comparison between **{p1.property_name}** and **{p2.property_name}**:\n\n"
                    f"| Feature | {p1.property_name} | {p2.property_name} |\n"
                    f"| :--- | :--- | :--- |\n"
                    f"| **Property ID** | `{p1.property_id}` | `{p2.property_id}` |\n"
                    f"| **Location** | {p1.district or p1.province} | {p2.district or p2.province} |\n"
                    f"| **Starting Price** | {p1.price_display or 'On Request'} | {p2.price_display or 'On Request'} |\n"
                    f"| **Property Type** | {p1.property_type} | {p2.property_type} |\n"
                    f"| **Bedrooms** | {p1.bedrooms_display or 'N/A'} | {p2.bedrooms_display or 'N/A'} |\n"
                    f"| **Built Area** | {p1.area_display or 'N/A'} | {p2.area_display or 'N/A'} |\n"
                    f"| **Private Pool** | {'Yes' if p1.has_pool else 'No'} | {'Yes' if p2.has_pool else 'No'} |\n"
                    f"| **Status** | {p1.status or 'Available'} | {p2.status or 'Available'} |\n"
                    f"| **Ownership** | {p1.ownership_type or 'Freehold / Leasehold'} | {p2.ownership_type or 'Freehold / Leasehold'} |\n\n"
                    f"Both developments offer verified construction quality in high-demand investment locations. "
                    f"Would you like to examine the architectural floor plans or schedule a viewing tour?"
                )

        stored_prefs["selected_property_ids"] = [p.property_id for p in props]
        _pctx = load_property_context(conversation)
        _pctx = set_comparison_set(_pctx, [p.property_id for p in props])
        # Keep first compared as active unless user already had a different active in the set
        if props:
            _pctx = set_active_property(_pctx, props[0].property_id, reference=getattr(props[0], "property_name", None))
        stored_prefs.update(save_property_context(conversation, _pctx))

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text,
            structured_data={"properties": cards, "property_ids": [c["property_id"] for c in cards]},
            new_preferences=stored_prefs
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="property_comparison",
                model=self.model if llm_calls else "deterministic",
                prompt_tokens=prompt_tokens,
                completion_tokens=comp_tokens,
                total_tokens=prompt_tokens + comp_tokens,
                llm_calls_count=llm_calls,
                latency_sec=round(time.time() - start_time, 3)
            ),
            db=db
        )

        if language == "th":
            q_replies = [
                f"นัดชมโครงการ {props[0].property_id}",
                f"นัดชมโครงการ {props[1].property_id}",
                "ชาวต่างชาติซื้อ Freehold ได้ไหม?"
            ]
        elif language == "pl":
            q_replies = [
                f"Umów wizytę dla {props[0].property_id}",
                f"Umów wizytę dla {props[1].property_id}",
                "Czy obcokrajowiec może kupić freehold?"
            ]
        else:
            q_replies = [
                f"Schedule viewing for {props[0].property_id}",
                f"Schedule viewing for {props[1].property_id}",
                "Can foreigners own these freehold?"
            ]

        return {
            "type": "property_cards",
            "text": reply_text,
            "properties": cards,
            "quick_replies": q_replies,
            "lead_captured": False,
            "preferences": stored_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _handle_brochure_request(
        self,
        db: Session,
        conversation: Conversation,
        args: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """
        Handles brochure/PDF download requests from the public chatbot.
        Returns a 'brochure' type response with brochure_property_id so the
        frontend auto-triggers the download. Zero LLM tokens consumed.
        """
        prop_id = args.get("property_id")
        needs_clarification = args.get("needs_clarification", False)
        stored_prefs = dict(conversation.preferences or {})
        stored_prefs.pop("focus_summary", None)
        stored_prefs.pop("what_we_fetched", None)

        # Multilingual clarification messages
        clarification_msgs = {
            "en": "Which property would you like the brochure for? Please mention the property name, or first search for properties and then request the brochure.",
            "pl": "Dla której nieruchomości chcesz broszurę? Podaj nazwę nieruchomości lub najpierw wyszukaj nieruchomości.",
            "th": "คุณต้องการโบรชัวร์สำหรับอสังหาริมทรัพย์ใด? กรุณาระบุชื่ออสังหาริมทรัพย์ หรือค้นหาก่อนแล้วขอโบรชัวร์ครับ"
        }

        # Confirm messages
        confirm_msgs = {
            "en": "📄 Generating your professional property brochure - your download will start automatically.",
            "pl": "📄 Generuję profesjonalną broszurę nieruchomości - pobieranie rozpocznie się automatycznie.",
            "th": "📄 กำลังสร้างโบรชัวร์อสังหาริมทรัพย์ระดับมืออาชีพสำหรับคุณ - การดาวน์โหลดจะเริ่มต้นโดยอัตโนมัติ"
        }

        # 1. Always prioritize any property explicitly named or referenced in the user's current message
        u_clean = user_message.lower()
        all_props = db.query(Property).all() if db is not None else []
        explicit_prop_id = None
        common_words = {'residence', 'residences', 'villa', 'villas', 'condo', 'condominium', 'waterfront', 'lake', 'phase', 'park', 'hill', 'hills', 'view', 'beach', 'project'}

        # Check explicit property ID (e.g. PROP-PHK-001)
        for p in all_props:
            if p.property_id and p.property_id.lower() in u_clean:
                explicit_prop_id = p.property_id
                break

        # Check target extracted from "brochure for <target>"
        cand_target = None
        if not explicit_prop_id:
            target_match = re.search(r"\b(?:for|of|about|dla|do|o|สำหรับ|ของ)\s+([a-zA-Z0-9\s]{3,})", u_clean)
            if target_match:
                cand_target = target_match.group(1).strip()
                for p in all_props:
                    p_name_l = (p.property_name or "").lower()
                    if cand_target and len(cand_target) >= 4 and cand_target in p_name_l:
                        explicit_prop_id = p.property_id
                        break

        # Check bidirectional substring and distinctive keyword tokens
        if not explicit_prop_id:
            for p in all_props:
                p_name_l = (p.property_name or "").lower()
                if len(p_name_l) > 3 and (p_name_l in u_clean or (len(u_clean) > 4 and u_clean in p_name_l)):
                    explicit_prop_id = p.property_id
                    break
                tokens = [w for w in p_name_l.split() if len(w) >= 4 and w not in common_words]
                for t in tokens:
                    if t in u_clean:
                        explicit_prop_id = p.property_id
                        break
                if explicit_prop_id:
                    break

        # Check Firestore for explicit target or current message
        if not explicit_prop_id and getattr(settings, "ENABLE_FIRESTORE_SYNC", True):
            from app.services.firestore_service import get_property_by_id_from_firestore
            for target_cand in [cand_target, prop_id, u_clean]:
                if target_cand and len(str(target_cand).strip()) >= 3:
                    fs_match = get_property_by_id_from_firestore(str(target_cand).strip())
                    if fs_match:
                        explicit_prop_id = fs_match.get("property_id")
                        break

        if explicit_prop_id:
            prop_id = explicit_prop_id
            needs_clarification = False
        elif not prop_id:
            selected_ids = stored_prefs.get("selected_property_ids", [])
            last_ids = stored_prefs.get("last_result_ids", [])
            if len(selected_ids) == 1:
                prop_id = selected_ids[0]
                needs_clarification = False
            elif len(last_ids) == 1:
                prop_id = last_ids[0]
                needs_clarification = False

        if not prop_id or needs_clarification:
            reply_text = clarification_msgs.get(language, clarification_msgs["en"])
            append_conversation_turn(db=db, conversation=conversation, user_message=user_message, assistant_message=reply_text)
            return {
                "type": "text",
                "text": reply_text,
                "properties": [],
                "quick_replies": ["Show me properties in Phuket", "Search villas under 15M THB"],
                "lead_captured": False,
                "preferences": stored_prefs,
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": language,
                "brochure_property_id": None
            }

        # Property found — fetch property details and record high-intent lead for staff follow-up
        from app.core.tenant import resolve_tenant
        tenant = resolve_tenant(conversation.site_origin)

        prop = db.query(Property).filter(Property.property_id == prop_id.upper()).first()
        p_name = prop.property_name if prop else prop_id
        pref_loc = f"{prop.district}, {prop.province}" if prop and prop.district else (prop.province if prop else None)

        # 1. Record lead linked to conversation in PostgreSQL for staff follow-up
        record_or_update_lead(
            db=db,
            conversation=conversation,
            preferred_location=pref_loc,
            preferred_type=prop.property_type if prop else None,
            buying_signal=f"Requested brochure for {prop_id} ({p_name})",
            notes=f"Client requested verified property brochure for {prop_id} on {tenant.brand_name} ({tenant.domain})"
        )

        # 2. Persist requested brochure ID in conversation preferences
        requested = list(stored_prefs.get("brochures_requested") or [])
        if prop_id not in requested:
            requested.append(prop_id)
        stored_prefs["brochures_requested"] = requested
        conversation.preferences = stored_prefs
        if hasattr(conversation, "__table__"):
            try:
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        # Generate 8-page luxury presentation brochure and upload to S3 / storage
        from app.services.brochure_service import generate_and_upload_brochure
        lead_name = stored_prefs.get("contact_name") or stored_prefs.get("name")
        try:
            brochure_res = generate_and_upload_brochure(
                db=db,
                property_id=prop_id,
                site_origin=conversation.site_origin,
                language=language,
                client_name=lead_name
            )
            pdf_url = brochure_res.get("file_url") or brochure_res.get("download_url")
        except Exception as be:
            logger.error(f"Failed to generate and upload brochure for {prop_id}: {be}", exc_info=True)
            pdf_url = f"/api/v1/chat/brochure/{prop_id}?site_origin={conversation.site_origin}&session_id={conversation.session_id}"

        confirm_msgs = {
            "en": f"📄 Official brochure for {p_name} - [Download PDF]({pdf_url})",
            "pl": f"📄 Oficjalna broszura dla {p_name} - [Pobierz PDF]({pdf_url})",
            "th": f"📄 โบรชัวร์สำหรับ {p_name} - [ดาวน์โหลด PDF]({pdf_url})"
        }

        reply_text = confirm_msgs.get(language, confirm_msgs["en"])
        append_conversation_turn(db=db, conversation=conversation, user_message=user_message, assistant_message=reply_text)
        return {
            "type": "brochure",
            "text": reply_text,
            "properties": [],
            "quick_replies": [
                f"View details for {prop_id}",
                "Schedule private viewing",
                "Contact senior advisor"
            ],
            "lead_captured": True,
            "preferences": stored_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language,
            "brochure_property_id": prop_id,
            "pdf_url": pdf_url,
            "download_url": pdf_url
        }

    def _handle_refine_filter(
        self,
        db: Session,
        conversation: Conversation,
        refinements: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Applies filter refinement deterministically, queries DB/cache, and synthesizes response with compact context."""
        stored_prefs = dict(conversation.preferences or {})
        active_filters = dict(stored_prefs.get("active_filters", {}))

        for k, v in refinements.items():
            active_filters[k] = v
            if k == "max_price":
                stored_prefs["budget_max"] = v
                stored_prefs["max_price"] = v
                usd_approx = int(v / 35)
                stored_prefs["budget_display"] = f"Up to {v/1_000_000:g}M THB (~${usd_approx:,} USD)"
            elif k == "location":
                stored_prefs["location"] = v
            elif k == "property_type":
                stored_prefs["property_type"] = v
            elif k == "has_pool":
                stored_prefs["has_pool"] = v
                features = set(stored_prefs.get("features", []))
                if v:
                    features.add("Private Pool")
                else:
                    features.discard("Private Pool")
                stored_prefs["features"] = sorted(list(features))

        stored_prefs["active_filters"] = active_filters
        stored_prefs["pagination_offset"] = 0

        loc = active_filters.get("location") or stored_prefs.get("location")
        p_type = active_filters.get("property_type") or stored_prefs.get("property_type")
        bedrooms = active_filters.get("bedrooms") or stored_prefs.get("bedrooms_num")
        max_p = Decimal(str(active_filters["max_price"])) if active_filters.get("max_price") else None
        has_pool = active_filters.get("has_pool")

        seen_ids = get_session_seen_property_ids(conversation)
        params = PropertyFilterParams(
            location=loc,
            property_type=p_type,
            bedrooms=bedrooms,
            max_price=max_p,
            has_pool=has_pool,
            offset=0,
            limit=4
        )
        cards = search_properties_unified(db, params, limit=4, exclude_property_ids=seen_ids)

        prompt_tokens = 0
        comp_tokens = 0
        llm_calls = 0
        cache_hit = False
        reply_text = ""

        if cards:
            stored_prefs["seen_property_ids"] = list(set(seen_ids + [c["property_id"] for c in cards]))
            stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
            if self.client:
                try:
                    projections = [
                        {
                            "id": c["property_id"],
                            "name": c["name"],
                            "price": c["price"],
                            "location": c["location"],
                            "type": c["property_type"],
                            "bedrooms": c["bedrooms"],
                            "pool": c["has_pool"]
                        }
                        for c in cards
                    ]
                    system_prompt = get_compact_system_prompt(language, site_origin=conversation.site_origin)
                    if language == "th":
                        user_content = (
                            f"ผู้ใช้ปรับเงื่อนไขการค้นหา: \"{user_message}\"\n"
                            f"พบ {len(cards)} อสังหาริมทรัพย์ที่ตรวจสอบแล้ว:\n"
                            f"{json.dumps(projections, ensure_ascii=False)}\n"
                            f"ทำหน้าที่เป็นที่ปรึกษาอสังหาริมทรัพย์มืออาชีพ Tajlandia นำเสนอรายการแบบมืออาชีพ "
                            f"ใช้ **1. [ชื่อโครงการ]** และจุด • เท่านั้น (ห้าม ### และห้าม '-') "
                            f"หัวข้อย่อย: 📍 ทำเล, 💰 ราคา, 🛏️ ห้องนอน, 🏊 สิ่งอำนวยความสะดวก, 📜 กรรมสิทธิ์ "
                            f"ห้ามตาราง Markdown ปิดท้ายด้วย **ขั้นตอนถัดไป**"
                        )
                    elif language == "pl":
                        user_content = (
                            f"Użytkownik zaktualizował kryteria: \"{user_message}\".\n"
                            f"Znaleziono {len(cards)} zweryfikowanych nieruchomości:\n"
                            f"{json.dumps(projections)}\n"
                            f"Działaj jako profesjonalny doradca ds. nieruchomości Tajlandia. Zaprezentuj oferty numerowane z punktorami • (NIGDY ### ani '-'):\n"
                            f"**1. [Nazwa nieruchomości]**\n"
                            f"• 📍 **Lokalizacja:** [Lokalizacja]\n"
                            f"• 💰 **Cena wyjściowa:** **[Cena w THB]** (~€[EUR] / ~[PLN] PLN)\n"
                            f"• 🛏️ **Sypialnie:** [Sypialnie] | 📐 **Typ:** [Typ]\n"
                            f"• 🏊 **Udogodnienia:** [Basen itp.]\n"
                            f"• 📜 **Forma własności:** [Foreign Freehold w condo / Leasehold na willę]\n"
                            f"NIGDY nie generuj tabel markdown. Zakończ sekcją **Kolejne kroki** z punktorami •."
                        )
                    else:
                        user_content = (
                            f"The user refined their search: \"{user_message}\".\n"
                            f"Found {len(cards)} verified properties:\n"
                            f"{json.dumps(projections)}\n"
                            f"Act as a professional real estate advisor for Tajlandia. Present ALL verified properties in clean numbered blocks with • bullets only (NEVER use ### headers or dash '-' bullets):\n"
                            f"**1. [Property Name]**\n"
                            f"• 📍 **Location:** [Location]\n"
                            f"• 💰 **Starting Price:** **[Price in THB]** (~€[EUR] / ~[PLN] PLN)\n"
                            f"• 🛏️ **Bedrooms:** [Bedrooms] | 📐 **Type:** [Type]\n"
                            f"• 🏊 **Amenities & Highlights:** [Pool, Sea View, etc.]\n"
                            f"• 📜 **Ownership Structure:** [Foreign Freehold Condominium / 30-Year Registered Leasehold Villa]\n"
                            f"NEVER output markdown tables. Conclude with **Recommended next steps** using • bullets."
                        )
                    resp = _safe_openai_create(
                        self.client,
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content}
                        ],
                        temperature=0.0,
                        max_tokens=800
                    )
                    reply_text = resp.choices[0].message.content or ""
                    llm_calls = 1
                    if resp.usage:
                        prompt_tokens = resp.usage.prompt_tokens
                        comp_tokens = resp.usage.completion_tokens
                except Exception as e:
                    logger.error(f"Error in refine synthesis: {e}")
                    reply_text = ""

            if not reply_text:
                reply_text = build_rich_property_recommendation_text(
                    cards=cards,
                    language=language,
                    is_alternative=False,
                    is_sparse=False,
                    location=loc,
                    budget=max_p,
                    bedrooms=bedrooms,
                    site_origin=conversation.site_origin
                )
        else:
            # Exact refine criteria had no production match — never invent/guess alternatives
            cards = []
            stored_prefs["last_result_ids"] = []
            reply_text = get_data_unavailable_fallback(
                language=language,
                site_origin=getattr(conversation, "site_origin", None),
                detail=user_message[:120] if user_message else None,
            )

        updated_prefs = extract_user_preferences(
            user_message=user_message,
            stored_preferences=stored_prefs,
            tool_args=active_filters
        )

        reply_text = ensure_structured_response(reply_text, cards=cards, user_message=user_message, language=language)
        reply_text = ThaiPropertyLegalGuardrail.sanitize_response(
            user_message=user_message,
            response_text=reply_text,
            language=language
        )

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text,
            structured_data={"properties": cards, "property_ids": [c["property_id"] for c in cards]},
            new_preferences=updated_prefs
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="refine_filter",
                model=self.model if llm_calls else "deterministic",
                prompt_tokens=prompt_tokens,
                completion_tokens=comp_tokens,
                total_tokens=prompt_tokens + comp_tokens,
                llm_calls_count=llm_calls,
                latency_sec=round(time.time() - start_time, 3),
                cache_hit=cache_hit
            ),
            db=db
        )

        return {
            "type": "property_cards" if cards else "text",
            "text": reply_text,
            "properties": cards,
            "quick_replies": self._generate_quick_replies(user_message, cards, language=language),
            "lead_captured": False,
            "preferences": conversation.preferences or updated_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _handle_property_search(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Directly executes search in DB/cache, then synthesizes recommendations with a single compact LLM call without tool schemas."""
        stored_prefs = dict(conversation.preferences or {})
        extracted = extract_search_filters_deterministic(user_message)
        cache_hit = False
        is_show_all = bool(extracted.get("show_all")) or any(k in user_message.lower() for k in ["show all", "all properties", "view all", "list all", "wszystkie nieruchomości", "wszystkie oferty"])
        foreign_loc = extracted.get("foreign_location")
        if is_show_all:
            stored_prefs = {}
            conversation.preferences = {}
            params = PropertyFilterParams(offset=0, limit=10)
            cards = search_properties_unified(db, params, limit=10)
            search_filters = {}
        elif foreign_loc:
            cards = []
            loc = None
            p_type = extracted.get("property_type")
            bedrooms = extracted.get("bedrooms")
            max_p = Decimal(str(extracted["max_price"])) if extracted.get("max_price") else None
            has_pool = extracted.get("has_pool")
            search_filters = {
                "foreign_location": foreign_loc,
                "property_type": p_type,
                "bedrooms": bedrooms,
                "max_price": float(max_p) if max_p else None,
                "has_pool": has_pool
            }
            stored_prefs["active_filters"] = search_filters
            seen_ids = get_session_seen_property_ids(conversation)
        else:
            if extracted.get("is_broad_thailand") or (any(k in user_message.lower() for k in ["in thailand", "across thailand", "w tajlandii", "w całej tajlandii", "ทั่วไทย", "ในไทย"]) and not extracted.get("location")):
                loc = None
                stored_prefs.pop("location", None)
            else:
                loc = extracted.get("location") or stored_prefs.get("location")

            if extracted.get("is_multiple_types"):
                p_type = None
                stored_prefs.pop("property_type", None)
            else:
                p_type = extracted.get("property_type") or stored_prefs.get("property_type")

            bedrooms = extracted.get("bedrooms") or stored_prefs.get("bedrooms_num")
            max_p = Decimal(str(extracted["max_price"])) if extracted.get("max_price") else (Decimal(str(stored_prefs["budget_max"])) if stored_prefs.get("budget_max") else None)
            has_pool = extracted.get("has_pool", stored_prefs.get("has_pool"))

            search_filters = {
                "location": loc,
                "property_type": p_type,
                "bedrooms": bedrooms,
                "max_price": float(max_p) if max_p else None,
                "has_pool": has_pool
            }
            stored_prefs["active_filters"] = search_filters
            seen_ids = get_session_seen_property_ids(conversation)
            params = PropertyFilterParams(
                location=loc,
                property_type=p_type,
                bedrooms=bedrooms,
                max_price=max_p,
                has_pool=has_pool,
                offset=0,
                limit=4
            )
            # Fresh searches must NOT exclude previously seen IDs — exclusion is only for "show more".
            # Excluding on every search caused unrelated new listings and felt like hallucination.
            cards = search_properties_unified(db, params, limit=4, exclude_property_ids=None)

            # Smart fresh criteria recovery: If merged search yielded 0 results because of stale constraints
            # from earlier turns, retry with ONLY the fresh criteria specified in this turn.
            has_fresh_criteria = bool(extracted.get("location") or extracted.get("property_type") or extracted.get("bedrooms") or extracted.get("max_price") or extracted.get("has_pool"))
            if not cards and has_fresh_criteria:
                fresh_loc = extracted.get("location")
                fresh_type = extracted.get("property_type")
                fresh_bed = extracted.get("bedrooms")
                fresh_max_p = Decimal(str(extracted["max_price"])) if extracted.get("max_price") else None
                fresh_pool = extracted.get("has_pool")

                fresh_params = PropertyFilterParams(
                    location=fresh_loc,
                    property_type=fresh_type,
                    bedrooms=fresh_bed,
                    max_price=fresh_max_p,
                    has_pool=fresh_pool,
                    offset=0,
                    limit=4
                )
                matched_fresh = search_properties_unified(db, fresh_params, limit=4, exclude_property_ids=None)
                if matched_fresh:
                    cards = matched_fresh
                    loc, p_type, bedrooms, max_p, has_pool = fresh_loc, fresh_type, fresh_bed, fresh_max_p, fresh_pool
                    search_filters = {
                        "location": loc,
                        "property_type": p_type,
                        "bedrooms": bedrooms,
                        "max_price": float(max_p) if max_p else None,
                        "has_pool": has_pool
                    }
                    stored_prefs["active_filters"] = search_filters

            # If specific sub-district yielded fewer than 2 results, broaden to Phuket to include adjacent luxury villas
            if len(cards) < 2 and loc in ["Bang Tao", "Choeng Thale", "Layan", "Kamala", "Surin", "Rawai", "Karon"]:
                broad_params = PropertyFilterParams(
                    location="Phuket",
                    property_type=p_type,
                    bedrooms=bedrooms,
                    max_price=max_p,
                    has_pool=has_pool,
                    offset=0,
                    limit=4
                )
                broad_matched = search_properties_unified(db, broad_params, limit=4, exclude_property_ids=None)
                for bm in broad_matched:
                    if bm.get("property_id") not in [c.get("property_id") for c in cards]:
                        cards.append(bm)

        prompt_tokens = 0
        comp_tokens = 0
        llm_calls = 0
        reply_text = ""

        if cards:
            stored_prefs["seen_property_ids"] = list(set(seen_ids + [c["property_id"] for c in cards]))
            stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
            # Persistent property context: ordinals for follow-ups; single-result => active focus
            _pctx = load_property_context(conversation)
            names = {
                str(c.get("property_id")): str(c.get("name") or c.get("property_name") or "")
                for c in cards if c.get("property_id")
            }
            _pctx = touch_discussed(
                _pctx, stored_prefs["last_result_ids"], names, refresh_ordinals=True
            )
            if len(stored_prefs["last_result_ids"]) == 1:
                only_id = stored_prefs["last_result_ids"][0]
                _pctx = set_active_property(_pctx, only_id, reference=names.get(only_id))
            stored_prefs.update(save_property_context(conversation, _pctx))
            if self.client:
                try:
                    projections = []
                    for c in cards:
                        p_min = c.get("price_numeric_min")
                        approx_eur = f"~€{round(p_min / 38):,}" if p_min else None
                        approx_pln = f"~{round(p_min / 8.5):,} PLN" if p_min else None
                        projections.append({
                            "id": c["property_id"],
                            "name": c["name"],
                            "price": c["price"],
                            "approx_eur": approx_eur,
                            "approx_pln": approx_pln,
                            "location": c["location"],
                            "type": c["property_type"],
                            "bedrooms": c["bedrooms"],
                            "pool": c["has_pool"]
                        })

                    hist_snippets = []
                    for h in (conversation.message_history or [])[-2:]:
                        hist_snippets.append(f"{h['role'].title()}: {h['content']}")
                    hist_ctx = "\n".join(hist_snippets)

                    system_prompt = get_compact_system_prompt(language, site_origin=conversation.site_origin)
                    if language == "th":
                        prompt_content = (
                            (f"บริบทการสนทนา:\n{hist_ctx}\n\n" if hist_ctx else "") +
                            f"คำขอของผู้ใช้: \"{user_message}\"\n\n"
                            f"รายการอสังหาริมทรัพย์ที่ตรงเงื่อนไขในไทย:\n"
                            f"{json.dumps(projections, ensure_ascii=False)}\n\n"
                            f"ทำหน้าที่เป็นที่ปรึกษาอสังหาริมทรัพย์มืออาชีพ Tajlandia นำเสนอรายการแบบมืออาชีพ "
                            f"ใช้ **1. [ชื่อโครงการ]** และจุด • เท่านั้น (ห้าม ### และห้าม '-') "
                            f"หัวข้อย่อย: 📍 ทำเล, 💰 ราคา, 🛏️ ห้องนอน, 🏊 สิ่งอำนวยความสะดวก, 📜 กรรมสิทธิ์ "
                            f"ห้ามตาราง Markdown ปิดท้ายด้วย **ขั้นตอนถัดไป**"
                        )
                    elif language == "pl":
                        prompt_content = (
                            (f"Kontekst rozmowy:\n{hist_ctx}\n\n" if hist_ctx else "") +
                            f"Zapytanie użytkownika: \"{user_message}\"\n\n"
                            f"Dopasowane zweryfikowane nieruchomości w Tajlandii:\n"
                            f"{json.dumps(projections)}\n\n"
                            f"Kontekst walutowy: 1 EUR ≈ 38 THB, 1 PLN ≈ 8.5 THB, 1 USD ≈ 35 THB (np. 8.5M THB to ok. 1.0M PLN / 224,000 EUR). "
                            f"Wszystkie powyższe nieruchomości MIESZCZĄ SIĘ w budżecie klienta. Nigdy nie twierdz, że oferta przekracza budżet. "
                            f"Działaj jako profesjonalny doradca ds. nieruchomości Tajlandia. Zaprezentuj oferty z punktorami • (NIGDY ### ani '-'):\n"
                            f"**1. [Nazwa nieruchomości]**\n"
                            f"• 📍 **Lokalizacja:** [Lokalizacja]\n"
                            f"• 💰 **Cena wyjściowa:** **[Cena w THB]** (~€[EUR] / ~[PLN] PLN)\n"
                            f"• 🛏️ **Sypialnie:** [Sypialnie] | 📐 **Typ:** [Typ]\n"
                            f"• 🏊 **Udogodnienia:** [Basen itp.]\n"
                            f"• 📜 **Forma własności:** [Foreign Freehold w condo / Leasehold na willę]\n"
                            f"NIGDY nie generuj tabel markdown. Zakończ sekcją **Kolejne kroki** z punktorami •."
                        )
                    else:
                        prompt_content = (
                            (f"Recent Context:\n{hist_ctx}\n\n" if hist_ctx else "") +
                            f"User Request: \"{user_message}\"\n\n"
                            f"Matching Verified Properties in Thailand:\n"
                            f"{json.dumps(projections)}\n\n"
                            f"Currency conversion context: 1 EUR ≈ 38 THB, 1 PLN ≈ 8.5 THB, 1 USD ≈ 35 THB. "
                            f"(For example, 8.5M THB is only ~€224,000 / ~1.0M PLN, which is well below a 1 million EUR or 4.5M PLN budget; 19.5M THB is ~€513,000). "
                            f"ALL properties in the list above are strictly WITHIN the user's budget. Never state that a returned property exceeds budget. "
                            f"Act as a professional real estate advisor for Tajlandia. Present ALL verified properties in clean numbered blocks with • bullets only (NEVER use ### headers or dash '-' bullets):\n"
                            f"**1. [Property Name]**\n"
                            f"• 📍 **Location:** [Location]\n"
                            f"• 💰 **Starting Price:** **[Price in THB]** (~€[EUR] / ~[PLN] PLN)\n"
                            f"• 🛏️ **Bedrooms:** [Bedrooms] | 📐 **Type:** [Type]\n"
                            f"• 🏊 **Amenities & Highlights:** [Pool, Sea View, etc.]\n"
                            f"• 📜 **Ownership Structure:** [Foreign Freehold Condominium / 30-Year Registered Leasehold Villa]\n"
                            f"NEVER output markdown tables. Conclude with **Recommended next steps** using • bullets."
                        )

                    is_sparse = (not loc) or (not max_p) or (not bedrooms)
                    if is_sparse:
                        if language == "th":
                            prompt_content += (
                                "\n\nคำแนะนำเพิ่มเติม: ผู้ใช้ยังไม่ได้ระบุรายละเอียดครบถ้วน (เช่น ทำเล งบประมาณ หรือจำนวนห้องนอน) "
                                "ให้สอบถามคำถามเพิ่มเติม 2-3 ข้ออย่างชาญฉลาดในตอนท้าย เพื่อทำความเข้าใจความต้องการและช่วยคัดกรองอสังหาริมทรัพย์ที่ตรงใจที่สุด"
                            )
                        elif language == "pl":
                            prompt_content += (
                                "\n\nWAŻNE: Użytkownik nie podał jeszcze kluczowych szczegółów (np. preferowanej lokalizacji, budżetu lub liczby sypialni). "
                                "Na końcu prezentacji ofert ZAWSZE zadaj 2-3 konkretne, inteligentne pytania doprecyzowujące (np. preferowany rejon: Phuket, Koh Samui czy Bangkok; orientacyjny budżet; cel zakupu)."
                            )
                        else:
                            prompt_content += (
                                "\n\nCRITICAL FOLLOW-UP INSTRUCTION: The user provided limited initial details (e.g., did not specify location, budget, or preferred island/bedrooms). "
                                "Along with presenting the verified options above, ALWAYS conclude by asking 2-3 smart, consultative follow-up questions to understand their exact preferences (e.g. preferred destination in Thailand, target budget range, and primary purpose)."
                            )

                    resp = _safe_openai_create(
                        self.client,
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt_content}
                        ],
                        temperature=0.0,
                        max_tokens=800
                    )
                    reply_text = resp.choices[0].message.content or ""
                    llm_calls = 1
                    if resp.usage:
                        prompt_tokens = resp.usage.prompt_tokens
                        comp_tokens = resp.usage.completion_tokens
                except Exception as e:
                    logger.error(f"Error in search LLM synthesis: {e}")
                    reply_text = ""

            if not reply_text:
                if is_show_all:
                    intro = "Here are all verified properties currently available in our Thailand portfolio:" if language == "en" else ("Oto wszystkie zweryfikowane nieruchomości dostępne w naszym portfolio w Tajlandii:" if language == "pl" else "นี่คืออสังหาริมทรัพย์ที่ผ่านการตรวจสอบทั้งหมดในพอร์ตโฟลิโอของเราในประเทศไทย:")
                    reply_text = intro
                else:
                    reply_text = build_rich_property_recommendation_text(
                        cards=cards,
                        language=language,
                        is_alternative=False,
                        is_sparse=is_sparse,
                        location=loc,
                        budget=max_p,
                        bedrooms=bedrooms,
                        site_origin=conversation.site_origin
                    )
        else:
            # No exact production match — never invent listings; EN/PL/TH fallback only
            cards = []
            stored_prefs["last_result_ids"] = []
            if foreign_loc:
                reply_text = get_off_topic_text(
                    language, site_origin=conversation.site_origin, user_message=user_message
                )
            else:
                reply_text = get_data_unavailable_fallback(
                    language=language,
                    site_origin=getattr(conversation, "site_origin", None),
                    detail=user_message[:120] if user_message else None,
                )

        updated_prefs = extract_user_preferences(
            user_message=user_message,
            stored_preferences=stored_prefs,
            tool_args=search_filters
        )

        reply_text = ensure_structured_response(reply_text, cards=cards, user_message=user_message, language=language)
        reply_text = ThaiPropertyLegalGuardrail.sanitize_response(
            user_message=user_message,
            response_text=reply_text,
            language=language
        )

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text,
            structured_data={"properties": cards, "property_ids": [c["property_id"] for c in cards]},
            new_preferences=updated_prefs
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="property_search",
                model=self.model if llm_calls else "deterministic",
                prompt_tokens=prompt_tokens,
                completion_tokens=comp_tokens,
                total_tokens=prompt_tokens + comp_tokens,
                llm_calls_count=llm_calls,
                latency_sec=round(time.time() - start_time, 3),
                cache_hit=cache_hit
            ),
            db=db
        )

        return {
            "type": "property_cards" if cards else "text",
            "text": reply_text,
            "properties": cards,
            "quick_replies": self._generate_quick_replies(user_message, cards, language=language),
            "lead_captured": False,
            "preferences": conversation.preferences or updated_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _handle_general_question(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Answers general real estate questions without loading heavy tool schemas."""
        prompt_tokens = 0
        comp_tokens = 0
        llm_calls = 0
        reply_text = ""

        u_low = user_message.lower()
        if any(k in u_low for k in ["pdf", "report", "export", "broszura", "dossier", "โบรชัวร์", "รายงาน", "เอกสาร"]) and any(k in u_low for k in ["generate", "export", "download", "send", "wygeneruj", "pobierz", "daj", "create", "สร้าง", "ดาวน์โหลด", "ส่ง", "ขอ", "want", "have", "get"]):
            return self._handle_brochure_request(db, conversation, {}, user_message, start_time, language=language)
        elif self.client:
            try:
                hist_snippets = []
                for h in (conversation.message_history or [])[-2:]:
                    hist_snippets.append(f"{h['role'].title()}: {h['content']}")
                hist_ctx = "\n".join(hist_snippets)

                system_prompt = get_compact_system_prompt(language, site_origin=conversation.site_origin)
                if language == "th":
                    user_content = (
                        (f"บริบทก่อนหน้า:\n{hist_ctx}\n\n" if hist_ctx else "") +
                        f"คำถาม: \"{user_message}\"\n\n"
                        f"ให้คำตอบที่กระชับ เป็นกลาง และเป็นมืออาชีพเกี่ยวกับกฎหมายอสังหาริมทรัพย์ไทย:\n"
                        f"1. สิทธิการถือครองที่ดินของชาวต่างชาติ (ชาวต่างชาติไม่สามารถถือครองกรรมสิทธิ์ที่ดินโดยตรงในนามตนเองได้ แต่สามารถเป็นเจ้าของสิ่งปลูกสร้าง/วิลล่า และจดทะเบียนสิทธิการเช่าระยะยาว Leasehold 30+30+30 ปี หรือถือครองกรรมสิทธิ์ห้องชุดคอนโดมิเนียม Foreign Freehold 49%)\n"
                        f"2. ความแตกต่างระหว่าง Freehold และ Leasehold\n"
                        f"3. โฉนดที่ดิน น.ส.4 จ. (Chanote) ซึ่งเป็นเอกสารสิทธิ์ที่ดินสูงสุดและปลอดภัยที่สุด พร้อมหมุดพิกัดดาวเทียม"
                    )
                elif language == "pl":
                    user_content = (
                        (f"Wcześniejszy kontekst:\n{hist_ctx}\n\n" if hist_ctx else "") +
                        f"Pytanie: \"{user_message}\"\n\n"
                        f"Udziel zwięzłej, obiektywnej i profesjonalnej odpowiedzi po polsku dotyczącej prawa nieruchomości w Tajlandii:\n"
                        f"1. Własność gruntów przez obcokrajowców (obcokrajowiec nie może bezpośrednio posiadać gruntu na własność, ale może posiadać 100% własność budynku i zarejestrowaną 30-letnią odnawialną dzierżawę Leasehold 30+30+30 lat, lub 100% Foreign Freehold w kondominium do 49% powierzchni budynku, lub spółkę prawa tajskiego)\n"
                        f"2. Różnica między Freehold (wieczysta, bezterminowa własność) a Leasehold (długoterminowa dzierżawa rejestrowana w urzędzie ziemskim)\n"
                        f"3. Tytuł Chanote (Nor Sor 4 Jor) - najwyższa i najbezpieczniejsza kategoria aktu własności z satelitarnymi punktami GPS"
                    )
                else:
                    user_content = (
                        (f"Previous context:\n{hist_ctx}\n\n" if hist_ctx else "") +
                        f"Question: \"{user_message}\"\n\n"
                        f"Provide a concise, objective, and professional answer regarding Thailand real estate law and ownership:\n"
                        f"1. Foreign Land Ownership: Foreign nationals cannot directly own freehold land in their individual name under Thai law. However, foreigners can legally own 100% Freehold of condominium units (within the 49% foreign building quota), own villa structures freehold with registered 30-year renewable leaseholds (30+30+30 years) at the Land Department, or acquire via a compliant Thai company structure with legal counsel.\n"
                        f"2. Freehold vs Leasehold: Foreign Freehold is perpetual, absolute individual ownership. Leasehold is a legally registered 30-year long-term lease (renewable up to 90 years) recorded directly on the property's title deed.\n"
                        f"3. Chanote Title Deed (Nor Sor 4 Jor): The highest and most secure classification of land title deed in Thailand, surveyed with official GPS satellite boundary markers, giving absolute right of possession and unrestricted transfer."
                    )

                resp = _safe_openai_create(
                    self.client,
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    temperature=0.0,
                    max_tokens=500
                )
                reply_text = resp.choices[0].message.content or ""
                llm_calls = 1
                if resp.usage:
                    prompt_tokens = resp.usage.prompt_tokens
                    comp_tokens = resp.usage.completion_tokens
            except Exception as e:
                logger.error(f"Error in general question: {e}")
                reply_text = ""

        if not reply_text:
            if language == "th":
                reply_text = (
                    "สำหรับกฎหมายและการถือครองอสังหาริมทรัพย์ในประเทศไทยของผู้ซื้อชาวต่างชาติ มีหลักการสำคัญดังนี้ครับ:\n\n"
                    "1. **การถือกรรมสิทธิ์ที่ดิน:** ตามประมวลกฎหมายที่ดินของไทย ชาวต่างชาติไม่สามารถถือกรรมสิทธิ์ที่ดินในนามบุคคลธรรมดาได้โดยตรง อย่างไรก็ตาม ชาวต่างชาติสามารถถือกรรมสิทธิ์ตัวอาคาร/วิลล่าได้ 100% และจดทะเบียนสิทธิการเช่าระยะยาว (Leasehold) หรือถือครองผ่านนิติบุคคลไทยที่ถูกต้องตามกฎหมาย\n\n"
                    "2. **ข้อแตกต่างระหว่าง Freehold และ Leasehold:**\n"
                    "• **Foreign Freehold:** กรรมสิทธิ์ขาดถาวรตลอดชีพ (มีสิทธิสูงสุด 49% ในอาคารชุดคอนโดมิเนียม)\n"
                    "• **Leasehold:** สิทธิการเช่าระยะยาวที่จดทะเบียน ณ สำนักงานที่ดิน มีอายุสัญญาครั้งละ 30 ปี และต่ออายุได้ตามข้อตกลง (เช่น 30+30+30 ปี รวม 90 ปี)\n\n"
                    "3. **โฉนดที่ดิน (Chanote - น.ส.4 จ.):** คือเอกสารสิทธิ์ที่ดินระดับสูงสุดและปลอดภัยที่สุดในประเทศไทย มีการรังวัดปักหมุดพิกัดดาวเทียม GPS ชัดเจน ผู้ถือครองมีกรรมสิทธิ์สมบูรณ์และสามารถโอนกรรมสิทธิ์ได้อย่างอิสระ\n\n"
                    "*คำแนะนำ: ควรปรึกษาทนายความผู้เชี่ยวชาญในไทยเสมอเพื่อตรวจสอบสัญญาและโฉนดที่ดินอย่างถูกต้อง*"
                )
            elif language == "pl":
                reply_text = (
                    "Zasady zakupu i prawa własności nieruchomości w Tajlandii dla zagranicznych inwestorów przedstawiają się następująco:\n\n"
                    "1. **Własność gruntów przez obcokrajowców:** Zgodnie z tajskim kodeksem ziemskim obcokrajowcy nie mogą bezpośrednio posiadać ziemi na własność (freehold) w swoim imieniu. Istnieją jednak w pełni legalne i bezpieczne rozwiązania:\n"
                    "   • **Foreign Freehold:** 100% pełnej, bezterminowej własności lokalu w kondominium (w ramach 49% kwoty obcokrajowców w budynku).\n"
                    "   • **Wille i domy (Leasehold):** Obcokrajowiec staje się właścicielem samego budynku na własność (Freehold), a grunt zabezpiecza oficjalną, rejestrowaną w rządowym urzędzie ziemskim umową dzierżawy (Leasehold 30 lat z gwarancją przedłużenia na kolejne okresy, łącznie do 90 lat).\n"
                    "   • **Spółka tajska:** Zakup gruntu przez zarejestrowaną spółkę prawa tajskiego (przy zachowaniu wymogów prawnych).\n\n"
                    "2. **Freehold vs Leasehold:**\n"
                    "   • **Freehold:** Wieczysta, pełna własność wpisana do rejestru bez ograniczeń czasowych.\n"
                    "   • **Leasehold:** Długoterminowa dzierżawa chroniona prawem rzeczowym, wpisywana bezpośrednio na odwrocie aktu własności w urzędzie ziemskim.\n\n"
                    "3. **Tytuł Chanote (Nor Sor 4 Jor):** To najwyższa, najbardziej prestiżowa i w 100% bezpieczna kategoria aktu własności ziemi w Tajlandii. Posiada precyzyjne współrzędne satelitarne GPS i betonowe słupki graniczne. Daje niepodważalne prawo własności i pełną swobodę dysponowania nieruchomością.\n\n"
                    "*Zalecenie: Przed finalizacją transakcji zawsze rekomendujemy audyt due diligence przeprowadzony przez licencjonowanego prawnika w Tajlandii.*"
                )
            else:
                reply_text = (
                    "Here is a clear legal overview of property ownership regulations in Thailand for international buyers:\n\n"
                    "1. **Foreign Land Ownership:**\n"
                    "Under the Thai Land Code, foreign nationals cannot directly own land in freehold under their personal name. However, there are established, legally protected pathways:\n"
                    "• **Condominiums (Foreign Freehold):** Foreigners can own 100% absolute freehold title in their personal name up to 49% of the condominium's total salable area.\n"
                    "• **Villas (Building Ownership + Registered Leasehold):** Foreign buyers legally own the physical villa structure in freehold, while the underlying land is secured via a 30-year registered leasehold with contractual renewals (typically 30+30+30 years, totaling 90 years), registered directly at the government Land Department.\n"
                    "• **Thai Majority Company:** Land acquisition through a properly structured Thai commercial entity with legal representation.\n\n"
                    "2. **Difference Between Freehold and Leasehold:**\n"
                    "• **Freehold:** Perpetual, unconditional ownership registered in your name forever with absolute rights to sell, transfer, or inherit.\n"
                    "• **Leasehold:** Long-term contractual and real right of possession registered on the back of the official land deed at the Land Department for 30-year renewable terms.\n\n"
                    "3. **What a Chanote Title Means (Nor Sor 4 Jor):**\n"
                    "The **Chanote** is the highest, most secure title deed in Thailand. It is surveyed using official GPS/satellite pins by the Land Department, conferring undisputed ownership rights, precise borders, and unrestricted legal transferability.\n\n"
                    "*Recommendation: We always advise engaging a qualified Thai real estate attorney for independent contract review and title due diligence.*"
                )

        reply_text = ThaiPropertyLegalGuardrail.sanitize_response(
            user_message=user_message,
            response_text=reply_text,
            language=language
        )

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="general_question",
                model=self.model if llm_calls else "deterministic",
                prompt_tokens=prompt_tokens,
                completion_tokens=comp_tokens,
                total_tokens=prompt_tokens + comp_tokens,
                llm_calls_count=llm_calls,
                latency_sec=round(time.time() - start_time, 3)
            ),
            db=db
        )

        return {
            "type": "text",
            "text": reply_text,
            "properties": [],
            "quick_replies": self._generate_quick_replies(user_message, [], language=language),
            "lead_captured": False,
            "preferences": conversation.preferences or {},
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _handle_greeting(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Warmly greets the visitor or acknowledges pleasantries without triggering fake property searches."""
        reply_text = get_greeting_text(language, user_message, site_origin=conversation.site_origin)

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text
        )

        safe_prefs = dict(conversation.preferences or {})
        safe_prefs.pop("focus_summary", None)
        safe_prefs.pop("what_we_fetched", None)

        # If user explicitly starts with a fresh greeting, clear old criteria so they start clean
        clean_msg = re.sub(r'[^\w\s]', '', user_message.lower()).strip()
        if clean_msg in ["hi", "hello", "hey", "cześć", "czesc", "dzień dobry", "dzien dobry", "witaj", "witam"]:
            safe_prefs = {}
            conversation.preferences = {}
            if hasattr(conversation, "__table__"):
                try:
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(conversation, "preferences")
                    db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass

        return {
            "type": "text",
            "text": reply_text,
            "properties": [],
            "quick_replies": self._generate_quick_replies(user_message, [], language=language),
            "lead_captured": False,
            "preferences": safe_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language
        }

    def _call_openai(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        raw_user_message: Optional[str] = None,
        start_time: Optional[float] = None,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Calls OpenAI with structured tools and persists multi-turn context."""
        messages = [{"role": "system", "content": get_public_system_prompt(language, site_origin=conversation.site_origin)}]

        # Compact long-conversation packing: persistent property IDs + rolling summary + recent window
        history = conversation.message_history or []
        pctx = load_property_context(conversation)
        messages.append({
            "role": "system",
            "content": assemble_llm_context_block(pctx, history),
        })
        recent_n = int(getattr(settings, "PROPERTY_CONTEXT_RECENT_MESSAGES", 20) or 20)
        for h in history[-(recent_n * 2):]:
            messages.append({"role": h["role"], "content": h["content"]})

        messages.append({"role": "user", "content": user_message})

        try:
            response = _safe_openai_create(
                self.client,
                model=self.model,
                messages=messages,
                tools=PUBLIC_TOOLS,
                temperature=self.temperature,
                max_tokens=800
            )

            resp_msg = response.choices[0].message
            tool_calls = resp_msg.tool_calls

            all_cards = []
            lead_captured = False
            logged_tool_calls = []
            logged_tool_results = []

            brochure_property_id = None
            if tool_calls:
                messages.append(resp_msg)
                for tc in tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        fn_args = {}

                    logged_tool_calls.append({"tool": fn_name, "args": fn_args})
                    
                    tool_res, cards, captured = self.execute_tool(db, conversation, fn_name, fn_args)
                    logged_tool_results.append({"tool": fn_name, "result": tool_res})

                    if fn_name == "generate_property_brochure" and tool_res.get("brochure_property_id"):
                        brochure_property_id = tool_res["brochure_property_id"]

                    if cards:
                        all_cards.extend(cards)
                    if captured:
                        lead_captured = True

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_res, ensure_ascii=False)
                    })

                # Follow-up completion to produce grounded text with mandatory structured formatting
                messages_for_second = list(messages)
                if all_cards:
                    messages_for_second.append({
                        "role": "user",
                        "content": (
                            "Present the verified properties in a professional format.\n"
                            "NEVER use ### markdown headers or dash (-) bullets. Use • bullets only:\n"
                            "**1. [Property Name]**\n"
                            "• 📍 **Location:** [Location]\n"
                            "• 💰 **Starting Price:** **[Price in THB]** (~€[EUR] / ~[PLN] PLN)\n"
                            "• 🛏️ **Bedrooms:** [Bedrooms] | 📐 **Type:** [Type]\n"
                            "• 🏊 **Amenities & Highlights:** [Amenities]\n"
                            "• 📜 **Ownership Structure:** [Foreign Freehold Condominium / 30-Year Registered Leasehold Villa]\n"
                            "NEVER output markdown tables.\n"
                            "If follow-up is about 'it/this property', keep the same active listing.\n"
                            "If the user provided sparse details, ask at most 2-3 clarifying questions, then **Recommended next steps** with • bullets."
                        )
                    })
                else:
                    foreign_keywords = [
                        "india", "indie", "indii", "dubai", "miami", "spain", "hiszpania",
                        "london", "londyn", "bali", "poland", "polsce", "cyprus", "greece",
                        "italy", "portugal", "france", "turkey", "germany", "singapore", "australia"
                    ]
                    is_foreign_query = any(re.search(r'\b' + re.escape(fl) + r'\b', (raw_user_message or user_message).lower()) for fl in foreign_keywords)
                    if is_foreign_query:
                        messages_for_second.append({
                            "role": "user",
                            "content": (
                                "The user is inquiring about real estate in a foreign location outside Thailand.\n"
                                "Act as an elite luxury real estate advisor for Varsovia Estate / Tajlandia:\n"
                                "1. Prestigiously and politely clarify that Varsovia Estate specializes exclusively in prime luxury real estate in Thailand (Phuket, Koh Samui, Koh Phangan, Bangkok, Pattaya), and we do not represent properties in foreign markets.\n"
                                "2. Provide a consultative market comparison: Explain why many international investors comparing global resort markets choose Thailand (e.g. attractive rental yields of 6%-10% net annually, 100% Foreign Freehold condominium ownership under personal name, private luxury pool villas offering superior value-per-sqm, and thriving year-round tourism).\n"
                                "3. Ask 2-3 natural, conversational follow-up questions based on what is actually missing from the user's conversation — do not use template questions, base them entirely on the specific context of what the user said.\n"
                                "4. Warmly offer that our senior advisory team can guide them to verified luxury properties in Thailand."
                            )
                        })
                    else:
                        # No production match — honest fallback only; never invent or inject random listings
                        all_cards = []
                        final_text = get_data_unavailable_fallback(
                            language=language,
                            site_origin=getattr(conversation, "site_origin", None),
                            detail=(raw_user_message or user_message or "")[:120] or None,
                        )
                        messages_for_second = None
                if messages_for_second is not None:
                    second_resp = _safe_openai_create(
                        self.client,
                        model=self.model,
                        messages=messages_for_second,
                        temperature=self.temperature,
                        max_tokens=800
                    )
                    final_text = second_resp.choices[0].message.content or ""
                    if not final_text and all_cards:
                        final_text = build_rich_property_recommendation_text(
                            cards=all_cards,
                            language=language,
                            is_alternative=False,
                            is_sparse=True,
                            site_origin=conversation.site_origin
                        )
            else:
                final_text = resp_msg.content or ""
                if not final_text:
                    final_text = get_off_topic_text(language, site_origin=conversation.site_origin, user_message=user_message)

            if brochure_property_id:
                resp_type = "brochure"
            else:
                resp_type = "property_cards" if all_cards else "text"

            final_text = ensure_structured_response(final_text, cards=all_cards, user_message=raw_user_message or user_message, language=language)

            final_text = ThaiPropertyLegalGuardrail.sanitize_response(
                user_message=raw_user_message or user_message,
                response_text=final_text,
                language=language
            )

            quick_replies = self._generate_quick_replies(user_message, all_cards, language=language)

            # Extract and update user preferences
            search_args = {}
            for tc in logged_tool_calls:
                if tc.get("tool") == "search_properties":
                    search_args = tc.get("args") or {}
                    break

            updated_prefs = extract_user_preferences(
                user_message=user_message,
                stored_preferences=conversation.preferences,
                tool_args=search_args
            )
            if all_cards:
                card_ids = [c["property_id"] for c in all_cards if c.get("property_id")]
                seen_ids = get_session_seen_property_ids(conversation)
                updated_prefs["seen_property_ids"] = list(set(seen_ids + card_ids))
                updated_prefs["last_result_ids"] = card_ids
                # Persist active property context so "does it have a pool?" follow-ups work
                _pctx = load_property_context(conversation)
                names = {
                    str(c.get("property_id")): str(c.get("name") or c.get("property_name") or "")
                    for c in all_cards if c.get("property_id")
                }
                _pctx = touch_discussed(_pctx, card_ids, names, refresh_ordinals=True)
                if len(card_ids) == 1:
                    _pctx = set_active_property(_pctx, card_ids[0], reference=names.get(card_ids[0]))
                elif card_ids:
                    # Location / single-focus follow-ups after multi-card: keep first as soft active
                    # only if user message is about one property via "it/this"
                    um = (raw_user_message or user_message or "").lower()
                    if any(k in um for k in ["it ", " its", "this property", "that property", "this villa", "where is it", "located"]):
                        _pctx = set_active_property(_pctx, card_ids[0], reference=names.get(card_ids[0]))
                updated_prefs.update(save_property_context(conversation, _pctx))

            final_text = _professionalize_chat_text(final_text)

            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=raw_user_message or user_message,
                assistant_message=final_text,
                tool_calls=logged_tool_calls,
                tool_results=logged_tool_results,
                structured_data={"properties": all_cards, "property_ids": [c["property_id"] for c in all_cards]},
                new_preferences=updated_prefs
            )

            # Token Tracking & Observability
            t1_p = response.usage.prompt_tokens if (response and response.usage) else 0
            t1_c = response.usage.completion_tokens if (response and response.usage) else 0
            t2_p = second_resp.usage.prompt_tokens if (tool_calls and 'second_resp' in locals() and second_resp and second_resp.usage) else 0
            t2_c = second_resp.usage.completion_tokens if (tool_calls and 'second_resp' in locals() and second_resp and second_resp.usage) else 0
            tot_p = t1_p + t2_p
            tot_c = t1_c + t2_c
            num_calls = 2 if tool_calls else 1
            lat = round(time.time() - start_time, 3) if start_time else 0.0

            token_tracker.log_turn(
                TurnTokenMetrics(
                    request_id=f"req_{uuid.uuid4().hex[:8]}",
                    session_id=conversation.session_id,
                    conversation_id=str(conversation.id),
                    intent="tool_invocation",
                    model=self.model,
                    prompt_tokens=tot_p,
                    completion_tokens=tot_c,
                    total_tokens=tot_p + tot_c,
                    llm_calls_count=num_calls,
                    latency_sec=lat
                ),
                db=db
            )

            return {
                "type": resp_type,
                "text": final_text,
                "properties": all_cards,
                "quick_replies": quick_replies,
                "lead_captured": lead_captured,
                "preferences": conversation.preferences or updated_prefs,
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": language,
                "brochure_property_id": brochure_property_id
            }

        except Exception as e:
            logger.error(f"OpenAI error: {e}", exc_info=True)
            # Fall back safely to deterministic retrieval without crashing
            return self._run_deterministic_retrieval(db, conversation, user_message, raw_user_message=raw_user_message, start_time=start_time, language=language)

    def _run_deterministic_retrieval(
        self,
        db: Session,
        conversation: Conversation,
        user_message: str,
        raw_user_message: Optional[str] = None,
        start_time: Optional[float] = None,
        language: str = "en"
    ) -> Dict[str, Any]:
        """
        Deterministic, safe rule-based retrieval that runs identically when OpenAI API key
        is not yet configured or during an API timeout.
        """
        u_lower = user_message.lower()
        if is_greeting_or_chit_chat(user_message):
            return self._handle_greeting(db, conversation, raw_user_message or user_message, language=language)

        stored_prefs = dict(conversation.preferences or {})

        # Check for ID lookup
        id_match = re.search(r"prop-[a-z0-9\-]+", u_lower)
        if id_match:
            pid = id_match.group(0).upper()
            prop = get_property_by_id(db, pid)
            if prop:
                card = format_property_card(prop)
                text = (
                    f"Oto kompletne szczegóły dla **{prop.property_name}** ({prop.property_id}):"
                    if language == "pl"
                    else f"Here are the complete details for **{prop.property_name}** ({prop.property_id}):"
                )
                append_conversation_turn(
                    db=db,
                    conversation=conversation,
                    user_message=user_message,
                    assistant_message=text,
                    structured_data={"properties": [card], "property_ids": [pid]}
                )
                return {
                    "type": "property_cards",
                    "text": text,
                    "properties": [card],
                    "quick_replies": self._generate_quick_replies(user_message, [card], language=language),
                    "lead_captured": False,
                    "preferences": conversation.preferences or {},
                    "session_id": conversation.session_id,
                    "conversation_id": str(conversation.id),
                    "language": language
                }
            else:
                text = (
                    f"Nie mogłem znaleźć nieruchomości o ID '{pid}' w naszej zweryfikowanej bazie danych."
                    if language == "pl"
                    else f"I could not find property ID '{pid}' in our verified database."
                )
                append_conversation_turn(db, conversation, user_message, text)
                return {
                    "type": "text",
                    "text": text,
                    "properties": [],
                    "quick_replies": self._generate_quick_replies(user_message, [], language=language),
                    "lead_captured": False,
                    "preferences": conversation.preferences or {},
                    "session_id": conversation.session_id,
                    "conversation_id": str(conversation.id),
                    "language": language
                }

        # Multi-turn preference resolution
        # Multi-turn preference resolution
        # Location: if user mentions a new location or change (e.g. Bangkok, Samui, Phuket), cleanly replace it
        loc = None
        thai_destinations = [
            "bang tao", "layan", "karon", "rawai", "kamala", "surin", "patong", "kata", "nai harn",
            "phuket", "maenam", "bophut", "chaweng", "lamai", "choeng mon", "plai laem", "samui",
            "koh samui", "phangan", "koh phangan", "pattaya", "bangkok", "hua hin", "krabi", "chiang mai", "phang nga"
        ]
        for cand in thai_destinations:
            if cand in u_lower:
                loc = cand.title()
                break
        
        # Check if user mentioned an un-indexed location like "in antarctica" or "in chiang mai"
        if not loc:
            loc_match = re.search(r"(?:in|at|near|around)\s+([a-zA-Z\s]+)", u_lower)
            if loc_match:
                extracted_loc = loc_match.group(1).strip()
                # Exclude common adjectives or non-location terms
                if extracted_loc not in ["a", "the", "my", "our", "thailand", "pool", "private pool", "villa", "condo", "terms", "area"]:
                    loc = extracted_loc

        # If user explicitly asks for something else (e.g. "actually show me bangkok instead")
        if loc:
            # Overwrite previously stored location
            stored_prefs["location"] = loc
        elif "location" in stored_prefs:
            loc = stored_prefs["location"]

        # Intercept foreign inquiries directly in deterministic retrieval
        foreign_locs = [
            "india", "indie", "indii", "mumbai", "delhi", "bangalore", "goa", "dubai", "warsaw", "warszawa",
            "poland", "polsce", "spain", "hiszpania", "miami", "florida", "london", "londyn", "bali", "cyprus",
            "greece", "italy", "portugal", "france", "turkey", "germany", "singapore", "australia", "canada", "usa", "uk"
        ]
        is_foreign = any(re.search(r'\b' + re.escape(fl) + r'\b', u_lower) for fl in foreign_locs) or (loc and any(fl in loc.lower() for fl in foreign_locs))
        if is_foreign:
            text = get_off_topic_text(language, site_origin=conversation.site_origin, user_message=user_message)
            append_conversation_turn(db, conversation, user_message, text)
            return {
                "type": "text",
                "text": text,
                "properties": [],
                "quick_replies": self._generate_quick_replies(user_message, [], language=language),
                "lead_captured": False,
                "preferences": conversation.preferences or {},
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": language
            }

        # Property Type
        p_type = None
        if "villa" in u_lower or "willa" in u_lower or "wille" in u_lower:
            p_type = "Villa"
        elif "condo" in u_lower or "apartment" in u_lower or "apartament" in u_lower or "mieszkanie" in u_lower:
            p_type = "Condominium"
        elif "property_type" in stored_prefs:
            p_type = stored_prefs["property_type"]

        # Bedrooms
        bed = None
        bed_match = re.search(r"(\d+)\s*(?:bed|bedroom|sypialn|pokoj)", u_lower)
        if bed_match:
            bed = int(bed_match.group(1))
        elif "bedrooms" in stored_prefs:
            bed = stored_prefs["bedrooms"]

        # Price / Budget
        max_p = None
        price_match = re.search(r"(?:under|below|max|budget|poniżej|do)\s*(\d+(?:\.\d+)?)\s*(?:m|million|mln)?\s*(?:thb)?", u_lower)
        if price_match:
            num = float(price_match.group(1))
            max_p = Decimal(str(int(num * 1000000))) if num < 500 else Decimal(str(int(num)))
        elif "max_price" in stored_prefs:
            max_p = Decimal(str(stored_prefs["max_price"]))

        has_pool = True if ("pool" in u_lower or "basen" in u_lower) else None

        # Guard: If query has neither filters nor property/Thailand intent, refuse off-topic instead of dumping random villas
        search_intent_terms = [
            "property", "properties", "villa", "villas", "condo", "condos", "apartment", "apartments",
            "willa", "wille", "mieszkanie", "mieszkania", "nieruchomoś", "nieruchomos", "dom", "domy",
            "thailand", "thai", "tajlandia", "tajlandii", "phuket", "samui", "phangan", "bangkok",
            "invest", "inwest", "buy", "kupić", "kupic", "zakup", "rent", "wynajem",
            "show", "pokaż", "pokaz", "find", "search", "szukaj", "ofert", "listing", "listings",
            "pool", "basen", "bedroom", "sypialn", "sea view", "beach"
        ]
        has_intent = (
            bool(loc or p_type or bed or max_p or has_pool) or
            any(k in u_lower for k in search_intent_terms)
        )
        if not has_intent:
            text = get_off_topic_text(language, site_origin=conversation.site_origin, user_message=user_message)
            append_conversation_turn(db, conversation, user_message, text)
            return {
                "type": "text",
                "text": text,
                "properties": [],
                "quick_replies": self._generate_quick_replies(user_message, [], language=language),
                "lead_captured": False,
                "preferences": conversation.preferences or {},
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": language
            }

        # Execute safe query
        seen_ids = get_session_seen_property_ids(conversation)
        params = PropertyFilterParams(
            location=loc,
            property_type=p_type,
            bedrooms=bed,
            max_price=max_p,
            has_pool=has_pool,
            limit=4
        )
        cards = search_properties_unified(db, params, limit=4, exclude_property_ids=None)

        # Smart fresh criteria recovery: If merged search yielded 0 results because of stale constraints
        # from earlier turns, retry with ONLY the fresh criteria specified in this turn.
        has_fresh_criteria = bool(loc or ("villa" in u_lower or "condo" in u_lower or "apartment" in u_lower) or bed_match or price_match or has_pool)
        if not cards and has_fresh_criteria:
            fresh_loc = loc
            fresh_type = "Villa" if ("villa" in u_lower or "willa" in u_lower) else ("Condominium" if ("condo" in u_lower or "apartment" in u_lower) else None)
            fresh_bed = int(bed_match.group(1)) if bed_match else None
            fresh_max_p = max_p if price_match else None
            fresh_pool = True if ("pool" in u_lower or "basen" in u_lower) else None

            fresh_params = PropertyFilterParams(
                location=fresh_loc,
                property_type=fresh_type,
                bedrooms=fresh_bed,
                max_price=fresh_max_p,
                has_pool=fresh_pool,
                limit=4
            )
            matched_fresh = search_properties_unified(db, fresh_params, limit=4, exclude_property_ids=None)
            if matched_fresh:
                cards = matched_fresh
                loc, p_type, bed, max_p, has_pool = fresh_loc, fresh_type, fresh_bed, fresh_max_p, fresh_pool

        # Extract contact information
        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", user_message)
        has_email = email_match.group(0) if email_match else None
        
        phone_match = re.search(r"(?:\+?\d{1,3}[- ]?)?\(?\d{3}\)?[- ]?\d{3}[- ]?\d{4}", user_message)
        has_phone = phone_match.group(0) if phone_match else None

        lead_captured = False
        if has_email or has_phone or max_p:
            record_or_update_lead(
                db=db,
                conversation=conversation,
                contact_email=has_email,
                contact_phone=has_phone,
                preferred_location=loc,
                budget_max=max_p,
                buying_signal=f"Extracted from message: {user_message[:100]}"
            )
            lead_captured = bool(has_email or has_phone)

        # Build response text strictly grounded in data
        if cards:
            is_sparse = (not loc) or (not max_p) or (not bed)
            reply_text = build_rich_property_recommendation_text(
                cards=cards,
                language=language,
                is_alternative=False,
                is_sparse=is_sparse,
                searched_location=loc,
                searched_bedrooms=bed,
                searched_budget=float(max_p) if max_p else None,
                site_origin=conversation.site_origin
            )
        else:
            # No production match — honest EN/PL/TH fallback, never guess listings
            cards = []

            def _safe_int(val):
                if not val:
                    return None
                try:
                    return int(val)
                except (ValueError, TypeError):
                    m = re.search(r'\d+', str(val))
                    return int(m.group(0)) if m else None

            def _safe_float(val):
                if not val:
                    return None
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return None

            reply_text = get_zero_match_text(
                language=language,
                searched_location=loc,
                searched_budget_max=_safe_float(max_p),
                searched_bedrooms=_safe_int(bed),
                fallback_properties=[],
                site_origin=conversation.site_origin
            )

        if lead_captured:
            contact_parts = []
            if has_email:
                contact_parts.append(f"Email: {has_email}")
            if has_phone:
                contact_parts.append(f"Phone: {has_phone}")
            c_str = f" ({', '.join(contact_parts)})" if contact_parts else ""
            if language == "th":
                reply_text += f"\n\nบันทึกข้อมูลติดต่อของคุณเรียบร้อยแล้วครับ{c_str} ที่ปรึกษาอสังหาริมทรัพย์จะติดต่อกลับในไม่ช้าเพื่อช่วยดูแลการค้นหาและยืนยันการนัดหมายเข้าชมโครงการครับ"
            elif language == "pl":
                reply_text += f"\n\nZarejestrowałem Twoje dane kontaktowe{c_str}. Starszy doradca ds. nieruchomości skontaktuje się z Tobą wkrótce, aby pomóc w wyborze i potwierdzić szczegóły."
            else:
                reply_text += f"\n\nThank you! I have recorded your contact details{c_str}. A senior property advisor will reach out shortly to assist with your search and confirm your viewing schedule."

        # Extract and update user preferences
        updated_prefs = extract_user_preferences(
            user_message=user_message,
            stored_preferences=conversation.preferences,
            tool_args={
                "location": loc,
                "property_type": p_type,
                "bedrooms": bed,
                "max_price": float(max_p) if max_p else None,
                "has_pool": has_pool
            }
        )
        if cards:
            updated_prefs["seen_property_ids"] = list(set(seen_ids + [c["property_id"] for c in cards if c.get("property_id")]))

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=raw_user_message or user_message,
            assistant_message=reply_text,
            structured_data={"properties": cards, "property_ids": [c["property_id"] for c in cards]},
            new_preferences=updated_prefs
        )

        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="deterministic_fallback",
                model="deterministic",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                llm_calls_count=0,
                latency_sec=round(time.time() - start_time, 3) if start_time else 0.0
            ),
            db=db
        )

        return {
            "type": "property_cards" if cards else "text",
            "text": reply_text,
            "properties": cards,
            "quick_replies": self._generate_quick_replies(user_message, cards, language=language),
            "lead_captured": lead_captured,
            "preferences": conversation.preferences or updated_prefs,
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language,
            "brochure_property_id": None
        }

    def _generate_quick_replies(
        self,
        user_msg: str,
        properties: List[Dict[str, Any]],
        language: str = "en"
    ) -> List[str]:
        return get_quick_replies(language=language, has_properties=bool(properties), user_message=user_msg)


public_agent = PublicAgentService()
