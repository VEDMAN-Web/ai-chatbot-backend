import time
import uuid
import re
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from openai import OpenAI
from app.core.config import settings
from app.services.property_search import (
    PropertyFilterParams, search_properties_safe, get_property_by_id, get_property_by_name,
    to_compact_projection, get_properties_by_ids
)
from app.services.lead_service import record_or_update_lead, enqueue_lead_save
from app.services.conversation_service import append_conversation_turn
from app.services.maps_service import (
    build_google_maps_url,
    build_google_maps_embed_url,
    get_property_proximity_landmarks
)
from app.services.intent_router import (
    IntentType, detect_deterministic_intent, extract_search_filters_deterministic
)
from app.services.cache_service import search_cache
from app.services.token_tracker import token_tracker, TurnTokenMetrics
from app.services.i18n_service import (
    resolve_language, get_compact_system_prompt, get_public_system_prompt,
    get_language_switch_confirmation, get_greeting_text, get_off_topic_text,
    get_thailand_only_foreign_reply,
    get_zero_match_text, get_quick_replies, get_data_unavailable_fallback,
    build_no_match_reply, build_name_not_found_reply, build_seen_all_reply, build_amenity_none_match_reply, get_brochure_unavailable_text,
    build_consultative_followup_questions, build_encouraging_alternative_intro,
    build_rich_property_recommendation_text,
    get_cannot_email_or_calendar_text,
)
from app.services.reply_validator import validate_reply
from app.core.tenant import resolve_tenant as _resolve_tenant_for_validation
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
    resolve_name_against_context,
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
7. LEAD CAPTURE & VIEWING REQUESTS: When a visitor provides contact details (email, phone, name) or asks for a callback, call `capture_lead`. When they ask to schedule a viewing or tour, call `schedule_viewing` (request only - it records the request for the advisory team; it does NOT book a calendar or send email). If a visitor asks for a brochure by email, explain that we cannot send emails from the chat, point them to the PDF brochure download button / link on the property card, and offer to record their email via `capture_lead` so the advisory team can send it. Never claim that an email was sent or a calendar event was created.
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
            "description": "Records a property viewing REQUEST for the advisory team (property, preferred date/time, name, email, phone, notes). Does NOT create a calendar event or send email. Use when the visitor asks to visit or tour a property.",
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

def _build_add_to_calendar_url(
    *,
    title: str,
    description: str = "",
    location: str = "",
) -> str:
    """Client-side 'Add to Google Calendar' link only — no Gmail/Calendar/Drive API."""
    import urllib.parse
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc) + timedelta(days=1)
    end = start + timedelta(hours=1)
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start.strftime('%Y%m%dT%H%M%SZ')}/{end.strftime('%Y%m%dT%H%M%SZ')}",
        "details": description or "Property viewing",
        "location": location or "Thailand",
    }
    return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"


def format_property_card(p: Any) -> Dict[str, Any]:
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

    # Client-side "Add to Calendar" link only (no Google Workspace API)
    cal_url = _build_add_to_calendar_url(
        title=f"Viewing: {p.property_name} ({p.property_id})",
        description=(
            f"Property viewing appointment for {p.property_name} in {loc_display}. "
            f"Ref: {p.property_id}. Price: {clean_price}."
        ),
        location=f"{p.property_name}, {loc_display}, Thailand",
    )

    card = {
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
        "highlights": list(p.highlights or [])[:8],
        "amenities": list(getattr(p, "amenities", None) or []),
        "description": getattr(p, "description", None) or "",
    }
    # Completion / handover date from ORM or metadata
    _meta = getattr(p, "raw_metadata", None)
    completion_raw = (
        getattr(p, "completion_date", None)
        or getattr(p, "completion", None)
        or (_meta.get("completionDate") if isinstance(_meta, dict) else None)
        or (_meta.get("completion_date") if isinstance(_meta, dict) else None)
    )
    if completion_raw:
        card["completion_date"] = str(completion_raw)
        stale = format_stale_completion_note(str(completion_raw), language="en")
        if stale:
            card["completion_note"] = stale
            card["status_note"] = stale
    try:
        from app.services.amenity_service import enrich_card_amenity_flags
        from app.services.listing_presentation import apply_presentation_to_card, clean_highlights_for_display
        enrich_card_amenity_flags(card)
        apply_presentation_to_card(card, language="en")
        # Single amenity helper path + chip dedupe
        card["highlights"] = clean_highlights_for_display(
            card.get("highlights") or [], prop=card, language="en", max_items=5
        )
        card["amenity_chips"] = list(card["highlights"])
    except Exception:
        pass
    return card


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


_SLUG_LIKE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$", re.IGNORECASE)
_EXPLICIT_DETAIL_TOKEN_RE = re.compile(
    r"(?:view\s+)?(?:details|specs|info)\s+(?:for|about|on)\s+(?:the\s+)?"
    r"([a-z0-9][\w\-'&.]{2,120})",
    re.IGNORECASE,
)
_ORDINAL_DETAIL_TOKENS = frozenset({
    "first", "second", "third", "fourth", "fifth",
    "1st", "2nd", "3rd", "4th", "5th", "1", "2", "3", "4", "5",
    "pierwsza", "druga", "trzecia", "อันแรก", "อันที่สอง", "อันที่สาม",
})


def extract_explicit_property_token(
    message: str,
    args: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Pull an explicit slug or property name from the user message / tool args.
    Used so 'View details for <slug>' never falls back to the active property.
    """
    args = args or {}
    if args.get("explicit_property_token"):
        tok = str(args.get("explicit_property_token") or "").strip(" .,?!")
        if tok and tok.lower() not in _ORDINAL_DETAIL_TOKENS:
            return tok

    msg = (message or "").strip()
    m = _EXPLICIT_DETAIL_TOKEN_RE.search(msg)
    if m:
        tok = m.group(1).strip(" .,?!")
        if tok and tok.lower() not in _ORDINAL_DETAIL_TOKENS:
            return tok

    for key in ("property_id", "property_name"):
        val = str(args.get(key) or "").strip(" .,?!")
        if not val or val.lower() in _ORDINAL_DETAIL_TOKENS:
            continue
        if val.upper().startswith("PROP-") or _SLUG_LIKE_RE.match(val):
            return val
        # Exact name passed as property_name when message also names it
        if key == "property_name" and len(val) >= 3 and val.lower() in msg.lower():
            return val
        # Named-fact questions pass the human name as property_id too
        if args.get("named_fact") and len(val) >= 3 and val.lower() in msg.lower():
            return val
    return None


def lookup_property_by_slug_or_name(token: str) -> Optional[Dict[str, Any]]:
    """
    Resolve slug / exact name via Firestore id+slug fields and the name index.
    Never invents a hit. Returns a normalized property card dict or None.
    """
    raw = (token or "").strip()
    if not raw or len(raw) < 2:
        return None
    try:
        from app.services.firestore_service import get_property_by_id_from_firestore
        fs = get_property_by_id_from_firestore(raw)
        if fs:
            return fs
    except Exception as e:
        logger.warning(f"Firestore slug/name lookup failed for '{raw}': {e}")

    try:
        from app.services.property_name_index import (
            ensure_name_index_from_firestore,
            match_property_name,
        )
        from app.services.firestore_service import get_property_by_id_from_firestore

        ensure_name_index_from_firestore()
        for q in (raw, raw.replace("-", " "), raw.replace("_", " ")):
            mr = match_property_name(q)
            if mr.confident and mr.confident.doc_id:
                fs = get_property_by_id_from_firestore(mr.confident.doc_id)
                if fs:
                    return fs
    except Exception as e:
        logger.warning(f"Name-index slug lookup failed for '{raw}': {e}")
    return None


def resolve_empty_search_after_seen_exclusion(
    db: Any,
    params: PropertyFilterParams,
    seen_ids: List[str],
    *,
    language: str = "en",
    site_origin: Optional[str] = None,
    criteria_summary: Optional[str] = None,
) -> Tuple[str, List[Dict[str, Any]], str]:
    """
    When a search with seen-ID exclusion returned no cards, distinguish:
    - seen_all: matches exist but were all already shown
    - no_match: nothing matches the filters at all
    Returns (reply_text, cards, reason).
    """
    all_matching = search_properties_unified(
        db, params, limit=100, exclude_property_ids=None
    )
    if not all_matching:
        return (
            build_no_match_reply(
                language=language,
                site_origin=site_origin,
                criteria_summary=criteria_summary,
            ),
            [],
            "no_match",
        )
    seen_set = {str(s).strip().lower() for s in (seen_ids or []) if s}
    unseen = [
        c for c in all_matching
        if str(c.get("property_id") or "").strip().lower() not in seen_set
    ]
    if unseen:
        return ("", unseen[:4], "unseen_remaining")
    n = len(all_matching)
    return (
        build_seen_all_reply(
            language=language,
            site_origin=site_origin,
            criteria_summary=criteria_summary,
            total_matching=n,
        ),
        [],
        "seen_all",
    )


def search_properties_unified(
    db: Any,
    params: PropertyFilterParams,
    limit: Optional[int] = None,
    exclude_property_ids: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Unified property search engine that combines Firestore records
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
            req_am = list(getattr(params, "required_amenities", None) or [])
            # Legacy pool/beach flags also implied by canonical amenity requests
            has_pool_flag = params.has_pool
            beach_flag = params.beach_access
            if "pool" in req_am or "pool_private" in req_am:
                has_pool_flag = True
            if "beach_access" in req_am:
                beach_flag = True
            fs_cards = search_properties_firestore(
                location=params.location,
                property_type=params.property_type,
                min_price=float(params.min_price) if params.min_price else None,
                max_price=float(params.max_price) if params.max_price else None,
                bedrooms=int(params.bedrooms) if params.bedrooms and str(params.bedrooms).isdigit() else None,
                has_pool=has_pool_flag,
                beach_access=beach_flag,
                required_amenities=req_am or None,
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


def message_asks_email_or_calendar_action(message: str) -> bool:
    """
    True when the visitor asks the bot to email them something or add an event
    to their calendar (actions the public chat cannot perform).
    """
    msg = (message or "").strip().lower()
    if not msg:
        return False
    email_ask = bool(re.search(
        r"(?:\bemail\s+me\b|\bsend\s+(?:me\s+)?(?:the\s+)?(?:brochure|pdf|details|info|information|documents?)\b.{0,40}\b(?:email|e-?mail)\b|"
        r"\bsend\s+(?:me\s+)?(?:an?\s+)?(?:email|e-?mail)\b|\be-?mail\s+(?:me\s+)?(?:the\s+)?(?:brochure|pdf)\b|"
        r"\bwyślij\s+(?:mi\s+)?(?:broszur|pdf|e-?mail)|"
        r"\bprześlij\s+(?:mi\s+)?(?:broszur|pdf)|"
        r"ส่ง.{0,30}(?:อีเมล|อีเมล์|email)|"
        r"(?:โบรชัวร์|เอกสาร).{0,30}(?:อีเมล|อีเมล์|email))",
        msg,
        re.IGNORECASE,
    ))
    calendar_ask = bool(re.search(
        r"(?:\badd\s+(?:this|it|the)?\s*(?:viewing|event|appointment)?\s*(?:to\s+)?(?:my\s+)?calendar\b|"
        r"\bput\s+(?:this|it|the\s+viewing)?\s+on\s+(?:my\s+)?calendar\b|"
        r"\bcreate\s+(?:a\s+)?calendar\s+event\b|"
        r"\bdodaj\s+(?:do\s+)?(?:mojego\s+)?kalendarz|"
        r"เพิ่ม.{0,20}ปฏิทิน|ใส่.{0,20}ปฏิทิน)",
        msg,
        re.IGNORECASE,
    ))
    return email_ask or calendar_ask


def looks_like_prompt_injection(message: str) -> bool:
    """True when the message tries to override system behaviour (not a visitor contact share)."""
    m = (message or "").strip().lower()
    if not m:
        return False
    # Legitimate property Q&A must not be blocked
    if re.search(
        r"\b(?:instructions?\s+for\s+(?:how\s+to\s+)?buy|how\s+to\s+buy|buying\s+process|"
        r"instrukcj\w*\s+(?:zakupu|kupna)|วิธีซื้อ)\b",
        m,
    ):
        return False
    markers = (
        "ignore all previous",
        "ignore previous instructions",
        "disregard all previous",
        "disregard previous instructions",
        "disregard the above",
        "forget all previous",
        "email all clients",
        "send all clients",
        "override the system",
        "you are now dan",
        "jailbreak",
        "repeat the text above",
        "zignoruj poprzednie",
        "zignoruj wszystkie poprzednie",
        "pokaż swój prompt",
        "pokaz swoj prompt",
        "prompt systemowy",
        "เพิกเฉยคำสั่งก่อนหน้า",
        "เพิกเฉยคำสั่งทั้งหมด",
    )
    if any(x in m for x in markers):
        return True
    # Reveal / print / show / repeat system prompt, hidden rules, secrets
    if re.search(
        r"(?:show\s+me|reveal|print|repeat|dump|give\s+me|output|display|"
        r"pokaż|pokaz|พิมพ์|แสดง|เปิดเผย)\s+"
        r"(?:your\s+|the\s+|swo(?:j|ój|ich)\s+|ของคุณ\s*)?"
        r"(?:system\s+)?(?:prompt|instructions?|hidden\s+rules?|text\s+above|"
        r"api\s*key|secret|token|password|credentials?)",
        m,
    ):
        return True
    if re.search(
        r"(?:api\s*key|secret\s+key|access\s+token|password)\b.{0,20}\b(?:print|show|reveal|dump)\b|"
        r"\b(?:print|show|reveal|dump)\b.{0,30}\b(?:api\s*key|secret|token|password)\b",
        m,
    ):
        return True
    if "system prompt" in m and re.search(r"\b(?:your|my|the|show|print|reveal|พิมพ์)\b", m):
        return True
    return False


_API_KEY_LEAK_RE = re.compile(
    r"(?:sk-[A-Za-z0-9]{10,}|AIza[0-9A-Za-z\-_]{20,}|"
    r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----|"
    r"-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----)",
    re.IGNORECASE,
)


def redact_sensitive_output(text: str) -> str:
    """Replace leaked API keys / private-key blocks in assistant text."""
    if not text or not isinstance(text, str):
        return text
    out = _API_KEY_LEAK_RE.sub("[redacted]", text)
    # Collapse multi-line private key remnants
    out = re.sub(
        r"-----BEGIN[^-]+-----.*?-----END[^-]+-----",
        "[redacted]",
        out,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return out


def format_stale_completion_note(
    completion_raw: Optional[str],
    *,
    language: str = "en",
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    If a planned completion date (e.g. 'Q4 2025') is in the past, return an honest note.
    """
    from datetime import datetime as _dt, timezone as _tz

    raw = (completion_raw or "").strip()
    if not raw:
        return None
    ref = now or _dt.now(_tz.utc)
    end: Optional[_dt] = None
    qm = re.search(r"\bQ([1-4])\s*[/\-]?\s*(20\d{2})\b", raw, re.I)
    if qm:
        q, year = int(qm.group(1)), int(qm.group(2))
        month = q * 3
        # End of quarter
        day = 28 if month == 2 else 30 if month in (4, 6, 9, 11) else 31
        try:
            end = _dt(year, month, min(day, 28 if month == 2 else day), tzinfo=_tz.utc)
        except ValueError:
            end = _dt(year, month, 28, tzinfo=_tz.utc)
    else:
        ym = re.search(r"\b(20\d{2})\b", raw)
        if ym and re.search(r"(?:complet|ready|handover|planned|q[1-4])", raw, re.I):
            try:
                end = _dt(int(ym.group(1)), 12, 31, tzinfo=_tz.utc)
            except ValueError:
                end = None
    if end is None or end >= ref:
        return None
    if language == "th":
        return (
            f"วันที่แล้วเสร็จที่ระบุ ({raw}) ผ่านมาแล้ว — "
            "โปรดยืนยันสถานะล่าสุดกับทีมที่ปรึกษา"
        )
    if language == "pl":
        return (
            f"Podana data ukończenia ({raw}) już minęła — "
            "potwierdź aktualny status z zespołem doradców"
        )
    return (
        f"The stated completion date ({raw}) has passed — "
        "please confirm the current status with our advisory team"
    )


def _bedroom_configs_from_description(description: Optional[str]) -> Optional[str]:
    """Pull bedroom configuration phrases from verified description text."""
    if not description:
        return None
    m = re.search(
        r"(\d+\s*[–\-]\s*\d+\s*bedrooms?|\d+\s*,\s*\d+(?:\s*,\s*\d+)*\s*(?:and\s+)?\d*\s*bedroom\s+configurations?|"
        r"(?:available|offered|units?)\s+in\s+\d+[^\n.]{0,40}bedroom)",
        description,
        re.I,
    )
    if m:
        return m.group(0).strip(" .,")
    m2 = re.search(r"\b(\d+\s*[–\-to]+\s*\d+)\s*bedrooms?\b", description, re.I)
    return m2.group(0).strip() if m2 else None


def extract_contact_email_from_message(message: str) -> Optional[str]:
    if looks_like_prompt_injection(message):
        return None
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", message or "")
    return m.group(0) if m else None


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
        "ขอบคุณค่ะ", "แต๊งกิ้ว", "คุณคือใคร", "ทำอะไรได้บ้าง", "ช่วยอะไรได้บ้าง", "ช่วยด้วย", "ช่วยหน่อย",
        "สบายดีไหม", "เป็นไงบ้าง", "อย่างไรบ้าง",
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

    # 5a. Fuzzy Thailand city (puket / phucket / pattya) is on-topic search context
    try:
        from app.services.intent_router import fuzzy_resolve_location, message_rejects_active_property
        if fuzzy_resolve_location(msg):
            return False
        if message_rejects_active_property(msg):
            return False
    except Exception:
        pass

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
        "which one", "show me more", "any more", "any other", "show me other", "show me others",
        "different ones", "something else", "instead of this", "don't like this", "dont like this",
        "how much", "what about the", "floor plan",
        "brochure", "viewing", "schedule", "cheaper", "more options", "compare",
        "pierwsz", "drug", "trzec", "więcej o", "wiecej o", "opowiedz", "szczegół",
        "ta oferta", "ta willa", "อันแรก", "อันที่สอง", "รายละเอียด", "เพิ่มเติม", "เปรียบเทียบ",
        "อย่างอื่น", "รายการอื่น", "inne oferty", "coś innego",
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


def _openai_timeout_sec() -> float:
    try:
        return float(getattr(settings, "OPENAI_TIMEOUT", 25.0) or 25.0)
    except Exception:
        return 25.0


def _openai_max_retries() -> int:
    try:
        return int(getattr(settings, "OPENAI_MAX_RETRIES", 1) or 0)
    except Exception:
        return 1


def _is_openai_timeout_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timed out" in msg or "timeout" in msg:
        return True
    try:
        import openai
        timeout_cls = getattr(openai, "APITimeoutError", None)
        if timeout_cls is not None and isinstance(exc, timeout_cls):
            return True
    except Exception:
        pass
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
    5. Applies OPENAI_TIMEOUT; on timeout does not cascade through model fallbacks (caller uses honest fallback).
    """
    call_kwargs = dict(kwargs)
    model_name = str(call_kwargs.get("model", "")).lower()
    timeout_sec = _openai_timeout_sec()

    is_reasoning_or_advanced = any(prefix in model_name for prefix in ["gpt-5", "gpt-6", "o1", "o3", "o4"])

    if is_reasoning_or_advanced:
        if "max_tokens" in call_kwargs:
            call_kwargs["max_completion_tokens"] = call_kwargs.pop("max_tokens")
        if "temperature" in call_kwargs and call_kwargs["temperature"] != 1:
            call_kwargs.pop("temperature", None)
        # OpenAI requires reasoning_effort="none" when function tools are used in /v1/chat/completions
        if call_kwargs.get("tools") or call_kwargs.get("functions"):
            call_kwargs["reasoning_effort"] = "none"

    call_kwargs.setdefault("timeout", timeout_sec)

    t0 = time.monotonic()
    try:
        # Client-level OPENAI_MAX_RETRIES handles one retry on timeout/transient errors.
        resp = client.chat.completions.create(**call_kwargs)
        logger.info(
            "timing.openai_call duration_sec=%.3f model=%s",
            time.monotonic() - t0,
            call_kwargs.get("model"),
        )
        return resp
    except Exception as e:
        if _is_openai_timeout_error(e):
            logger.warning(
                "OpenAI timeout after %.3fs (timeout=%ss, max_retries=%s): %s",
                time.monotonic() - t0,
                timeout_sec,
                _openai_max_retries(),
                e,
            )
            raise
        err_str = str(e).lower()
        logger.warning(f"Initial OpenAI API call for model '{model_name}' encountered: {e}. Applying self-healing...")

        if "reasoning_effort" in err_str:
            call_kwargs["reasoning_effort"] = "none"
        if "max_tokens" in err_str and "max_tokens" in call_kwargs:
            call_kwargs["max_completion_tokens"] = call_kwargs.pop("max_tokens")
        if "temperature" in err_str:
            call_kwargs.pop("temperature", None)

        try:
            t1 = time.monotonic()
            resp = client.chat.completions.create(**call_kwargs)
            logger.info(
                "timing.openai_call duration_sec=%.3f model=%s attempt=selfheal",
                time.monotonic() - t1,
                call_kwargs.get("model"),
            )
            return resp
        except Exception as retry_err:
            if _is_openai_timeout_error(retry_err):
                raise
            logger.error(f"Retry for model '{model_name}' failed: {retry_err}. Attempting fallback to gpt-4o...")
            # Fallback to gpt-4o for zero downtime (not used for timeouts)
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["model"] = "gpt-4o"
            fallback_kwargs.setdefault("timeout", timeout_sec)
            fallback_kwargs.pop("reasoning_effort", None)
            if "max_completion_tokens" in fallback_kwargs:
                fallback_kwargs["max_tokens"] = fallback_kwargs.pop("max_completion_tokens")
            try:
                return client.chat.completions.create(**fallback_kwargs)
            except Exception as final_err:
                if _is_openai_timeout_error(final_err):
                    raise
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
        self.timeout = _openai_timeout_sec()
        self.max_retries = _openai_max_retries()
        if self.api_key:
            self.client = OpenAI(
                api_key=self.api_key,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        else:
            self.client = None

    def _validate_or_fallback(
        self,
        reply_text: str,
        cards: Optional[List[Dict[str, Any]]],
        *,
        language: str,
        site_origin: Optional[str],
        applied_filters: Optional[Dict[str, Any]] = None,
        tool_results: Optional[List[Any]] = None,
        criteria_summary: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Drop unsafe replies; return honest no-match text and empty cards on failure."""
        from app.services.reply_validator import sanitize_listing_line_text
        from app.services.listing_presentation import apply_presentation_to_card

        cards = list(cards or [])
        tenant = _resolve_tenant_for_validation(site_origin)
        # Presentation clean-up on cards (name/location/highlights/marketing)
        cleaned_cards = []
        for c in cards:
            if isinstance(c, dict):
                try:
                    apply_presentation_to_card(c, language=language)
                except Exception:
                    pass
                cleaned_cards.append(c)
        cards = cleaned_cards
        # Never repeat guaranteed-return / marketing claims as bot facts
        reply_text = sanitize_listing_line_text(reply_text or "")
        # Drop null-price cards from budget searches before validation when possible
        filters = dict(applied_filters or {})
        if filters.get("max_price") is not None or filters.get("min_price") is not None:
            kept = []
            for c in cards:
                raw = str(c.get("price") or c.get("price_display") or "").strip().lower()
                num = c.get("price_numeric_min")
                if num is None and (not raw or "request" in raw or raw in ("n/a", "na", "")):
                    continue  # exclude null-price from budget results
                kept.append(c)
            cards = kept

        ok, reasons = validate_reply(
            reply_text=reply_text,
            cards=cards,
            tool_results=tool_results,
            applied_filters=filters,
            language=language,
            tenant_advisor_email=getattr(tenant, "advisor_email", None),
        )
        if ok:
            return reply_text, cards
        logger.warning("reply_validation_failed reasons=%s", reasons)
        asked = (
            filters.get("query")
            or filters.get("property_name")
            or filters.get("asked_property_name")
            or criteria_summary
        )
        name_miss = any(str(r).startswith("named_property_missing:") for r in reasons)
        if name_miss or (asked and any(str(r).startswith("named_property_missing") for r in reasons)):
            from app.services.property_name_index import get_last_name_search_suggestions
            # Prefer suggestions carried on tool results (survives thread/context switches)
            tips: List[str] = []
            for tr in tool_results or []:
                res = tr.get("result") if isinstance(tr, dict) else None
                if not isinstance(res, dict):
                    continue
                raw_tips = res.get("name_suggestions")
                if raw_tips:
                    tips = [str(s) for s in raw_tips if s]
                    break
            if not tips:
                tips = get_last_name_search_suggestions()
            fallback = build_name_not_found_reply(
                language=language,
                site_origin=site_origin,
                asked_name=str(asked) if asked else None,
                suggestions=tips,
            )
            # Pending candidates are stored by the caller when conversation is available
            # (execute_tool / _sync_pending_name_candidates_from_last_search).
        else:
            fallback = build_no_match_reply(
                language=language,
                site_origin=site_origin,
                criteria_summary=criteria_summary,
            )
        if not fallback:
            fallback = (
                f"I could not verify that information. Our advisor can help: {tenant.advisor_email}"
            )
        return fallback, []

    def execute_tool(
        self,
        db: Any,
        conversation: Any,
        tool_name: str,
        args: Dict[str, Any]
    ) -> (Dict[str, Any], List[Dict[str, Any]], bool):
        """Safely executes structured tool calls. Returns (tool_result_dict, cards_list, lead_captured_bool)."""
        matched_cards = []
        lead_captured = False

        if tool_name == "search_properties":
            # 1. Direct query / project name check (e.g. '15 Sukhumvit Residences', 'Sea Heaven')
            query_term = args.get("query") or args.get("property_name") or args.get("project_name")
            from app.services.intent_router import (
                extract_search_filters_deterministic,
                message_looks_like_filter_search,
            )
            # Polish/EN filter phrases must not be treated as project names
            if query_term and message_looks_like_filter_search(str(query_term)):
                derived = extract_search_filters_deterministic(str(query_term))
                for k, v in derived.items():
                    if k not in args or args.get(k) in (None, "", []):
                        args[k] = v
                query_term = None
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

            tool_amenities = list(args.get("required_amenities") or [])
            if not tool_amenities:
                try:
                    from app.services.amenity_service import extract_requested_amenities
                    tool_amenities = extract_requested_amenities(
                        str(args.get("query") or args.get("property_name") or "")
                    )
                except Exception:
                    tool_amenities = []
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
                required_amenities=tool_amenities or None,
                query=query_term,
                status=args.get("status"),
                limit=6
            )
            seen_ids = get_session_seen_property_ids(conversation)
            # Do not exclude seen IDs on a fresh tool search — that caused unrelated new cards
            matched_cards = search_properties_unified(db, params, limit=6, exclude_property_ids=None)
            # Honest zero-result: never run an unfiltered / empty-filter search as fallback

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
                enqueue_lead_save(
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
            # Named search with zero confident matches: honest message + Did you mean (no invented facts)
            # Prefer values already on tool_summary; ContextVar is only a fallback (cross-thread safe).
            if query_term and not matched_cards and not message_looks_like_filter_search(str(query_term)):
                from app.services.property_name_index import (
                    get_last_name_search_suggestions,
                    get_last_name_search_candidates,
                    get_last_name_search_query,
                )
                from app.services.name_confirmation import store_pending_name_candidates
                tips = list(tool_summary.get("name_suggestions") or []) or get_last_name_search_suggestions()
                cands = list(tool_summary.get("name_candidates") or []) or get_last_name_search_candidates()
                # If search left ContextVar tips but empty candidates, recover via index
                if tips and not cands:
                    set_last_name_search_meta = __import__(
                        "app.services.property_name_index", fromlist=["set_last_name_search_meta"]
                    ).set_last_name_search_meta
                    set_last_name_search_meta(str(query_term), suggestions=tips, candidates=None)
                    cands = get_last_name_search_candidates()
                lang_pref = (conversation.preferences or {}).get("language") or "en"
                tool_summary["name_suggestions"] = tips
                tool_summary["name_candidates"] = cands
                tool_summary["message"] = build_name_not_found_reply(
                    language=str(lang_pref),
                    site_origin=getattr(conversation, "site_origin", None),
                    asked_name=str(query_term),
                    suggestions=tips,
                )
                tool_summary["properties"] = []
                if cands:
                    conversation.preferences = store_pending_name_candidates(
                        conversation.preferences,
                        query=str(query_term) or get_last_name_search_query(),
                        candidates=cands,
                    )
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
                        "ownership": (fs_prop.get("ownership") or fs_prop.get("ownership_type") or None),
                        "amenities": fs_prop.get("highlights", []),
                        "investment_info": f"Expected ROI: {fs_prop.get('expected_roi') or 'N/A'}",
                        "detail_page_url": fs_prop.get("detail_page_url")
                    }, matched_cards, False

            # 2. Fallback
            prop = get_property_by_id(db, pid) if (db and pid) else None
            if not prop and args.get("property_name") and db:
                prop = get_property_by_name(db, args["property_name"])

            if prop:
                card = format_property_card(prop)
                matched_cards = [card]
                enqueue_lead_save(
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
                enqueue_lead_save(
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
            enqueue_lead_save(
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
            # Public path: record viewing REQUEST as a lead only.
            # Never create calendar events or send confirmation emails.
            from app.services.i18n_service import get_viewing_request_recorded_text

            pid = args.get("property_id")
            prop = get_property_by_id(db, pid) if pid else None
            p_name = prop.property_name if prop else (pid or "Thailand Property")
            p_date = args.get("preferred_date") or "the requested date"
            p_time = args.get("preferred_time") or "the requested time"
            visitor_notes = (args.get("notes") or "").strip()
            lead_notes = (
                f"Viewing request for {p_name} ({pid}) on {p_date} at {p_time}."
                + (f" Notes: {visitor_notes}" if visitor_notes else "")
            )
            enqueue_lead_save(
                db=db,
                conversation=conversation,
                contact_name=args.get("customer_name"),
                contact_email=args.get("customer_email"),
                contact_phone=args.get("customer_phone"),
                notes=lead_notes,
                buying_signal=f"Viewing request for {pid} on {p_date} at {p_time}",
            )
            lang = "en"
            try:
                lang = (conversation.preferences or {}).get("language") or "en"
            except Exception:
                lang = "en"
            reply = get_viewing_request_recorded_text(
                language=lang,
                property_label=p_name,
                preferred_date=str(p_date),
                preferred_time=str(p_time),
            )
            matched_cards = [format_property_card(prop)] if prop else []
            return {
                "status": "success",
                "appointment_confirmed": False,
                "viewing_request_recorded": True,
                "property": p_name,
                "date": p_date,
                "time": p_time,
                "message": reply,
            }, matched_cards, True

        elif tool_name == "send_property_brochure":
            # Tool removed from PUBLIC_TOOLS; refuse silently if a stale model still emits it.
            from app.services.i18n_service import get_brochure_email_not_available_text
            lang = "en"
            try:
                lang = (conversation.preferences or {}).get("language") or "en"
            except Exception:
                lang = "en"
            return {
                "status": "unavailable",
                "message": get_brochure_email_not_available_text(
                    language=lang,
                    site_origin=getattr(conversation, "site_origin", None),
                ),
            }, [], False

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
            resp["text"] = redact_sensitive_output(
                _professionalize_chat_text(clean_response_text(resp["text"]))
            )
            # Never leave literal None in visitor-facing text
            resp["text"] = re.sub(r"\bNone\b", "not listed in our verified data", resp["text"])
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
        db: Any,
        conversation: Any,
        user_message: str,
        requested_language: Optional[str] = None
    ) -> Dict[str, Any]:
        """Runs one conversational turn using deterministic intent routing and token-efficient synthesis."""
        t0 = time.time()
        try:
            res = self._run_conversation_internal(
                db=db,
                conversation=conversation,
                user_message=user_message,
                requested_language=requested_language
            )
            return self._post_process_response(res, user_message=user_message)
        finally:
            logger.info(
                "timing.request_total duration_sec=%.3f session=%s",
                time.time() - t0,
                getattr(conversation, "session_id", "?"),
            )

    def _run_conversation_internal(
        self,
        db: Any,
        conversation: Any,
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

        # 0b. Did-you-mean confirmation follow-up (before greeting so "ok"/"sure" resolve)
        pending_handled = self._maybe_resolve_pending_name_candidates(
            db=db,
            conversation=conversation,
            user_message=user_message,
            start_time=start_time,
            language=active_lang,
        )
        if pending_handled is not None:
            return pending_handled

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

        # 1a. Prompt-injection / jailbreak: refuse, send nothing, do not capture attack emails
        if looks_like_prompt_injection(user_message):
            reply_text = get_off_topic_text(
                active_lang,
                site_origin=conversation.site_origin,
                user_message=user_message,
            )
            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=user_message,
                assistant_message=reply_text,
            )
            token_tracker.log_turn(
                TurnTokenMetrics(
                    request_id=f"req_{uuid.uuid4().hex[:8]}",
                    session_id=conversation.session_id,
                    conversation_id=str(conversation.id),
                    intent="prompt_injection_refused",
                    model="deterministic",
                    latency_sec=round(time.time() - start_time, 3),
                ),
                db=db,
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
                "language": active_lang,
            }

        # 1a2. Admin-taught FAQ answer (near-exact match only, vetted by grounding policy)
        try:
            from app.services.taught_answers import find_match as _find_taught_answer
            _taught = _find_taught_answer(
                user_message, site_origin=conversation.site_origin, language=active_lang
            )
        except Exception:
            _taught = None
        if _taught:
            reply_text = _taught["answer"]
            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=user_message,
                assistant_message=reply_text,
            )
            token_tracker.log_turn(
                TurnTokenMetrics(
                    request_id=f"req_{uuid.uuid4().hex[:8]}",
                    session_id=conversation.session_id,
                    conversation_id=str(conversation.id),
                    intent="taught_answer",
                    model="deterministic",
                    latency_sec=round(time.time() - start_time, 3),
                ),
                db=db,
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
                "language": active_lang,
            }

        # 1b. Visitor asks bot to email / add calendar — honest refusal (no Gmail / no Calendar)
        if message_asks_email_or_calendar_action(user_message):
            return self._handle_cannot_email_or_calendar(
                db=db,
                conversation=conversation,
                user_message=user_message,
                start_time=start_time,
                language=active_lang,
            )

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

    def _sync_pending_name_candidates_from_last_search(
        self,
        conversation: Any,
        asked_name: Optional[str] = None,
        candidates: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Any]] = None,
    ) -> None:
        """Persist Did-you-mean candidates into conversation.preferences when present."""
        from app.services.property_name_index import (
            get_last_name_search_candidates,
            get_last_name_search_query,
        )
        from app.services.name_confirmation import store_pending_name_candidates

        cands = list(candidates or [])
        if not cands and tool_results:
            for tr in tool_results:
                res = (tr or {}).get("result") if isinstance(tr, dict) else None
                if isinstance(res, dict) and res.get("name_candidates"):
                    cands = list(res.get("name_candidates") or [])
                    break
        if not cands:
            cands = get_last_name_search_candidates()
        if not cands:
            return
        conversation.preferences = store_pending_name_candidates(
            conversation.preferences,
            query=asked_name or get_last_name_search_query(),
            candidates=cands,
        )

    def _clear_pending_name_candidates(self, conversation: Any) -> None:
        from app.services.name_confirmation import clear_pending_name_candidates
        conversation.preferences = clear_pending_name_candidates(conversation.preferences)

    def _maybe_resolve_pending_name_candidates(
        self,
        db: Any,
        conversation: Any,
        user_message: str,
        start_time: float,
        language: str = "en",
    ) -> Optional[Dict[str, Any]]:
        """
        If preferences hold pending_name_candidates from a Did-you-mean turn,
        resolve yes/ordinal/name, ask which, or clear and continue.
        Returns a response dict when handled; None to continue normal routing.
        """
        from app.services.name_confirmation import (
            try_resolve_pending_name,
            clear_pending_name_candidates,
            PREF_CANDIDATES,
        )

        prefs = conversation.preferences or {}
        if not prefs.get(PREF_CANDIDATES):
            return None

        outcome = try_resolve_pending_name(user_message, prefs, language=language)
        if outcome.kind == "none":
            return None

        if outcome.kind == "clear_continue":
            conversation.preferences = clear_pending_name_candidates(prefs)
            return None

        if outcome.kind == "ask_which":
            ask_text = outcome.ask_text or "Which one did you mean?"
            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=user_message,
                assistant_message=ask_text,
                new_preferences=dict(conversation.preferences or {}),
            )
            token_tracker.log_turn(
                TurnTokenMetrics(
                    request_id=f"req_{uuid.uuid4().hex[:8]}",
                    session_id=conversation.session_id,
                    conversation_id=str(conversation.id),
                    intent="pending_name_clarify",
                    model="deterministic",
                    latency_sec=round(time.time() - start_time, 3),
                ),
                db=db,
            )
            return {
                "type": "text",
                "text": ask_text,
                "properties": [],
                "quick_replies": self._generate_quick_replies(user_message, [], language=language),
                "lead_captured": False,
                "preferences": conversation.preferences or {},
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": language,
            }

        if outcome.kind == "resolved" and outcome.property_id:
            conversation.preferences = clear_pending_name_candidates(prefs)
            return self._handle_property_details(
                db=db,
                conversation=conversation,
                args={"property_id": outcome.property_id},
                user_message=user_message,
                start_time=start_time,
                language=language,
            )

        conversation.preferences = clear_pending_name_candidates(prefs)
        return None

    def _handle_lead_capture(
        self,
        db: Any,
        conversation: Any,
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

        # Fast in-session upsert for lead_id in the reply; durable Firestore write is backgrounded.
        try:
            lead = record_or_update_lead(
                db=None,
                conversation=conversation,
                contact_name=c_name,
                contact_email=c_email,
                contact_phone=c_phone,
                preferred_location=pref_loc,
                budget_max=budget_dec,
                buying_signal=f"{'Private viewing request' if is_viewing else 'Contact details submitted'}: {c_email or c_phone or c_name}",
                notes=f"Client requested viewing/contact: {user_message}",
            )
        except Exception as le:
            logger.warning("Lead capture in-session save failed: %s", le)
            lead = type("TmpLead", (), {"id": "lead_pending"})()
        enqueue_lead_save(
            db=db,
            conversation=conversation,
            contact_name=c_name,
            contact_email=c_email,
            contact_phone=c_phone,
            preferred_location=pref_loc,
            budget_max=budget_dec,
            buying_signal=f"{'Private viewing request' if is_viewing else 'Contact details submitted'}: {c_email or c_phone or c_name}",
            notes=f"Client requested viewing/contact: {user_message}",
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
        relaxed_filter_label = None
        if not cards and pref_loc:
            # Exactly one relaxed filter: broaden sub-district to Phuket, keep type + budget
            is_phuket = any(k in (pref_loc or "").lower() for k in ["layan", "bang tao", "karon", "rawai", "kamala", "surin"])
            if is_phuket:
                params_broad = PropertyFilterParams(
                    location="Phuket",
                    property_type=args.get("property_type", "Villa"),
                    max_price=budget_dec,
                    offset=0,
                    limit=3
                )
                cards = search_properties_unified(db, params_broad, limit=3)
                if cards:
                    relaxed_filter_label = "location -> Phuket" if language == "en" else (
                        "lokalizacja -> Phuket" if language == "pl" else "ทำเล -> ภูเก็ต"
                    )
        # Never run an empty-filter search for lead confirmation listings

        # Localized confirmation
        if language == "th":
            opening = (
                f"ขอบคุณครับ{', คุณ' + c_name if c_name else ''}! "
                f"ระบบได้ลงทะเบียนการนัดหมายเข้าชมโครงการส่วนตัวสำหรับวิลล่าใน{pref_loc or 'ภูเก็ต'}เรียบร้อยแล้วครับ\n\n"
                f"ที่ปรึกษาอาวุโสของ {tenant.brand_name} ({tenant.advisor_email}) จะติดต่อกลับผ่านทาง "
                f"**{c_phone or c_email or 'ข้อมูลที่ระบุ'}** ภายใน 15-30 นาทีเพื่อยืนยันกำหนดการและอำนวยความสะดวกเรื่องการเดินทางครับ"
            )
        elif language == "pl":
            opening = (
                f"Dziękujemy{', ' + c_name if c_name else ''}! "
                f"Twoje zgłoszenie na prywatną prezentację willi w rejonie **{pref_loc or 'Phuket'}** zostało pomyślnie zarejestrowane.\n\n"
                f"Dedykowany doradca {tenant.brand_name} ({tenant.advisor_email}) skontaktuje się z Tobą pod adresem "
                f"**{c_phone or c_email or 'Twoim kontaktem'}** w celu potwierdzenia dogodnego terminu wizyty i organizacji transportu."
            )
        else:
            opening = (
                f"Thank you{', ' + c_name if c_name else ''}! "
                f"Your request for a private property viewing in **{pref_loc or 'Phuket'}** has been successfully registered.\n\n"
                f"A senior advisor from {tenant.brand_name} ({tenant.advisor_email}) will contact you directly via "
                f"**{c_phone or c_email or 'your contact details'}** to confirm your schedule, arrange private VIP transport, and prepare property access."
            )
        if cards and relaxed_filter_label:
            opening = opening + "\n\n" + build_no_match_reply(
                language=language,
                site_origin=conversation.site_origin,
                criteria_summary=pref_loc or None,
                relaxed_filter=relaxed_filter_label,
                alternatives=cards,
            )
        elif cards:
            if language == "th":
                opening += "\n\nระหว่างนี้ นี่คือโครงการที่ผ่านการตรวจสอบตรงตามเงื่อนไขของคุณ:"
            elif language == "pl":
                opening += "\n\nPoniżej oferty zweryfikowane spełniające Twoje kryteria:"
            else:
                opening += "\n\nIn the meantime, here are verified listings matching your criteria:"
        else:
            opening = opening + "\n\n" + build_no_match_reply(
                language=language,
                site_origin=conversation.site_origin,
                criteria_summary=pref_loc or None,
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
        db: Any,
        conversation: Any,
        args: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Handles pagination and 'show more' / 'any other' requests, excluding already-shown IDs."""
        stored_prefs = dict(conversation.preferences or {})
        seen_ids = list(get_session_seen_property_ids(conversation))
        cache_hit = False

        # Reject current focused property → force-exclude it for alternatives
        if args.get("reject_active"):
            pctx_reject = load_property_context(conversation)
            reject_ids = []
            if pctx_reject.active_property_id:
                reject_ids.append(str(pctx_reject.active_property_id))
            for pid in (stored_prefs.get("selected_property_ids") or []):
                if pid:
                    reject_ids.append(str(pid))
            for pid in reject_ids:
                if pid not in seen_ids:
                    seen_ids.append(pid)

        active_filters = dict(stored_prefs.get("active_filters", {}))
        # Merge NEW values from this message over stored filters; keep stored
        # only for fields the message does not mention.
        overrides = dict(args.get("filter_overrides") or {})
        if not overrides and user_message:
            # Belt-and-suspenders if intent args omitted overrides
            try:
                from app.services.intent_router import extract_search_filters_deterministic
                extracted = extract_search_filters_deterministic(user_message)
                for k in (
                    "location", "property_type", "bedrooms", "max_price",
                    "min_price", "has_pool", "required_amenities", "beach_access",
                ):
                    if k in extracted and extracted[k] is not None:
                        overrides[k] = extracted[k]
            except Exception:
                pass
        filters_changed = bool(args.get("filters_changed")) or bool(overrides)
        for k, v in overrides.items():
            active_filters[k] = v

        loc = active_filters.get("location") or stored_prefs.get("location")
        p_type = active_filters.get("property_type") or stored_prefs.get("property_type")
        bedrooms = active_filters.get("bedrooms") or stored_prefs.get("bedrooms_num")
        max_p = Decimal(str(active_filters["max_price"])) if active_filters.get("max_price") else (Decimal(str(stored_prefs["budget_max"])) if stored_prefs.get("budget_max") else None)
        has_pool = active_filters.get("has_pool", stored_prefs.get("has_pool"))
        req_amenities = list(active_filters.get("required_amenities") or [])

        # Persist merged filters so later follow-ups use the new budget/location
        stored_prefs["active_filters"] = {
            "location": loc,
            "property_type": p_type,
            "bedrooms": bedrooms,
            "max_price": float(max_p) if max_p is not None else None,
            "has_pool": has_pool,
            "required_amenities": req_amenities or None,
        }
        if loc:
            stored_prefs["location"] = loc
        if p_type:
            stored_prefs["property_type"] = p_type
        if bedrooms is not None:
            stored_prefs["bedrooms_num"] = bedrooms
        if max_p is not None:
            stored_prefs["budget_max"] = float(max_p)
            stored_prefs["max_price"] = float(max_p)
        if filters_changed:
            stored_prefs["pagination_offset"] = 0
        conversation.preferences = stored_prefs

        # Exclude already-shown only when filters did not change (pure "show more").
        # A new budget/location starts a fresh result list for those filters.
        exclude_ids = [] if filters_changed else seen_ids

        params = PropertyFilterParams(
            location=loc,
            property_type=p_type,
            bedrooms=bedrooms,
            max_price=max_p,
            has_pool=has_pool,
            required_amenities=req_amenities or None,
            limit=4,
            offset=0 if filters_changed else int(args.get("offset") or 0),
        )
        cards = search_properties_unified(db, params, limit=4, exclude_property_ids=exclude_ids)
        relaxed_filter_label = None
        used_relaxed_alternatives = False
        empty_reason = None

        # Exactly one relaxed filter when narrow sub-district yields nothing: broaden to Phuket
        if not cards and loc in ["Bang Tao", "Choeng Thale", "Layan", "Kamala", "Surin", "Rawai", "Karon", "Patong"]:
            broad_params = PropertyFilterParams(
                location="Phuket",
                property_type=p_type,
                bedrooms=bedrooms,
                max_price=max_p,
                has_pool=has_pool,
                limit=3
            )
            relaxed = search_properties_unified(db, broad_params, limit=3, exclude_property_ids=exclude_ids)
            if relaxed:
                cards = relaxed
                used_relaxed_alternatives = True
                relaxed_filter_label = "location -> Phuket" if language == "en" else (
                    "lokalizacja -> Phuket" if language == "pl" else "ทำเล -> ภูเก็ต"
                )
        # Never run empty-filter or Thailand-wide unfiltered searches as fallback

        criteria_bits = []
        if loc:
            criteria_bits.append(str(loc))
        if p_type:
            criteria_bits.append(str(p_type))
        if bedrooms:
            criteria_bits.append(f"{bedrooms} bed")
        if max_p is not None:
            criteria_bits.append(f"budget <= {max_p}")
        criteria_summary = ", ".join(criteria_bits) if criteria_bits else None

        if cards and not used_relaxed_alternatives:
            reply_text = build_rich_property_recommendation_text(
                cards=cards,
                language=language,
                is_alternative=False,
                is_sparse=(not loc) or (not max_p) or (not bedrooms),
                searched_location=loc,
                searched_bedrooms=bedrooms,
                searched_budget=float(max_p) if max_p else None,
                site_origin=conversation.site_origin
            )
            reply_text = ensure_structured_response(reply_text, cards=cards, user_message=user_message, language=language)
            new_seen = list(set(seen_ids + [c["property_id"] for c in cards]))
            stored_prefs["seen_property_ids"] = new_seen
            stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
        elif cards and used_relaxed_alternatives:
            reply_text = build_no_match_reply(
                language=language,
                site_origin=conversation.site_origin,
                criteria_summary=criteria_summary,
                relaxed_filter=relaxed_filter_label,
                alternatives=cards,
            )
            new_seen = list(set(seen_ids + [c["property_id"] for c in cards]))
            stored_prefs["seen_property_ids"] = new_seen
            stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
        else:
            # Distinguish true zero-match vs all matches already shown this session
            reply_text, recovered, empty_reason = resolve_empty_search_after_seen_exclusion(
                db,
                params,
                exclude_ids,
                language=language,
                site_origin=conversation.site_origin,
                criteria_summary=criteria_summary,
            )
            if empty_reason == "unseen_remaining" and recovered:
                cards = recovered
                empty_reason = None
                reply_text = build_rich_property_recommendation_text(
                    cards=cards,
                    language=language,
                    is_alternative=False,
                    is_sparse=(not loc) or (not max_p) or (not bedrooms),
                    searched_location=loc,
                    searched_bedrooms=bedrooms,
                    searched_budget=float(max_p) if max_p else None,
                    site_origin=conversation.site_origin,
                )
                reply_text = ensure_structured_response(
                    reply_text, cards=cards, user_message=user_message, language=language
                )
                new_seen = list(set(seen_ids + [c["property_id"] for c in cards]))
                stored_prefs["seen_property_ids"] = new_seen
                stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
            else:
                cards = []

        if empty_reason != "seen_all":
            reply_text, cards = self._validate_or_fallback(
                reply_text,
                cards,
                language=language,
                site_origin=conversation.site_origin,
                applied_filters={
                    "location": loc,
                    "property_type": p_type,
                    "bedrooms": bedrooms,
                    "max_price": float(max_p) if max_p is not None else None,
                },
                criteria_summary=criteria_summary,
            )

        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text,
            structured_data={"properties": cards, "property_ids": [c.get("property_id") for c in cards if c.get("property_id")]},
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
        db: Any,
        conversation: Any,
        args: Dict[str, Any],
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Resolve property reference, then fetch CURRENT authorized facts (never chat memory)."""
        pctx = load_property_context(conversation)

        # Prefer explicit args, else resolve from persistent context
        pid = args.get("property_id") or args.get("property_name")
        pname = args.get("property_name")
        ordinal_index = args.get("ordinal_index")
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
        explicit_resolved = False

        # TASK 1: Explicit slug / name in the message always overrides active property
        explicit_token = extract_explicit_property_token(user_message, args)
        if explicit_token:
            fs_card = lookup_property_by_slug_or_name(explicit_token)
            if fs_card and isinstance(fs_card, dict):
                card = fs_card
                pid = fs_card.get("property_id") or explicit_token
                pname = fs_card.get("name") or explicit_token
                p_name = fs_card.get("name") or fs_card.get("property_name")
                p_id = fs_card.get("property_id") or pid
                p_loc = fs_card.get("location")
                p_price = fs_card.get("price") or fs_card.get("price_display")
                p_beds = fs_card.get("bedrooms") or fs_card.get("bedrooms_display")
                p_area = fs_card.get("area") or fs_card.get("area_display")
                p_status = fs_card.get("status") or "Available"
                from app.services.listing_presentation import extract_ownership_from_property
                p_owner = extract_ownership_from_property(fs_card)
                p_high = fs_card.get("highlights") or []
                p_desc = fs_card.get("description") or ""
                explicit_resolved = True
            else:
                # If the "name" was actually a filter search (e.g. PL "wille w Phuket"), search instead
                from app.services.intent_router import message_looks_like_filter_search
                if message_looks_like_filter_search(user_message):
                    return self._handle_property_search(
                        db, conversation, user_message, start_time, language=language
                    )
                # Honest not-found — never answer with a different (active) property
                label = explicit_token
                reply_text = build_name_not_found_reply(
                    language=language,
                    site_origin=getattr(conversation, "site_origin", None),
                    asked_name=label,
                    suggestions=[],
                )
                pctx = update_rolling_summary(pctx, user_message, reply_text, None)
                stored_prefs = save_property_context(conversation, pctx)
                append_conversation_turn(
                    db=db,
                    conversation=conversation,
                    user_message=user_message,
                    assistant_message=reply_text,
                    structured_data={"properties": [], "property_ids": []},
                    new_preferences=stored_prefs,
                )
                token_tracker.log_turn(
                    TurnTokenMetrics(
                        request_id=f"req_{uuid.uuid4().hex[:8]}",
                        session_id=conversation.session_id,
                        conversation_id=str(conversation.id),
                        intent="property_details",
                        model="deterministic",
                        latency_sec=round(time.time() - start_time, 3),
                    ),
                    db=db,
                )
                return {
                    "type": "text",
                    "text": reply_text,
                    "properties": [],
                    "quick_replies": self._generate_quick_replies(user_message, [], language=language),
                    "lead_captured": False,
                    "preferences": stored_prefs,
                    "session_id": conversation.session_id,
                    "conversation_id": str(conversation.id),
                    "language": language,
                }

        if not explicit_resolved and ordinal_index is not None:
            batch = []
            for label in ("first property", "second property", "third property", "fourth property", "fifth property"):
                if pctx.reference_map.get(label):
                    batch.append(pctx.reference_map[label])
            if not batch:
                batch = list(pctx.recently_discussed_ids or [])
            try:
                idx = int(ordinal_index)
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(batch):
                pid = batch[idx]

        if not explicit_resolved and (
            not pid
            or args.get("from_property_context")
            or (
                isinstance(pid, str)
                and not re.match(r"(?i)^prop-", str(pid))
                and (" " in str(pid) or not re.search(r"\d", str(pid)))
            )
        ):
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

        # Named follow-up like "tell me more about Fantasea Condo" — map shown card name → id
        # Skip when an explicit slug/name already resolved from live data
        if not explicit_resolved:
            for candidate in [pname, pid, user_message]:
                if not candidate:
                    continue
                mapped = resolve_name_against_context(str(candidate), pctx, conversation)
                if mapped:
                    pid = mapped
                    break
                # Also try extracting quoted / trailing project name from the user message
                m_name = re.search(
                    r"(?:about|regarding|for|on)\s+(.+?)\s*$",
                    str(candidate),
                    flags=re.IGNORECASE,
                )
                if m_name:
                    mapped = resolve_name_against_context(m_name.group(1).strip(" .,?!"), pctx, conversation)
                    if mapped:
                        pid = mapped
                        break

        # Ordinal phrases that slipped through as names ("first properties")
        if not explicit_resolved and isinstance(pid, str) and re.search(
            r"^(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+"
            r"(?:ones?|propert(?:y|ies)|villas?|condos?|listings?)$",
            pid.strip(),
            flags=re.IGNORECASE,
        ):
            ord_resolved = resolve_property_reference(user_message, pctx, language=language)
            if ord_resolved.property_id:
                pid = ord_resolved.property_id

        if not explicit_resolved:
            pname = args.get("property_name") or pid

        # Live authorized fetch only — never use prior assistant text as factual source
        lookup_keys = []
        if not explicit_resolved:
            for candidate in [pid, pname, args.get("property_name")]:
                s = str(candidate or "").strip()
                if s and s not in lookup_keys:
                    lookup_keys.append(s)

        for key in lookup_keys:
            try:
                _rec, live_card, _src = fetch_authorized_property(db, key)
            except Exception as fetch_err:
                logger.warning(f"Authorized property fetch failed for '{key}': {fetch_err}")
                _rec, live_card = None, None
            if live_card and isinstance(live_card, dict):
                card = live_card
                p_name = live_card.get("name") or live_card.get("property_name")
                p_id = live_card.get("property_id") or key
                p_loc = live_card.get("location")
                p_price = live_card.get("price") or live_card.get("price_display")
                p_beds = live_card.get("bedrooms") or live_card.get("bedrooms_display")
                p_area = live_card.get("area") or live_card.get("area_display")
                p_status = live_card.get("status") or "Available"
                from app.services.listing_presentation import extract_ownership_from_property
                p_owner = extract_ownership_from_property(live_card) if isinstance(live_card, dict) else None
                p_high = live_card.get("highlights") or []
                p_desc = live_card.get("description") or ""
                pid = p_id
                break
            if _rec is not None and not isinstance(_rec, dict):
                card = format_property_card(_rec)
                p_name = getattr(_rec, "property_name", None)
                p_id = getattr(_rec, "property_id", key)
                p_loc = card.get("location")
                p_price = getattr(_rec, "price_display", None) or "Price on request"
                p_beds = getattr(_rec, "bedrooms_display", None) or "N/A"
                p_area = getattr(_rec, "area_display", None) or "N/A"
                p_status = getattr(_rec, "status", None) or "Available"
                p_owner = getattr(_rec, "ownership_type", None) or None
                if p_owner and str(p_owner).strip().lower() in (
                    "foreign freehold / leasehold available",
                    "freehold / leasehold",
                ):
                    p_owner = None
                p_high = getattr(_rec, "highlights", None) or []
                p_desc = getattr(_rec, "description", None) or ""
                pid = p_id
                break

        if not card:
            # Legacy dual-path fallback (ID then name) + Firestore fuzzy by display name
            if getattr(settings, "ENABLE_FIRESTORE_SYNC", True) and lookup_keys:
                from app.services.firestore_service import get_property_by_id_from_firestore
                fs_prop = None
                for candidate in lookup_keys:
                    try:
                        fs_prop = get_property_by_id_from_firestore(str(candidate))
                    except Exception as fs_err:
                        logger.warning(f"Firestore name/id lookup failed for '{candidate}': {fs_err}")
                        fs_prop = None
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
                    from app.services.listing_presentation import extract_ownership_from_property
                    p_owner = extract_ownership_from_property(fs_prop)
                    p_high = fs_prop.get("highlights") or []
                    p_desc = fs_prop.get("description") or ""
                    pid = p_id or pid
            if not card:
                prop = get_property_by_id(db, pid) if (db and pid) else None
                if not prop and args.get("property_name") and db:
                    prop = get_property_by_name(db, args["property_name"])
                if not prop and pname and db and pname != pid:
                    prop = get_property_by_name(db, str(pname))
                if prop:
                    card = format_property_card(prop)
                    p_name = prop.property_name
                    p_id = prop.property_id
                    p_loc = card["location"]
                    p_price = prop.price_display or "Price on request"
                    p_beds = prop.bedrooms_display or "N/A"
                    p_area = prop.area_display or "N/A"
                    p_status = prop.status or "Available"
                    from app.services.listing_presentation import extract_ownership_from_property
                    p_owner = extract_ownership_from_property(card) or (
                        prop.ownership_type if prop.ownership_type and str(prop.ownership_type).strip().lower()
                        not in ("foreign freehold / leasehold available", "freehold / leasehold") else None
                    )
                    p_high = prop.highlights or []
                    p_desc = prop.description or ""

        if card:
            focus_q = (args.get("focus_question") or user_message or "").lower()
            from app.services.amenity_service import (
                collect_property_amenities,
                enrich_card_amenity_flags,
                extract_requested_amenities,
                property_has_amenity,
                AMENITY_LABELS_EN,
            )
            from app.services.listing_presentation import (
                apply_presentation_to_card,
                clean_highlights_for_display,
                extract_ownership_from_property,
                pool_display_label,
                sanitize_marketing_claims,
            )
            if isinstance(card, dict):
                enrich_card_amenity_flags(card)
                apply_presentation_to_card(card, language=language)
                p_name = card.get("name") or p_name
                p_loc = card.get("location") or p_loc
                p_desc = sanitize_marketing_claims(str(p_desc or card.get("description") or ""), mode="description")
                p_high = clean_highlights_for_display(p_high or card.get("highlights") or [], prop=card, language=language)
                p_owner = extract_ownership_from_property(card) or p_owner
                if p_owner and str(p_owner).strip().lower() in (
                    "foreign freehold / leasehold available",
                    "freehold / leasehold",
                ):
                    p_owner = None
            amenity_facts = collect_property_amenities(card if isinstance(card, dict) else {})
            has_pool = bool(amenity_facts.get("pool") and amenity_facts["pool"].present) or bool(
                amenity_facts.get("pool_private") and amenity_facts["pool_private"].present
            )
            beach = bool(amenity_facts.get("beach_access") and amenity_facts["beach_access"].present)
            has_parking = bool(amenity_facts.get("parking") and amenity_facts["parking"].present)
            pool_lab = pool_display_label(card if isinstance(card, dict) else {}, language=language)

            def _amenity_yes_no(canon: str) -> bool:
                return property_has_amenity(card if isinstance(card, dict) else {}, canon)

            def _amenity_reply(canon: str, yes_en: str, no_en: str, yes_pl: str, no_pl: str, yes_th: str, no_th: str) -> str:
                """Short fact answer only — never dump the full spec block."""
                ok = _amenity_yes_no(canon)
                if language == "th":
                    body = yes_th if ok else no_th
                elif language == "pl":
                    body = yes_pl if ok else no_pl
                else:
                    body = yes_en if ok else no_en
                return f"**{p_name}**: {body}"

            def _display_fact(val: Any, *, missing_en: str = "not listed in our verified data") -> str:
                if val is None:
                    return missing_en
                s = str(val).strip()
                if not s or s.lower() in {"none", "n/a", "null", "unknown"}:
                    return missing_en
                return s

            p_beds_out = _display_fact(p_beds)
            p_loc_out = _display_fact(p_loc, missing_en="not listed in our verified data")
            p_area_out = _display_fact(p_area)
            p_price_out = _display_fact(p_price, missing_en="Price on request")
            # Prefer display helpers so specs never print literal None
            p_beds = p_beds_out
            p_loc = p_loc_out
            p_area = p_area_out
            if p_price_out:
                p_price = p_price_out

            reply_text = ""
            asked_amenities = extract_requested_amenities(focus_q)

            # Direct single-fact answers for the active property (short; no full spec dump)
            desc_txt = ""
            if isinstance(card, dict):
                desc_txt = str(card.get("description") or p_desc or "")
            else:
                desc_txt = str(p_desc or "")
            cfg_phrase = _bedroom_configs_from_description(desc_txt)

            if any(k in focus_q for k in [
                "how many bedroom", "bedrooms in this", "bedrooms in it", "bedroom in this",
                "ile sypialn", "กี่ห้องนอน", "how many beds",
            ]) or (
                "bedroom" in focus_q
                and any(k in focus_q for k in ["how many", "how much", "number of", "ile", "กี่"])
            ):
                if p_beds_out != "not listed in our verified data":
                    if language == "th":
                        reply_text = f"**{p_name}** มี **{p_beds_out}** ห้องนอนครับ"
                    elif language == "pl":
                        reply_text = f"**{p_name}** ma **{p_beds_out}** sypialni."
                    else:
                        reply_text = f"**{p_name}** has **{p_beds_out}** bedrooms."
                elif cfg_phrase:
                    if language == "th":
                        reply_text = (
                            f"**{p_name}**: จำนวนห้องนอนไม่ได้ระบุเป็นตัวเลขเดียวในฐานข้อมูลที่ตรวจสอบแล้ว "
                            f"— ตามคำอธิบายโครงการมีรูปแบบ {cfg_phrase}"
                        )
                    elif language == "pl":
                        reply_text = (
                            f"**{p_name}**: liczba sypialni nie jest podana jako jedna wartość w zweryfikowanych danych "
                            f"— w opisie projektu: {cfg_phrase}."
                        )
                    else:
                        reply_text = (
                            f"**{p_name}**: bedrooms are not listed as a single count in our verified data. "
                            f"The description mentions {cfg_phrase}."
                        )
                else:
                    if language == "th":
                        reply_text = f"**{p_name}**: ห้องนอนไม่ระบุในฐานข้อมูลที่ตรวจสอบแล้ว"
                    elif language == "pl":
                        reply_text = f"**{p_name}**: liczba sypialni nie figuruje w zweryfikowanych danych."
                    else:
                        reply_text = f"**{p_name}**: bedrooms are not listed in our verified data."
            elif any(k in focus_q for k in [
                "where is it", "where is this", "located", "location of", "where is the",
                "gdzie jest", "ทำเล", "อยู่ที่ไหน",
            ]) and not any(k in focus_q for k in ["pool", "bedroom", "price", "parking"]):
                map_url = ""
                if isinstance(card, dict):
                    map_url = str(card.get("maps_url") or "")
                    lat, lng = card.get("latitude"), card.get("longitude")
                    if not map_url and lat is not None and lng is not None:
                        map_url = f"https://www.google.com/maps?q={lat},{lng}"
                if language == "th":
                    reply_text = f"**{p_name}** ตั้งอยู่ที่ **{p_loc_out}**"
                elif language == "pl":
                    reply_text = f"**{p_name}** znajduje się w **{p_loc_out}**."
                else:
                    reply_text = f"**{p_name}** is located in **{p_loc_out}**."
                if map_url:
                    reply_text += f" [Map]({map_url})"
            # Focused amenity/price answers from unified amenity helper — never invent
            elif any(k in focus_q for k in ["pool", "swimming", "basen", "สระ"]) or "pool" in asked_amenities or "pool_private" in asked_amenities:
                reply_text = _amenity_reply(
                    "pool_private" if "private" in focus_q or "pool_private" in asked_amenities else "pool",
                    "Yes — verified data confirms a pool.",
                    "A pool is not in our verified data for this listing.",
                    "Tak — według zweryfikowanych danych posiada basen.",
                    "Basen nie figuruje w zweryfikowanych danych dla tej oferty.",
                    "มีสระว่ายน้ำในข้อมูลที่ตรวจสอบแล้วครับ",
                    "ในฐานข้อมูลที่ตรวจสอบแล้วยังไม่มีข้อมูลว่ามีสระว่ายน้ำ",
                )
            elif any(k in focus_q for k in ["parking", "garage", "ที่จอด", "garaż", "garaz"]) or "parking" in asked_amenities:
                reply_text = _amenity_reply(
                    "parking",
                    "Yes — verified data confirms parking.",
                    "Parking is not in our verified data for this listing.",
                    "Tak — według zweryfikowanych danych jest parking.",
                    "Parking nie figuruje w zweryfikowanych danych dla tej oferty.",
                    "มีที่จอดรถในข้อมูลที่ตรวจสอบแล้วครับ",
                    "ในฐานข้อมูลที่ตรวจสอบแล้วยังไม่มีข้อมูลที่จอดรถ",
                )
            elif any(k in focus_q for k in ["beach", "ชายหาด", "plaża", "plaza"]) or "beach_access" in asked_amenities:
                reply_text = _amenity_reply(
                    "beach_access",
                    "Yes — verified data confirms beach access.",
                    "Beach access is not in our verified data for this listing.",
                    "Tak — według zweryfikowanych danych jest dostęp do plaży.",
                    "Dostęp do plaży nie figuruje w zweryfikowanych danych dla tej oferty.",
                    "มีทางเข้าชายหาดในข้อมูลที่ตรวจสอบแล้วครับ",
                    "ในฐานข้อมูลที่ตรวจสอบแล้วยังไม่มีข้อมูลทางเข้าชายหาด",
                )
            elif any(k in focus_q for k in ["gym", "fitness", "siłowni", "silowni", "ฟิตเนส", "ยิม"]) or "gym" in asked_amenities:
                reply_text = _amenity_reply(
                    "gym",
                    "Yes — verified data confirms a gym / fitness area.",
                    "A gym is not in our verified data for this listing.",
                    "Tak — według zweryfikowanych danych jest siłownia.",
                    "Siłownia nie figuruje w zweryfikowanych danych dla tej oferty.",
                    "มีฟิตเนสในข้อมูลที่ตรวจสอบแล้วครับ",
                    "ในฐานข้อมูลที่ตรวจสอบแล้วยังไม่มีข้อมูลฟิตเนส",
                )
            elif asked_amenities:
                canon = asked_amenities[0]
                label = AMENITY_LABELS_EN.get(canon, canon)
                reply_text = _amenity_reply(
                    canon,
                    f"Yes — verified data confirms {label.lower()}.",
                    f"{label} is not in our verified data for this listing.",
                    f"Tak — według zweryfikowanych danych: {label}.",
                    f"{label} nie figuruje w zweryfikowanych danych dla tej oferty.",
                    f"มี{label}ในข้อมูลที่ตรวจสอบแล้วครับ",
                    f"ในฐานข้อมูลที่ตรวจสอบแล้วยังไม่มีข้อมูลเกี่ยวกับ {label}",
                )
            elif any(k in focus_q for k in ["price", "cost", "how much", "cena", "ราคา"]) and not any(
                k in focus_q for k in ["bedroom", "ownership", "status", "sypialn", "własno", "wlasno", "กรรมสิทธิ์"]
            ):
                price_out = p_price if p_price not in (None, "", "N/A") else "Not documented in verified database"
                if language == "th":
                    reply_text = f"**{p_name}** ราคา: **{price_out}**\n\n• **ทำเล:** {p_loc or 'ไม่ระบุในฐานข้อมูล'}\n• **ห้องนอน / พื้นที่:** {p_beds or 'ไม่ระบุในฐานข้อมูล'} | {p_area or 'ไม่ระบุในฐานข้อมูล'}"
                elif language == "pl":
                    reply_text = f"**{p_name}** cena: **{price_out}**\n\n• **Lokalizacja:** {p_loc or 'Brak w bazie'}\n• **Sypialnie / Powierzchnia:** {p_beds or 'Brak w bazie'} | {p_area or 'Brak w bazie'}"
                else:
                    reply_text = f"**{p_name}** verified price: **{price_out}**\n\n• **Location:** {p_loc or 'Not documented in verified database'}\n• **Bedrooms / Area:** {p_beds or 'Not documented in verified database'} | {p_area or 'Not documented in verified database'}"
            elif sum(1 for k in ("price", "bedroom", "ownership", "cena", "sypialn", "własno", "wlasno", "ราคา", "ห้องนอน", "กรรมสิทธิ์") if k in focus_q) >= 2:
                # Multi-fact ask: only emit fields that exist; say not available when missing
                def _field(label_en, label_pl, label_th, value):
                    missing = value in (None, "", "N/A")
                    if language == "th":
                        return f"• **{label_th}:** {'ไม่ระบุในฐานข้อมูล' if missing else value}"
                    if language == "pl":
                        return f"• **{label_pl}:** {'Brak w bazie' if missing else value}"
                    return f"• **{label_en}:** {'Not documented in verified database' if missing else value}"
                lines = [
                    f"Verified facts for **{p_name}**:" if language == "en" else (
                        f"Zweryfikowane dane dla **{p_name}**:" if language == "pl" else f"ข้อมูลที่ยืนยันแล้วสำหรับ **{p_name}**:"
                    ),
                    "",
                    _field("Price", "Cena", "ราคา", p_price),
                    _field("Bedrooms", "Sypialnie", "ห้องนอน", p_beds),
                ]
                if p_owner:
                    lines.append(_field("Ownership", "Forma własności", "กรรมสิทธิ์", p_owner))
                lines.append(_field("Status", "Status", "สถานะ", p_status))
                if "location" in focus_q or "ทำเล" in focus_q:
                    lines.append(_field("Location", "Lokalizacja", "ทำเล", p_loc))
                reply_text = "\n".join(lines)
            elif language == "th":
                lines_th = [
                    f"ข้อมูลจำเพาะที่ผ่านการตรวจสอบสำหรับ **{p_name}** ({p_id}):",
                    "",
                    f"• **ทำเล:** {p_loc}",
                    f"• **ราคา:** {p_price}",
                    f"• **ห้องนอน / พื้นที่ใช้สอย:** {p_beds} | {p_area}",
                    f"• **สถานะ:** {p_status}",
                ]
                if pool_lab:
                    lines_th.append(f"• **สระว่ายน้ำ:** {pool_lab}")
                if beach:
                    lines_th.append("• **ใกล้ชายหาด:** มี")
                if p_owner:
                    lines_th.append(f"• **รูปแบบกรรมสิทธิ์:** {p_owner}")
                if p_high:
                    lines_th.append(f"• **จุดเด่น:** {', '.join(p_high)}")
                lines_th.extend(["", p_desc or "", "", "คุณต้องการดูผังอาคาร (Floor Plan) เพิ่มเติม หรือต้องการให้นัดหมายเข้าชมโครงการไหมครับ?"])
                reply_text = "\n".join(lines_th)
            elif language == "pl":
                lines_pl = [
                    f"Oto zweryfikowane parametry dla **{p_name}** ({p_id}):",
                    "",
                    f"• **Lokalizacja:** {p_loc}",
                    f"• **Cena:** {p_price}",
                    f"• **Sypialnie / Powierzchnia:** {p_beds} | {p_area}",
                    f"• **Status:** {p_status}",
                ]
                if pool_lab:
                    lines_pl.append(f"• **Basen:** {pool_lab}")
                if beach:
                    lines_pl.append("• **Dostęp do plaży:** Tak")
                if p_owner:
                    lines_pl.append(f"• **Forma własności:** {p_owner}")
                if p_high:
                    lines_pl.append(f"• **Główne atuty:** {', '.join(p_high)}")
                lines_pl.extend(["", p_desc or "", "", "Czy chcesz uzyskać więcej szczegółów na temat rzutów pięter lub umówić termin prezentacji z naszym doradcą?"])
                reply_text = "\n".join(lines_pl)
            else:
                lines_en = [
                    f"Here are the verified specifications for **{p_name}** ({p_id}):",
                    "",
                    f"• **Location:** {p_loc}",
                    f"• **Price:** {p_price}",
                    f"• **Bedrooms / Area:** {p_beds} | {p_area}",
                    f"• **Status:** {p_status}",
                ]
                if pool_lab:
                    lines_en.append(f"• **Pool:** {pool_lab}")
                if beach:
                    lines_en.append("• **Beach access:** Yes")
                if p_owner:
                    lines_en.append(f"• **Ownership:** {p_owner}")
                if p_high:
                    lines_en.append(f"• **Key Highlights:** {', '.join(p_high)}")
                _comp = None
                if isinstance(card, dict):
                    _comp = card.get("completion_note") or format_stale_completion_note(
                        card.get("completion_date") or card.get("completion"), language=language
                    )
                if _comp:
                    lines_en.append(f"• **Completion:** {_comp}")
                lines_en.extend(["", p_desc or "", "", "Would you like more details regarding floor plans or assistance scheduling a viewing tour?"])
                reply_text = "\n".join(lines_en)
            cards = [card]
            enqueue_lead_save(
                db=db,
                conversation=conversation,
                buying_signal=f"Requested details for {p_id} ({p_name})"
            )
        else:
            from app.core.tenant import resolve_tenant as _rt_nf
            _adv = _rt_nf(getattr(conversation, "site_origin", None)).advisor_email
            label = pid or pname or "that listing"
            if language == "th":
                reply_text = (
                    f"ไม่พบ '{label}' ในฐานข้อมูลที่ตรวจสอบแล้วครับ\n\n"
                    f"ทีมที่ปรึกษา ({_adv}) สามารถช่วยตรวจสอบเพิ่มเติมได้ "
                    "หรือแจ้งทำเล / งบประมาณ / ประเภทอสังหาฯ เพื่อค้นหาจากรายการที่มีอยู่จริงครับ"
                )
            elif language == "pl":
                reply_text = (
                    f"Nie znaleziono '{label}' w zweryfikowanej bazie.\n\n"
                    f"Nasz zespół doradców ({_adv}) może pomóc dalej. "
                    "Możesz też podać lokalizację, budżet lub typ nieruchomości."
                )
            else:
                reply_text = (
                    f"'{label}' was not found in our verified database.\n\n"
                    f"Our advisory team ({_adv}) can help further. "
                    "You can also share a location, budget, or property type to search among verified listings only."
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
        db: Any,
        conversation: Any,
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
                    f"| **รูปแบบกรรมสิทธิ์** | {p1.ownership_type or 'ยืนยันกับที่ปรึกษา'} | {p2.ownership_type or 'ยืนยันกับที่ปรึกษา'} |\n\n"
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
                    f"| **Forma własności** | {p1.ownership_type or 'Potwierdź z doradcą'} | {p2.ownership_type or 'Potwierdź z doradcą'} |\n\n"
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
                    f"| **Ownership** | {p1.ownership_type or 'Confirm with advisor'} | {p2.ownership_type or 'Confirm with advisor'} |\n\n"
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
        db: Any,
        conversation: Any,
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
        try:
            from app.repositories import properties_store

            all_props = properties_store.public_rows(db)
        except Exception:
            all_props = []
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

        # Property found — resolve display name (PG or Firestore), then generate brochure
        from app.core.tenant import resolve_tenant
        tenant = resolve_tenant(conversation.site_origin)

        p_name = prop_id
        pref_loc = None
        pref_type = None
        if getattr(settings, "ENABLE_FIRESTORE_SYNC", True):
            try:
                from app.services.firestore_service import get_property_by_id_from_firestore
                fs_card = get_property_by_id_from_firestore(str(prop_id).strip())
                if fs_card:
                    p_name = fs_card.get("name") or p_name
                    pref_loc = fs_card.get("location") or pref_loc
                    pref_type = fs_card.get("property_type") or pref_type
            except Exception:
                pass

        # Generate 8-page luxury presentation brochure and upload to S3 / storage
        from app.services.brochure_service import generate_and_upload_brochure
        lead_name = stored_prefs.get("contact_name") or stored_prefs.get("name")
        pdf_url = None
        brochure_ok = False
        try:
            brochure_res = generate_and_upload_brochure(
                db=db,
                property_id=prop_id,
                site_origin=conversation.site_origin,
                language=language,
                client_name=lead_name
            )
            pdf_url = brochure_res.get("file_url") or brochure_res.get("download_url")
            brochure_ok = bool(pdf_url)
        except Exception as be:
            logger.error(f"Failed to generate and upload brochure for {prop_id}: {be}", exc_info=True)
            brochure_ok = False
            pdf_url = None

        if not brochure_ok:
            # Honest failure: no fake download link; do not mark brochure as delivered
            reply_text = get_brochure_unavailable_text(
                language=language,
                site_origin=conversation.site_origin,
                property_label=p_name,
            )
            try:
                enqueue_lead_save(
                    db=db,
                    conversation=conversation,
                    preferred_location=pref_loc,
                    preferred_type=pref_type,
                    buying_signal=f"Brochure request failed for {prop_id} ({p_name})",
                    notes=f"Client asked for brochure for {prop_id} on {tenant.brand_name} but generation failed",
                )
            except Exception as le:
                logger.warning(f"Could not record failed-brochure lead: {le}")
            append_conversation_turn(
                db=db, conversation=conversation, user_message=user_message, assistant_message=reply_text
            )
            return {
                "type": "text",
                "text": reply_text,
                "properties": [],
                "quick_replies": [
                    f"View details for {prop_id}",
                    "Show villas in Phuket",
                    "Contact advisor",
                ],
                "lead_captured": True,
                "preferences": stored_prefs,
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": language,
                "brochure_property_id": None,
                "pdf_url": None,
                "download_url": None,
            }

        # Success: record lead + mark brochure as delivered
        enqueue_lead_save(
            db=db,
            conversation=conversation,
            preferred_location=pref_loc,
            preferred_type=pref_type,
            buying_signal=f"Requested brochure for {prop_id} ({p_name})",
            notes=f"Client requested verified property brochure for {prop_id} on {tenant.brand_name} ({tenant.domain})",
        )
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

        confirm_msgs = {
            "en": f"Official brochure for {p_name} - [Download PDF]({pdf_url})",
            "pl": f"Oficjalna broszura dla {p_name} - [Pobierz PDF]({pdf_url})",
            "th": f"โบรชัวร์สำหรับ {p_name} - [ดาวน์โหลด PDF]({pdf_url})",
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
        db: Any,
        conversation: Any,
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
                    is_sparse=(not loc) or (not max_p) or (not bedrooms),
                    location=loc,
                    budget=max_p,
                    bedrooms=bedrooms,
                    site_origin=conversation.site_origin
                )
        else:
            # Exact refine with exclusion empty — seen-all vs true no-match
            criteria_bits = []
            if loc:
                criteria_bits.append(str(loc))
            if p_type:
                criteria_bits.append(str(p_type))
            if bedrooms:
                criteria_bits.append(f"{bedrooms} bed")
            if max_p is not None:
                criteria_bits.append(f"budget <= {max_p}")
            criteria_summary = ", ".join(criteria_bits) if criteria_bits else None
            reply_text, recovered, empty_reason = resolve_empty_search_after_seen_exclusion(
                db,
                params,
                seen_ids,
                language=language,
                site_origin=getattr(conversation, "site_origin", None),
                criteria_summary=criteria_summary,
            )
            if empty_reason == "unseen_remaining" and recovered:
                cards = recovered
                stored_prefs["seen_property_ids"] = list(
                    set(seen_ids + [c["property_id"] for c in cards])
                )
                stored_prefs["last_result_ids"] = [c["property_id"] for c in cards]
                reply_text = build_rich_property_recommendation_text(
                    cards=cards,
                    language=language,
                    is_alternative=False,
                    is_sparse=(not loc) or (not max_p) or (not bedrooms),
                    location=loc,
                    budget=max_p,
                    bedrooms=bedrooms,
                    site_origin=conversation.site_origin,
                )
            else:
                cards = []
                stored_prefs["last_result_ids"] = []

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
        db: Any,
        conversation: Any,
        user_message: str,
        start_time: float,
        language: str = "en"
    ) -> Dict[str, Any]:
        """Directly executes search in DB/cache, then synthesizes recommendations with a single compact LLM call without tool schemas."""
        stored_prefs = dict(conversation.preferences or {})
        extracted = extract_search_filters_deterministic(user_message)
        cache_hit = False
        search_relaxed_filter = None
        search_relaxed_alts = None
        loc = None
        p_type = None
        bedrooms = None
        max_p = None
        has_pool = None
        beach_access = None
        required_amenities: List[str] = []
        amenity_none_match = False
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
            beach_access = extracted.get("beach_access", stored_prefs.get("beach_access"))
            required_amenities = list(
                extracted.get("required_amenities")
                or (stored_prefs.get("active_filters") or {}).get("required_amenities")
                or []
            )

            search_filters = {
                "location": loc,
                "property_type": p_type,
                "bedrooms": bedrooms,
                "max_price": float(max_p) if max_p else None,
                "has_pool": has_pool,
                "beach_access": beach_access,
                "required_amenities": required_amenities or None,
            }
            stored_prefs["active_filters"] = search_filters
            seen_ids = get_session_seen_property_ids(conversation)
            params = PropertyFilterParams(
                location=loc,
                property_type=p_type,
                bedrooms=bedrooms,
                max_price=max_p,
                has_pool=has_pool,
                beach_access=beach_access,
                required_amenities=required_amenities or None,
                offset=0,
                limit=4
            )
            # Fresh searches must NOT exclude previously seen IDs — exclusion is only for "show more".
            # Excluding on every search caused unrelated new listings and felt like hallucination.
            cards = search_properties_unified(db, params, limit=4, exclude_property_ids=None)

            # Smart fresh criteria recovery: If merged search yielded 0 results because of stale constraints
            # from earlier turns, retry with ONLY the fresh criteria specified in this turn.
            has_fresh_criteria = bool(
                extracted.get("location")
                or extracted.get("property_type")
                or extracted.get("bedrooms")
                or extracted.get("max_price")
                or extracted.get("has_pool")
                or extracted.get("required_amenities")
            )
            if not cards and has_fresh_criteria:
                fresh_loc = extracted.get("location")
                fresh_type = extracted.get("property_type")
                fresh_bed = extracted.get("bedrooms")
                fresh_max_p = Decimal(str(extracted["max_price"])) if extracted.get("max_price") else None
                fresh_pool = extracted.get("has_pool")
                fresh_am = list(extracted.get("required_amenities") or [])

                fresh_params = PropertyFilterParams(
                    location=fresh_loc,
                    property_type=fresh_type,
                    bedrooms=fresh_bed,
                    max_price=fresh_max_p,
                    has_pool=fresh_pool,
                    beach_access=extracted.get("beach_access"),
                    required_amenities=fresh_am or None,
                    offset=0,
                    limit=4
                )
                matched_fresh = search_properties_unified(db, fresh_params, limit=4, exclude_property_ids=None)
                if matched_fresh:
                    cards = matched_fresh
                    loc, p_type, bedrooms, max_p, has_pool = fresh_loc, fresh_type, fresh_bed, fresh_max_p, fresh_pool
                    required_amenities = fresh_am
                    search_filters = {
                        "location": loc,
                        "property_type": p_type,
                        "bedrooms": bedrooms,
                        "max_price": float(max_p) if max_p else None,
                        "has_pool": has_pool,
                        "required_amenities": required_amenities or None,
                    }
                    stored_prefs["active_filters"] = search_filters

            # Amenity none-match: honest message + labelled alternatives (never fake a gym match)
            if not cards and required_amenities:
                from app.services.amenity_service import (
                    AMENITY_LABELS_EN,
                    apply_amenity_labels,
                    filter_cards_by_amenities,
                )
                # Same location without amenity filter
                base_params = PropertyFilterParams(
                    location=loc,
                    property_type=p_type,
                    bedrooms=bedrooms,
                    max_price=max_p,
                    offset=0,
                    limit=6,
                )
                same_loc = search_properties_unified(db, base_params, limit=6, exclude_property_ids=None)
                # Other locations that DO have the amenity
                other_params = PropertyFilterParams(
                    location=None,
                    property_type=p_type,
                    bedrooms=bedrooms,
                    max_price=max_p,
                    required_amenities=required_amenities,
                    offset=0,
                    limit=4,
                )
                other_match = search_properties_unified(db, other_params, limit=4, exclude_property_ids=None)
                other_match = [
                    c for c in other_match
                    if loc and loc.lower() not in str(c.get("location") or "").lower()
                ] if loc else other_match
                alts: List[Dict[str, Any]] = []
                if other_match:
                    alts = apply_amenity_labels(other_match[:3], required_amenities, language=language)
                elif same_loc:
                    alts = apply_amenity_labels(same_loc[:3], required_amenities, language=language)
                    # Force "not listed" labels for same-location non-matches
                    for c in alts:
                        c["amenity_match"] = False
                amenity_none_match = True
                amenity_lab = AMENITY_LABELS_EN.get(required_amenities[0], required_amenities[0])
                reply_text = build_amenity_none_match_reply(
                    language=language,
                    site_origin=getattr(conversation, "site_origin", None),
                    location=loc,
                    amenity_label=amenity_lab,
                    alternatives=alts,
                )
                cards = alts
                search_relaxed_alts = alts
                search_relaxed_filter = f"amenity:{required_amenities[0]}"

            # Zero results only: exactly one relaxed filter (sub-district -> Phuket), never pad silently
            if not cards and not amenity_none_match and loc in ["Bang Tao", "Choeng Thale", "Layan", "Kamala", "Surin", "Rawai", "Karon"]:
                broad_params = PropertyFilterParams(
                    location="Phuket",
                    property_type=p_type,
                    bedrooms=bedrooms,
                    max_price=max_p,
                    has_pool=has_pool,
                    required_amenities=required_amenities or None,
                    offset=0,
                    limit=3
                )
                broad_matched = search_properties_unified(db, broad_params, limit=3, exclude_property_ids=None)
                if broad_matched:
                    search_relaxed_alts = broad_matched[:3]
                    search_relaxed_filter = "location -> Phuket" if language == "en" else (
                        "lokalizacja -> Phuket" if language == "pl" else "ทำเล -> ภูเก็ต"
                    )

        prompt_tokens = 0
        comp_tokens = 0
        llm_calls = 0
        reply_text = reply_text if amenity_none_match else ""
        is_sparse = (not loc) or (not max_p) or (not bedrooms)

        # Label matching amenity search results (Gym ✓) — never present non-matches as matches
        if cards and required_amenities and not amenity_none_match:
            from app.services.amenity_service import (
                AMENITY_LABELS_EN,
                apply_amenity_labels,
                filter_cards_by_amenities,
            )
            matching, _non = filter_cards_by_amenities(cards, required_amenities)
            if matching:
                cards = apply_amenity_labels(matching, required_amenities, language=language)
            else:
                # Safety: never present non-matching cards as if they matched
                labelled = apply_amenity_labels(cards[:3], required_amenities, language=language)
                for c in labelled:
                    c["amenity_match"] = False
                amenity_lab = AMENITY_LABELS_EN.get(required_amenities[0], required_amenities[0])
                reply_text = build_amenity_none_match_reply(
                    language=language,
                    site_origin=getattr(conversation, "site_origin", None),
                    location=loc,
                    amenity_label=amenity_lab,
                    alternatives=labelled,
                )
                cards = labelled
                amenity_none_match = True

        if cards and not amenity_none_match:
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

            # Text list must mirror cards (same count + order) — deterministic from card payload
            if is_show_all:
                intro = (
                    "Here are all verified properties currently available in our Thailand portfolio:"
                    if language == "en"
                    else (
                        "Oto wszystkie zweryfikowane nieruchomości dostępne w naszym portfolio w Tajlandii:"
                        if language == "pl"
                        else "นี่คืออสังหาริมทรัพย์ที่ผ่านการตรวจสอบทั้งหมดในพอร์ตโฟลิโอของเราในประเทศไทย:"
                    )
                )
                body = build_rich_property_recommendation_text(
                    cards=cards,
                    language=language,
                    is_alternative=False,
                    is_sparse=is_sparse,
                    location=loc,
                    budget=max_p,
                    bedrooms=bedrooms,
                    site_origin=conversation.site_origin,
                )
                reply_text = f"{intro}\n\n{body}" if body else intro
            else:
                reply_text = build_rich_property_recommendation_text(
                    cards=cards,
                    language=language,
                    is_alternative=False,
                    is_sparse=is_sparse,
                    location=loc,
                    budget=max_p,
                    bedrooms=bedrooms,
                    site_origin=conversation.site_origin,
                )
            if is_sparse:
                followups = build_consultative_followup_questions(
                    language=language,
                    location=loc,
                    budget=max_p,
                    bedrooms=bedrooms,
                )[:2]
                if followups:
                    if language == "pl":
                        q_hdr = "Aby dopasować oferty precyzyjniej, powiedz proszę:"
                    elif language == "th":
                        q_hdr = "เพื่อคัดกรองให้ตรงกับคุณมากขึ้น ขออนุญาตถามเพิ่มเติม:"
                    else:
                        q_hdr = "To narrow this down to the best fit, may I ask:"
                    reply_text = (
                        (reply_text or "").rstrip()
                        + "\n\n"
                        + q_hdr
                        + "\n"
                        + "\n".join(f"• {q}" for q in followups)
                    )
        elif amenity_none_match:
            # Honest amenity miss already composed; keep labelled alternatives only
            stored_prefs["last_result_ids"] = [c["property_id"] for c in cards if c.get("property_id")]
            if cards:
                stored_prefs["seen_property_ids"] = list(
                    set(seen_ids + [c["property_id"] for c in cards if c.get("property_id")])
                )
        else:
            # No exact production match — never invent listings; honest no-match only
            stored_prefs["last_result_ids"] = []
            if foreign_loc:
                cards = []
                reply_text = get_thailand_only_foreign_reply(
                    language,
                    site_origin=conversation.site_origin,
                    foreign_place=str(foreign_loc),
                )
            elif search_relaxed_alts and search_relaxed_filter:
                cards = list(search_relaxed_alts)
                criteria_bits = []
                if loc:
                    criteria_bits.append(str(loc))
                if p_type:
                    criteria_bits.append(str(p_type))
                if bedrooms:
                    criteria_bits.append(f"{bedrooms} bed")
                if max_p is not None:
                    criteria_bits.append(f"budget <= {max_p}")
                reply_text = build_no_match_reply(
                    language=language,
                    site_origin=getattr(conversation, "site_origin", None),
                    criteria_summary=", ".join(criteria_bits) if criteria_bits else None,
                    relaxed_filter=search_relaxed_filter,
                    alternatives=cards,
                )
                stored_prefs["last_result_ids"] = [c["property_id"] for c in cards if c.get("property_id")]
            else:
                cards = []
                criteria_bits = []
                if loc:
                    criteria_bits.append(str(loc))
                if p_type:
                    criteria_bits.append(str(p_type))
                if bedrooms:
                    criteria_bits.append(f"{bedrooms} bed")
                if max_p is not None:
                    criteria_bits.append(f"budget <= {max_p}")
                reply_text = build_no_match_reply(
                    language=language,
                    site_origin=getattr(conversation, "site_origin", None),
                    criteria_summary=", ".join(criteria_bits) if criteria_bits else (
                        user_message[:120] if user_message else None
                    ),
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
        criteria_bits = []
        if loc:
            criteria_bits.append(str(loc))
        if p_type:
            criteria_bits.append(str(p_type))
        if bedrooms:
            criteria_bits.append(f"{bedrooms} bed")
        if max_p is not None:
            criteria_bits.append(f"budget <= {max_p}")
        reply_text, cards = self._validate_or_fallback(
            reply_text,
            cards,
            language=language,
            site_origin=getattr(conversation, "site_origin", None),
            applied_filters=search_filters if isinstance(search_filters, dict) else {},
            criteria_summary=", ".join(criteria_bits) if criteria_bits else None,
        )
        if cards:
            self._clear_pending_name_candidates(conversation)
        elif "Did you mean" in (reply_text or "") or "Czy chodziło" in (reply_text or "") or "คุณหมายถึง" in (reply_text or ""):
            self._sync_pending_name_candidates_from_last_search(conversation)
            updated_prefs = dict(updated_prefs or {})
            updated_prefs.update(conversation.preferences or {})

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
        db: Any,
        conversation: Any,
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

    def _handle_cannot_email_or_calendar(
        self,
        db: Any,
        conversation: Any,
        user_message: str,
        start_time: float,
        language: str = "en",
    ) -> Dict[str, Any]:
        """
        Honest refusal when the visitor asks the chat to email them or add a calendar event.
        Saves a lead when contact details are present. Never calls Gmail or Calendar APIs.
        """
        visitor_email = extract_contact_email_from_message(user_message)
        phone_m = re.search(
            r"(?:\+?\d{1,4}[-.\s]?(?:\(?\d{1,4}\)?[-.\s]?)?(?:\d{2,4}[-.\s]?){2,4}\d{2,4})",
            user_message or "",
        )
        visitor_phone = phone_m.group(0).strip() if phone_m else None
        lead_captured = False
        if visitor_email or visitor_phone:
            try:
                enqueue_lead_save(
                    db=db,
                    conversation=conversation,
                    contact_email=visitor_email,
                    contact_phone=visitor_phone,
                    notes=f"Visitor asked for email/calendar action from chat: {user_message[:200]}",
                    buying_signal="Requested email or calendar action via public chat (recorded only)",
                )
                lead_captured = True
            except Exception as le:
                logger.warning("Lead save failed on cannot-email path (reply unaffected): %s", le)
                lead_captured = False

        reply_text = get_cannot_email_or_calendar_text(
            language=language,
            site_origin=getattr(conversation, "site_origin", None),
            visitor_email=visitor_email,
        )
        append_conversation_turn(
            db=db,
            conversation=conversation,
            user_message=user_message,
            assistant_message=reply_text,
        )
        token_tracker.log_turn(
            TurnTokenMetrics(
                request_id=f"req_{uuid.uuid4().hex[:8]}",
                session_id=conversation.session_id,
                conversation_id=str(conversation.id),
                intent="cannot_email_or_calendar",
                model="deterministic",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                llm_calls_count=0,
                latency_sec=round(time.time() - start_time, 3),
            ),
            db=db,
        )
        return {
            "type": "text",
            "text": reply_text,
            "properties": [],
            "quick_replies": self._generate_quick_replies(user_message, [], language=language),
            "lead_captured": lead_captured,
            "preferences": conversation.preferences or {},
            "session_id": conversation.session_id,
            "conversation_id": str(conversation.id),
            "language": language,
        }

    def _handle_greeting(
        self,
        db: Any,
        conversation: Any,
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
        db: Any,
        conversation: Any,
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
                                "The user is inquiring about real estate outside Thailand.\n"
                                "1. Politely clarify that we only cover verified listings in Thailand "
                                "(Phuket, Koh Samui, Koh Phangan, Bangkok, Pattaya) and do not represent foreign markets.\n"
                                "2. Invite them to browse verified Thailand listings. "
                                "Do NOT quote any rental yield percentages, ROI figures, or invented market statistics.\n"
                                "3. Ask up to 2 short clarifying questions about Thailand preferences "
                                "(location, budget, or property type) if those details are missing.\n"
                                "4. Offer the tenant advisory email already in your system prompt for further help."
                            )
                        })
                    else:
                        # Prefer honest Did-you-mean from search_properties when present
                        all_cards = []
                        name_msg = None
                        name_cands_from_tool: List[Dict[str, Any]] = []
                        for tr in logged_tool_results:
                            res = (tr or {}).get("result") or {}
                            if isinstance(res, dict) and res.get("name_suggestions") is not None:
                                name_msg = res.get("message") or build_name_not_found_reply(
                                    language=language,
                                    site_origin=getattr(conversation, "site_origin", None),
                                    asked_name=(raw_user_message or user_message or "")[:120] or None,
                                    suggestions=list(res.get("name_suggestions") or []),
                                )
                                name_cands_from_tool = list(res.get("name_candidates") or [])
                                if name_cands_from_tool:
                                    from app.services.name_confirmation import store_pending_name_candidates
                                    conversation.preferences = store_pending_name_candidates(
                                        conversation.preferences,
                                        query=(raw_user_message or user_message or ""),
                                        candidates=name_cands_from_tool,
                                    )
                                break
                        if name_msg:
                            final_text = name_msg
                        else:
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
            final_text, all_cards = self._validate_or_fallback(
                final_text,
                all_cards,
                language=language,
                site_origin=getattr(conversation, "site_origin", None),
                applied_filters=search_args if isinstance(search_args, dict) else {},
                tool_results=logged_tool_results,
                criteria_summary=(raw_user_message or user_message or "")[:120] or None,
            )
            if not all_cards:
                resp_type = "text"
            if all_cards:
                self._clear_pending_name_candidates(conversation)
            else:
                asked = (
                    (search_args or {}).get("query")
                    or (search_args or {}).get("property_name")
                    or (raw_user_message or user_message)
                )
                self._sync_pending_name_candidates_from_last_search(
                    conversation,
                    asked_name=str(asked) if asked else None,
                    tool_results=logged_tool_results,
                )
                updated_prefs = dict(updated_prefs or {})
                updated_prefs.update(conversation.preferences or {})

            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=raw_user_message or user_message,
                assistant_message=final_text,
                tool_calls=logged_tool_calls,
                tool_results=logged_tool_results,
                structured_data={"properties": all_cards, "property_ids": [c["property_id"] for c in all_cards if c.get("property_id")]},
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
        db: Any,
        conversation: Any,
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

        # Extract contact information (never from prompt-injection payloads)
        has_email = extract_contact_email_from_message(user_message)
        phone_match = re.search(r"(?:\+?\d{1,3}[- ]?)?\(?\d{3}\)?[- ]?\d{3}[- ]?\d{4}", user_message)
        has_phone = None if looks_like_prompt_injection(user_message) else (
            phone_match.group(0) if phone_match else None
        )

        lead_captured = False
        if has_email or has_phone or max_p:
            enqueue_lead_save(
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
