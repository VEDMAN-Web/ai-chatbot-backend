import re
import json
import logging
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from decimal import Decimal

logger = logging.getLogger(__name__)

class IntentType(str, Enum):
    GREETING = "greeting"
    SHOW_MORE = "show_more"
    REFINE_FILTER = "refine_filter"
    PROPERTY_SEARCH = "property_search"
    PROPERTY_DETAILS = "property_details"
    PROPERTY_COMPARISON = "property_comparison"
    BROCHURE_REQUEST = "brochure_request"
    GENERAL_QUESTION = "general_question"
    LEAD_CAPTURE = "lead_capture"
    OFF_TOPIC = "off_topic"


# Deterministic trigger patterns
SHOW_MORE_PATTERNS = [
    r"^(?:show|see|load|fetch|give\s+me)\s+more(?:\s+properties|\s+villas|\s+condos|\s+results)?$",
    r"^(?:next|more)\s*(?:page|results|options)?$",
    r"^any\s+more(?:\s+options|\s+villas|\s+properties)?$",
    r"^more$",
    r"^(?:pokaż|pokaz|zobacz|daj\s+mi)?\s*(?:więcej|wiecej|kolejne)(?:\s+ofert|\s+willi|\s+apartamentów|\s+wyników)?$",
    r"^(?:następna|nastepna)\s*(?:strona|oferta)?$",
    r"^więcej$",
    r"^wiecej$",
    r"^(?:ดูเพิ่มเติม|เพิ่มเติม|หน้าถัดไป|ข้อเสนอถัดไป|แสดงเพิ่ม|ขอเพิ่ม|ดูเพิ่ม)$",
    r"^(?:ดู|ขอ)?\s*(?:เพิ่มเติม|เพิ่มอีก|รายการอื่น)$"
]

COMPARISON_PATTERNS = [
    r"(?:compare|comparison)\s+(?:the\s+)?(?:first|1st|1)\s+(?:and|with|to)\s+(?:the\s+)?(?:second|2nd|2)",
    r"(?:compare|comparison)\s+(?:the\s+)?(?:first|1st|1)\s+(?:and|with|to)\s+(?:the\s+)?(?:third|3rd|3)",
    r"(?:compare|comparison)\s+(?:the\s+)?(?:second|2nd|2)\s+(?:and|with|to)\s+(?:the\s+)?(?:third|3rd|3)",
    r"(?:compare|comparison)\s+(?:properties|props|listings)?\s*(prop-[a-z0-9\-]+)\s+(?:and|with|to|vs|versus)\s+(prop-[a-z0-9\-]+)",
    r"(?:compare|comparison)\s+(?:them|these|both|all)",
    r"(?:porównaj|porownaj|porównanie|porownanie)\s+(?:pierwszą|pierwsza|1)\s+(?:i|z|oraz)\s+(?:drugą|druga|2)",
    r"(?:porównaj|porownaj)\s+(?:je|obie|wszystkie)",
    r"(?:เปรียบเทียบ|เทียบ)\s*(?:อันแรก|รายการแรก|1)\s*(?:กับ|และ)\s*(?:อันที่สอง|รายการที่สอง|2)",
    r"(?:เปรียบเทียบ|เทียบ)\s*(?:ทั้งหมด|ทั้งสอง|สองอันนี้|สองรายการนี้)"
]

DETAILS_PATTERNS = [
    r"(?:tell\s+me\s+more\s+about|details\s+for|specs\s+for|show\s+me|info\s+on)\s+(PROP-[A-Z0-9\-]+)",
    r"(?:tell\s+me\s+more\s+about|details\s+for|specs\s+for)\s+(?:the\s+)?(first|second|third|1st|2nd|3rd)\s+(?:one|property|villa|condo)",
    r"^(?:the\s+)?(first|second|third|1st|2nd|3rd)\s+one$",
    # Named property follow-ups: "tell me more about ParQ Villa Phuket"
    r"(?:tell\s+me\s+more\s+about|tell\s+me\s+about|more\s+(?:details|info)\s+(?:about|on|for)|details\s+(?:about|for|on)|specs\s+for|info\s+(?:about|on))\s+(?:the\s+)?([a-z0-9][\w\s\-'&.]{2,80}?)\s*$",
    r"(?:pokaż|pokaz|szczegóły|szczegoly|informacje\s+o)\s+(PROP-[A-Z0-9\-]+)",
    r"(?:pokaż|pokaz|szczegóły|szczegoly|więcej\s+o|opowiedz\s+o|powiedz\s+więcej\s+o)\s+(?:tę|ta|tą|o\s+)?([a-ząćęłńóśźż0-9][\w\s\-'&.]{2,80}?)\s*$",
    r"(?:pokaż|pokaz|szczegóły|szczegoly)\s+(?:tę|ta|tą)\s+(?:pierwszą|druga|trzecią|1|2|3)",
    r"^(?:ta\s+)?(pierwsza|druga|trzecia)\s+oferta$",
    r"(?:ดูรายละเอียด|ขอรายละเอียด|ข้อมูล)\s+(PROP-[A-Z0-9\-]+)",
    r"(?:ดูรายละเอียด|ขอข้อมูล|บอกเพิ่มเติมเกี่ยวกับ|รายละเอียด)\s+([a-z0-9ก-๙][\w\s\-'&.ก-๙]{2,80}?)\s*$",
    r"(?:ดูรายละเอียด|ขอข้อมูล|ดู)\s+(?:อันแรก|อันที่สอง|อันที่สาม|1|2|3)",
    r"^(?:อันแรก|อันที่สอง|อันที่สาม)$"
]

REFINE_PATTERNS = [
    r"(?:show\s+me\s+)?cheaper(?:\s+ones|\s+villas|\s+options)?",
    r"(?:make\s+it\s+)?cheaper",
    r"(?:under|below|max|budget)\s*(\d+(?:\.\d+)?)\s*(?:m|million)?\s*(?:thb|baht)?",
    r"(?:with|add)\s+(?:a\s+)?(?:private\s+)?pool",
    r"(?:remove|without|no)\s+pool",
    r"(?:with|add)\s+sea\s*view",
    r"(?:with|add)\s+beach(?:front)?",
    r"(?:in|switch\s+to|show\s+me)\s+(samui|phuket|bangkok|layan|bang\s*tao|karon|maenam|phangan)(?:\s+instead)?",
    r"(?:pokaż\s+)?tańsze(?:\s+oferty|\s+wille)?",
    r"(?:tańszą|tansze|tansza)",
    r"(?:poniżej|ponizej|do|maksymalnie|budżet|budzet)\s*(\d+(?:\.\d+)?)\s*(?:mln|m|milionów|milionow)?\s*(?:thb|baht)?",
    r"(?:z|dodaj)\s+(?:prywatnym\s+)?basen(?:em)?",
    r"(?:bez|usuń|usun)\s+basen(?:u)?",
    r"(?:z|dodaj)\s+widok(?:iem)?\s+na\s+morze",
    r"(?:ขอ|ดู)?\s*ราคาถูกกว่า",
    r"(?:ถูกลง|ประหยัดกว่า)",
    r"(?:ราคาไม่เกิน|งบไม่เกิน|ต่ำกว่า|ไม่เกิน)\s*(\d+(?:\.\d+)?)\s*(?:ล้าน|ล้านบาท|m|thb)?",
    r"(?:มี|เพิ่ม|ขอ)\s*(?:สระว่ายน้ำ|สระ)",
    r"(?:ไม่มี|ไม่เอา|ตัด)\s*(?:สระว่ายน้ำ|สระ)",
    r"(?:มี|เพิ่ม|ขอ)\s*วิวทะเล"
]

SHOW_ALL_PATTERNS = [
    r"^(?:show|view|see|list|get)\s+all(?:\s+properties|\s+villas|\s+condos|\s+listings)?$",
    r"^all\s+(?:properties|villas|condos|listings)$",
    r"^show\s+all$",
    r"^(?:pokaż|pokaz|zobacz|wyświetl|wyswietl)\s+wszystkie(?:\s+nieruchomości|\s+nieruchomosci|\s+wille|\s+apartamenty|\s+oferty)?$",
    r"^wszystkie\s+(?:nieruchomości|\s+nieruchomosci|\s+oferty)$",
    r"^(?:แสดงทั้งหมด|ดูทั้งหมด|ดูอสังหาริมทรัพย์ทั้งหมด|รายการทั้งหมด)$",
    r"^แสดงทั้งหมด$"
]

PDF_EXPORT_PATTERNS = [
    # English patterns
    r"(?:generate|export|download|create|send|give|get|want|need|provide|show)\s+(?:me\s+)?(?:a\s+|the\s+)?(?:pdf|report|brochure|dossier|catalogue|catalog|factsheet)",
    r"(?:can|could)\s+(?:i|you)\s+(?:have|get|see|send|provide)\s+(?:me\s+)?(?:a\s+|the\s+)?(?:pdf|report|brochure|dossier|factsheet)",
    r"\b(?:brochure|pdf|factsheet|dossier)\b.*?(?:download|export|generate|please|link|file)",
    r"(?:download|export|generate|send|get)\s+(?:the\s+)?(?:brochure|pdf)",
    r"^(?:the\s+)?(?:brochure|pdf)(?:\s+for|\s+of|\s+please)?",
    r"\b(?:brochure|pdf)\s+(?:for|of|about)\b",
    r"^(?:brochure|pdf)$",

    # Polish patterns
    r"(?:wygeneruj|pobierz|eksportuj|stwórz|stworz|prześlij|przeslij|daj|poproszę|poprosze|chcę|chce|wyslij|wyślij)\s+(?:mi\s+)?(?:raport|pdf|broszurę|broszure|katalog|folder|zestawienie|dokument)",
    r"(?:czy\s+mogę\s+dostać|czy\s+moge\s+dostac|czy\s+mogę\s+prosić|czy\s+moge\s+prosiv)\s+(?:o\s+)?(?:broszurę|broszure|pdf)",
    r"^(?:broszura|broszurę|broszure|pdf)(?:\s+dla|\s+do|\s+proszę|\s+prosze)?",
    r"\b(?:broszura|broszurę|broszure|pdf)\s+(?:dla|do|o)\b",
    r"^(?:broszura|broszurę|broszure)$",

    # Thai patterns
    r"(?:สร้าง|ดาวน์โหลด|ส่ง|ออก|ขอ|อยากได้|ขอดู|ขอรับ)\s*(?:รายงาน|pdf|โบรชัวร์|เอกสาร|แคตตาล็อก|ไฟล์)",
    r"^(?:โบรชัวร์|pdf)(?:\s*สำหรับ|\s*ของ)?",
    r"\b(?:โบรชัวร์|pdf)\s*(?:สำหรับ|ของ)\b",
    r"^(?:โบรชัวร์|ขอโบรชัวร์)$"
]


def detect_deterministic_intent(
    message: str,
    stored_state: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[IntentType], Dict[str, Any]]:
    """
    Evaluates high-confidence deterministic commands directly with regex / rules.
    Returns (IntentType, extracted_parameters) with ZERO LLM token consumption.
    """
    msg_clean = message.strip()
    msg_low = msg_clean.lower()
    state = stored_state or {}
    last_result_ids = list(state.get("last_result_ids") or [])
    selected_ids = list(state.get("selected_property_ids") or [])

    # Hydrate from persistent property_context (survives long chats / restarts)
    pctx = state.get("property_context") if isinstance(state.get("property_context"), dict) else {}
    if pctx:
        if not selected_ids and pctx.get("active_property_id"):
            selected_ids = [pctx["active_property_id"]]
            state = dict(state)
            state["selected_property_ids"] = selected_ids
        if not last_result_ids:
            ref_map = pctx.get("reference_map") or {}
            batch = []
            for label in ("first property", "second property", "third property", "fourth property", "fifth property"):
                if ref_map.get(label):
                    batch.append(ref_map[label])
            last_result_ids = batch or list(pctx.get("recently_discussed_ids") or [])
            state = dict(state)
            state["last_result_ids"] = last_result_ids

    # 0.1 Lead Contact & Private Viewing Request Detection
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", message)
    phone_match = re.search(r"(\+?\d{1,4}[-.\s]?(?:\(?\d{1,4}\)?[-.\s]?)?(?:\d{2,4}[-.\s]?){2,4}\d{2,4})", message)
    viewing_keywords = [
        "viewing", "arrange a viewing", "schedule a viewing", "visit next month", "private viewing",
        "book a viewing", "tour the villa", "prezentacj", "oglądani", "umówić spotkanie",
        "นัดชม", "เข้าชม", "จองคิวชม"
    ]
    has_viewing_intent = any(k in msg_low for k in viewing_keywords)
    if email_match or phone_match or (has_viewing_intent and any(k in msg_low for k in ["name", "email", "phone", "contact", "telefon", "mail"])):
        name_match = re.search(r"(?:my name is|i am|nazywam się|imię:|name:)\s+([A-Z][a-zA-Z\'-]+(?:\s+[A-Z][a-zA-Z\'-]+)+)", message, re.I)
        search_f = extract_search_filters_deterministic(message)
        return IntentType.LEAD_CAPTURE, {
            "email": email_match.group(0) if email_match else None,
            "phone": phone_match.group(0).strip() if phone_match else None,
            "name": name_match.group(1).strip() if name_match else None,
            "location": search_f.get("location"),
            "property_type": search_f.get("property_type", "Villa"),
            "budget": search_f.get("max_price"),
            "is_viewing": has_viewing_intent
        }

    # 1. Show All Properties (Reset all filters)
    for pat in SHOW_ALL_PATTERNS:
        if re.search(pat, msg_low):
            return IntentType.PROPERTY_SEARCH, {"show_all": True}

    # 1.1 Show More / Pagination
    for pat in SHOW_MORE_PATTERNS:
        if re.search(pat, msg_low):
            curr_offset = state.get("pagination_offset", 0)
            return IntentType.SHOW_MORE, {
                "offset": curr_offset + state.get("limit", 4),
                "limit": state.get("limit", 4)
            }

    # 1.2 PDF / Brochure request — detect and extract target property ID
    for pat in PDF_EXPORT_PATTERNS:
        if re.search(pat, msg_low):
            # Try to extract explicit property ID from message
            pid_match = re.search(r"(prop-[a-z0-9\-]+)", msg_low)
            prop_id = pid_match.group(1).upper() if pid_match else None
            prop_name = None

            # Fallback: resolve ordinal / named references
            if not prop_id:
                if any(k in msg_low for k in ["first", "1st", "อันแรก", "pierwsz", "รายการแรก"]):
                    prop_id = last_result_ids[0] if last_result_ids else None
                elif any(k in msg_low for k in ["second", "2nd", "อันที่สอง", "drug", "รายการที่สอง"]):
                    prop_id = last_result_ids[1] if len(last_result_ids) >= 2 else None
                elif any(k in msg_low for k in ["third", "3rd", "อันที่สาม", "trzeci", "รายการที่สาม"]):
                    prop_id = last_result_ids[2] if len(last_result_ids) >= 3 else None
                else:
                    named = re.search(
                        r"\b(?:for|of|about|dla|do|o|สำหรับ|ของ)\s+([A-Za-z0-9][\w\s\-'&.]{2,80})",
                        msg_clean,
                        flags=re.IGNORECASE
                    )
                    if named:
                        prop_name = named.group(1).strip(" .,?!")
                        prop_id = prop_name  # resolve via Firestore/SQL by name
                    elif len(last_result_ids) == 1:
                        prop_id = last_result_ids[0]
                    elif state.get("selected_property_ids"):
                        prop_id = state["selected_property_ids"][0]

            return IntentType.BROCHURE_REQUEST, {
                "property_id": prop_id,
                "property_name": prop_name,
                "needs_clarification": prop_id is None
            }

    # 2. Property Comparison
    prop_matches_all = re.findall(r"prop-[a-z0-9\-]+", msg_low)
    if len(prop_matches_all) >= 2 and any(k in msg_low for k in ["compare", "comparison", "vs", "versus", "difference", "between", "porównaj", "porownaj", "เปรียบเทียบ", "เทียบ"]):
        return IntentType.PROPERTY_COMPARISON, {
            "property_ids": [p.upper() for p in prop_matches_all[:2]],
            "raw_query": msg_clean
        }

    for pat in COMPARISON_PATTERNS:
        m = re.search(pat, msg_low)
        if m:
            groups = m.groups()
            comp_ids = []
            if groups and groups[0] and "prop-" in groups[0].lower():
                comp_ids = [g.upper() for g in groups if g]
            elif "first" in msg_low or "1st" in msg_low or "1" in msg_low or "อันแรก" in msg_low or "รายการแรก" in msg_low:
                if "second" in msg_low or "2nd" in msg_low or "2" in msg_low or "อันที่สอง" in msg_low or "รายการที่สอง" in msg_low:
                    if len(last_result_ids) >= 2:
                        comp_ids = [last_result_ids[0], last_result_ids[1]]
                elif "third" in msg_low or "3rd" in msg_low or "3" in msg_low or "อันที่สาม" in msg_low or "รายการที่สาม" in msg_low:
                    if len(last_result_ids) >= 3:
                        comp_ids = [last_result_ids[0], last_result_ids[2]]
            elif "second" in msg_low and "third" in msg_low:
                if len(last_result_ids) >= 3:
                    comp_ids = [last_result_ids[1], last_result_ids[2]]
            elif "both" in msg_low or "them" in msg_low or "ทั้งหมด" in msg_low or "ทั้งสอง" in msg_low:
                comp_ids = last_result_ids[:2]

            return IntentType.PROPERTY_COMPARISON, {
                "property_ids": comp_ids,
                "raw_query": msg_clean
            }

    # 3. Property Details Lookup
    prop_matches = re.findall(r"prop-[a-z0-9\-]+", msg_low)
    if len(prop_matches) == 1 and not any(k in msg_low for k in ["compare", "comparison", "เปรียบเทียบ"]):
        return IntentType.PROPERTY_DETAILS, {
            "property_id": prop_matches[0].upper()
        }

    _DETAIL_SKIP_NAMES = {
        "phuket", "samui", "koh samui", "bangkok", "pattaya", "thailand", "tajlandia",
        "villas", "villa", "condo", "condominium", "properties", "property", "more",
        "options", "results", "listings"
    }
    for pat in DETAILS_PATTERNS:
        m = re.search(pat, msg_low, flags=re.IGNORECASE)
        if m:
            target_raw = (m.group(1) or "").strip()
            target = target_raw.upper()
            target_id = None
            if target.startswith("PROP-"):
                target_id = target
            elif target in ["FIRST", "1ST", "1", "อันแรก"] and len(last_result_ids) >= 1:
                target_id = last_result_ids[0]
            elif target in ["SECOND", "2ND", "2", "อันที่สอง"] and len(last_result_ids) >= 2:
                target_id = last_result_ids[1]
            elif target in ["THIRD", "3RD", "3", "อันที่สาม"] and len(last_result_ids) >= 3:
                target_id = last_result_ids[2]
            elif target_raw and target_raw.lower() not in _DETAIL_SKIP_NAMES and len(target_raw) >= 3:
                # Named project follow-up — resolve via Firestore/SQL by name (preserve original casing from message)
                name_match = re.search(pat, msg_clean, flags=re.IGNORECASE)
                named = (name_match.group(1).strip() if name_match else target_raw).strip(" .,?!")
                return IntentType.PROPERTY_DETAILS, {
                    "property_name": named,
                    "property_id": named,
                }

            if target_id:
                return IntentType.PROPERTY_DETAILS, {
                    "property_id": target_id
                }

    # Anaphoric + active property context (supports 100+ follow-ups without renaming the property)
    active_id = (selected_ids[0] if selected_ids else None) or (pctx.get("active_property_id") if pctx else None)
    if any(phrase in msg_low for phrase in [
        "that one", "give me that", "show that", "tell me about it", "อันนั้น", "ขออันนี้", "ดูอันนั้น",
        "this property", "that property", "this villa", "that villa", "its price", "its amenities",
        "its location", "does it", "is it ", "has it", "what about it", "does it also",
    ]):
        focus = active_id or (last_result_ids[0] if last_result_ids else None)
        if focus:
            return IntentType.PROPERTY_DETAILS, {
                "property_id": focus,
                "focus_question": msg_clean,
                "from_property_context": True,
            }

    # Attribute / fact questions about a focused or named property (prevents false new searches)
    attr_q = bool(re.search(
        r"(?:does|do|has|have|is|are|what(?:'s| is)|how much|price of|cena|czy|ile|มี|ราคา|เท่าไหร่)",
        msg_low
    ))
    amenity_or_fact = bool(re.search(
        r"(?:pool|swimming|beach|price|cost|bedroom|bedrooms|area|size|sqm|location|status|parking|"
        r"amenit|available|availability|basen|sypialn|cena|สระ|ห้องนอน|ราคา|ทำเล|ที่จอด)",
        msg_low
    ))
    if attr_q and amenity_or_fact:
        if any(k in msg_low for k in ["first one", "1st one", "the first", "pierwsz", "อันแรก"]) and last_result_ids:
            return IntentType.PROPERTY_DETAILS, {"property_id": last_result_ids[0], "focus_question": msg_clean}
        if any(k in msg_low for k in ["second one", "2nd one", "the second", "drug", "อันที่สอง"]) and len(last_result_ids) >= 2:
            return IntentType.PROPERTY_DETAILS, {"property_id": last_result_ids[1], "focus_question": msg_clean}
        if any(k in msg_low for k in ["third one", "3rd one", "the third", "trzeci", "อันที่สาม"]) and len(last_result_ids) >= 3:
            return IntentType.PROPERTY_DETAILS, {"property_id": last_result_ids[2], "focus_question": msg_clean}
        if any(k in msg_low for k in ["fourth one", "4th one", "the fourth"]) and len(last_result_ids) >= 4:
            return IntentType.PROPERTY_DETAILS, {"property_id": last_result_ids[3], "focus_question": msg_clean}
        if any(k in msg_low for k in ["fifth one", "5th one", "the fifth"]) and len(last_result_ids) >= 5:
            return IntentType.PROPERTY_DETAILS, {"property_id": last_result_ids[4], "focus_question": msg_clean}
        if any(k in msg_low for k in [
            "this property", "that property", "this villa", "that villa", "this one", "that one",
            "tej nieruchomości", "ta willa", "โครงการนี้", "วิลล่านี้", "does it", "has it", " its"
        ]) or (active_id and not re.search(r"\b(?:phuket|samui|bangkok|pattaya|thailand)\b", msg_low)
               and "prop-" not in msg_low):
            focus = active_id or (last_result_ids[0] if last_result_ids else None)
            if focus:
                return IntentType.PROPERTY_DETAILS, {
                    "property_id": focus,
                    "focus_question": msg_clean,
                    "from_property_context": True,
                }
        named_fact = re.search(
            r"(?:does|do|has|have|what(?:'s| is)|how much|price of|cena|czy)\s+(.+?)\s+(?:have|has|include|with|cost|price|pool|beach|bedroom|อยู่|มี)",
            msg_low
        )
        if not named_fact:
            named_fact = re.search(
                r"(?:does|do)\s+(.+?)\s+have\s+",
                msg_low
            )
        if named_fact:
            named = named_fact.group(1).strip(" .,?!")
            named = re.sub(r"^(?:the|a|an)\s+", "", named).strip()
            if named and len(named) >= 3 and named.lower() not in _DETAIL_SKIP_NAMES and "property" not in named.lower():
                return IntentType.PROPERTY_DETAILS, {
                    "property_name": named,
                    "property_id": named,
                    "focus_question": msg_clean,
                }

    # Named property comparison: "Compare Larimar Villa and Nile Residence"
    if any(k in msg_low for k in ["compare", "comparison", "vs", "versus", "porównaj", "porownaj", "เปรียบเทียบ"]):
        named_vs = re.search(
            r"(?:compare|comparison|porównaj|porownaj|เปรียบเทียบ)\s+(.+?)\s+(?:and|with|to|vs|versus|i|z|oraz|กับ|และ)\s+(.+?)(?:\s*\?|$)",
            msg_clean,
            flags=re.IGNORECASE
        )
        if named_vs:
            a = named_vs.group(1).strip(" .,?!")
            b = named_vs.group(2).strip(" .,?!")
            a = re.sub(r"^(?:the|a|an)\s+", "", a, flags=re.I).strip()
            b = re.sub(r"^(?:the|a|an)\s+", "", b, flags=re.I).strip()
            if a and b and a.lower() not in _DETAIL_SKIP_NAMES and b.lower() not in _DETAIL_SKIP_NAMES:
                return IntentType.PROPERTY_COMPARISON, {
                    "property_ids": [a, b],
                    "raw_query": msg_clean,
                }

    # 4. Filter Refinement on active search
    has_refine_clue = any(re.search(p, msg_low) for p in REFINE_PATTERNS)
    is_short = len(msg_low.split()) <= 8
    if has_refine_clue and is_short and (state.get("active_filters") or state.get("location") or state.get("property_type")):
        refinements = {}
        # Cheaper delta
        if "cheaper" in msg_low or "tańsze" in msg_low or "ถูกกว่า" in msg_low:
            curr_max = state.get("max_price") or state.get("budget_max")
            if curr_max:
                refinements["max_price"] = float(curr_max) * 0.85
            else:
                refinements["max_price"] = 8000000.0

        # Specific price limit
        p_match = re.search(r"(?:under|below|max|budget|ไม่เกิน|ต่ำกว่า)\s*(\d+(?:\.\d+)?)\s*(?:m|million|ล้าน|ล้านบาท)?\s*(?:thb|baht|บาท)?", msg_low)
        if p_match and "bed" not in p_match.group(0) and "ห้อง" not in p_match.group(0):
            val = float(p_match.group(1))
            refinements["max_price"] = val * 1_000_000 if val < 500 else val

        # Pool modification
        if "without pool" in msg_low or "remove pool" in msg_low or "no pool" in msg_low or "ไม่มีสระ" in msg_low or "ไม่เอาสระ" in msg_low:
            refinements["has_pool"] = False
        elif "pool" in msg_low or "basen" in msg_low or "สระว่ายน้ำ" in msg_low or "สระ" in msg_low:
            refinements["has_pool"] = True

        # Sea view
        if "sea view" in msg_low or "widok na morze" in msg_low or "วิวทะเล" in msg_low:
            refinements["sea_view"] = True

        # Location switch
        for kw, canonical in [
            ("samui", "Koh Samui"), ("phuket", "Phuket"), ("bangkok", "Bangkok"),
            ("layan", "Layan"), ("bang tao", "Bang Tao"), ("karon", "Karon"),
            ("maenam", "Maenam"), ("phangan", "Koh Phangan"), ("ภูเก็ต", "Phuket"),
            ("สมุย", "Koh Samui"), ("บางเทา", "Bang Tao"), ("แม่น้ำ", "Maenam")
        ]:
            if kw in msg_low:
                refinements["location"] = canonical
                break

        if refinements:
            return IntentType.REFINE_FILTER, refinements

    # 5. General Real Estate Informational Questions (Priority over broad search triggers)
    general_topics = [
        "foreign freehold", "leasehold", "foreign ownership", "can a foreigner", "can foreigners",
        "buying process", "taxes", "chanote", "due diligence", "rental yield",
        "roi", "ownership", "common fee", "cam fee", "transfer fee", "how to buy",
        "how does property ownership", "how does buying", "ownership & buying", "ownership and buying",
        "buying work in thailand", "property laws", "legal requirements",
        "popular", "neighborhood", "areas", "places to live", "where to live", "beaches",
        "กรรมสิทธิ์", "ชาวต่างชาติ", "ต่างชาติซื้อ", "สัญญาเช่า", "โฉนด", "ขั้นตอนการซื้อ",
        "ค่าส่วนกลาง", "ผลตอบแทน", "กฎหมายที่ดิน", "ซื้อบ้านในไทย", "ซื้อคอนโดในไทย"
    ]
    if any(t in msg_low for t in general_topics) and not any(k in msg_low for k in ["find villa", "show me villa", "find condo", "show me condo", "search for", "ค้นหาวิลล่า", "ค้นหาคอนโด"]):
        return IntentType.GENERAL_QUESTION, {}

    # 6. Direct search keywords and structured filter presence check
    search_keywords = [
        "show me", "find", "search", "looking for", "villas in", "condos in",
        "properties in", "apartments in", "pool villa", "luxury villa", "penthouse",
        "villa in", "condo in", "houses in", "apartments in", "buy a villa", "buy a condo",
        "cheapest", "lowest price", "most affordable", "najtańszy", "najtanszy",
        "anything in", "something in", "options in", "what about", "how about",
        "coś w", "cos w", "cokolwiek w", "oferty w",
        "ค้นหา", "แสดง", "ขอดู", "หาวิลล่า", "หาคอนโด", "พูลวิลล่าใน", "คอนโดใน",
        "วิลล่าใน", "อสังหาริมทรัพย์ใน", "ถูกที่สุด", "ราคาถูกที่สุด", "มีอะไรใน"
    ]
    extracted_test = extract_search_filters_deterministic(message)
    has_search_intent = any(k in msg_low for k in search_keywords) or (
        ("location" in extracted_test) and ("property_type" in extracted_test or "bedrooms" in extracted_test or "max_price" in extracted_test)
    ) or ("property_type" in extracted_test and any(k in msg_low for k in ["cheapest", "lowest", "affordable", "najtańszy", "najtanszy", "ถูกที่สุด", "ราคาต่ำสุด"])) or bool(extracted_test.get("foreign_location"))
    if has_search_intent:
        return IntentType.PROPERTY_SEARCH, extracted_test

    return None, {}


def extract_search_filters_deterministic(message: str) -> Dict[str, Any]:
    """
    Extracts structured search criteria from natural language without calling an LLM.
    Supports English, Polish, and Thai expressions, including real-time currency conversion (EUR, USD, PLN -> THB).
    """
    msg_low = message.lower().strip()
    filters: Dict[str, Any] = {}

    FX_RATES = {
        "pln": 8.5, "zł": 8.5, "zl": 8.5, "zlote": 8.5, "złotych": 8.5,
        "eur": 38.0, "euro": 38.0, "€": 38.0,
        "usd": 35.0, "dollars": 35.0, "$": 35.0,
        "thb": 1.0, "baht": 1.0, "บาท": 1.0
    }

    # Location (English / Polish / Thai)
    destinations = [
        ("choeng thale", "Bang Tao"), ("cherngtalay", "Bang Tao"), ("เชิงทะเล", "Bang Tao"),
        ("bang tao", "Bang Tao"), ("bangtao", "Bang Tao"), ("บางเทา", "Bang Tao"),
        ("layan", "Layan"), ("ลายัน", "Layan"),
        ("karon", "Karon"), ("กะรน", "Karon"),
        ("rawai", "Rawai"), ("ราไวย์", "Rawai"),
        ("kamala", "Kamala"), ("กมลา", "Kamala"),
        ("surin", "Surin"), ("สุรินทร์", "Surin"),
        ("patong", "Patong"), ("ป่าตอง", "Patong"),
        ("kata", "Kata"), ("กะตะ", "Kata"),
        ("nai harn", "Nai Harn"), ("ในหาน", "Nai Harn"),
        ("phuket", "Phuket"), ("ภูเก็ต", "Phuket"),
        ("maenam", "Maenam"), ("แม่น้ำ", "Maenam"),
        ("bophut", "Bophut"), ("บ่อผุด", "Bophut"),
        ("chaweng", "Chaweng"), ("เฉวง", "Chaweng"),
        ("lamai", "Lamai"), ("ละไม", "Lamai"),
        ("choeng mon", "Choeng Mon"), ("เชิงมน", "Choeng Mon"),
        ("plai laem", "Plai Laem"), ("ปลายแหลม", "Plai Laem"),
        ("samui", "Koh Samui"), ("koh samui", "Koh Samui"), ("เกาะสมุย", "Koh Samui"), ("สมุย", "Koh Samui"),
        ("phangan", "Koh Phangan"), ("koh phangan", "Koh Phangan"), ("เกาะพะงัน", "Koh Phangan"), ("พะงัน", "Koh Phangan"),
        ("pattaya", "Pattaya"), ("พัทยา", "Pattaya"),
        ("bangkok", "Bangkok"), ("กรุงเทพ", "Bangkok"), ("กรุงเทพฯ", "Bangkok"),
        ("hua hin", "Hua Hin"), ("หัวหิน", "Hua Hin"),
        ("krabi", "Krabi"), ("กระบี่", "Krabi"),
        ("chiang mai", "Chiang Mai"), ("เชียงใหม่", "Chiang Mai"),
        ("phang nga", "Phang Nga"), ("พังงา", "Phang Nga")
    ]
    for kw, val in destinations:
        if kw in msg_low:
            filters["location"] = val
            break

    if not filters.get("location"):
        foreign_destinations = [
            ("india", "India"), ("indie", "India"), ("indii", "India"), ("mumbai", "India"),
            ("delhi", "India"), ("bangalore", "India"), ("goa", "India"),
            ("dubai", "Dubai"), ("ดูไบ", "Dubai"),
            ("warsaw", "Poland"), ("warszawa", "Poland"), ("poland", "Poland"), ("polsce", "Poland"),
            ("spain", "Spain"), ("hiszpania", "Spain"), ("hiszpanii", "Spain"),
            ("miami", "USA"), ("florida", "USA"), ("new york", "USA"), ("usa", "USA"),
            ("london", "UK"), ("londyn", "UK"), ("uk", "UK"),
            ("bali", "Bali"), ("indonesia", "Indonesia"),
            ("cyprus", "Cyprus"), ("cypr", "Cyprus"),
            ("greece", "Greece"), ("grecja", "Greece"),
            ("italy", "Italy"), ("włochy", "Italy"), ("wlochy", "Italy"),
            ("portugal", "Portugal"), ("portugalia", "Portugal"),
            ("france", "France"), ("francja", "France"),
            ("turkey", "Turkey"), ("turcja", "Turkey"),
            ("germany", "Germany"), ("niemcy", "Germany"),
            ("singapore", "Singapore"), ("australia", "Australia"), ("canada", "Canada"),
            ("อินเดีย", "India"), ("ไมอามี", "USA"), ("สเปน", "Spain")
        ]
        for kw, val in foreign_destinations:
            if re.search(r'\b' + re.escape(kw) + r'\b', msg_low):
                filters["foreign_location"] = val
                break

    broad_thailand_phrases = [
        "in thailand", "across thailand", "around thailand", "throughout thailand",
        "all thailand", "anywhere in thailand", "all areas", "everywhere",
        "w tajlandii", "w całej tajlandii", "ทั่วไทย", "ในไทย", "ในประเทศไทย",
        "all destinations", "całej tajlandii"
    ]
    if any(phrase in msg_low for phrase in broad_thailand_phrases) or ("thailand" in msg_low and not filters.get("location")):
        if not filters.get("location"):
            filters["is_broad_thailand"] = True
            filters["location"] = None

    # Property Type (English / Polish / Thai)
    has_villa = any(k in msg_low for k in ["villa", "villas", "willa", "wille", "willi", "วิลล่า", "พูลวิลล่า", "บ้าน"])
    has_condo = any(k in msg_low for k in ["condo", "condos", "condominium", "condominiums", "apartment", "apartments", "apartament", "apartamenty", "mieszkanie", "mieszkania", "คอนโด", "คอนโดมิเนียม", "อพาร์ทเมนท์", "อพาร์ตเมนต์"])
    if has_villa and has_condo:
        filters["property_type"] = None
        filters["is_multiple_types"] = True
    elif has_villa:
        filters["property_type"] = "Villa"
    elif has_condo:
        filters["property_type"] = "Condominium"

    # Bedrooms
    bed_match = re.search(r"(\d+)\s*(?:bed|bedroom|br|sypialn|pokoj|ห้องนอน|ห้อง)", msg_low)
    if bed_match:
        filters["bedrooms"] = int(bed_match.group(1))

    # Bathrooms
    bath_match = re.search(r"(\d+)\s*(?:bath|bathroom|lazienk|łazienk|ห้องน้ำ)", msg_low)
    if bath_match:
        filters["bathrooms"] = int(bath_match.group(1))

    # Max Price / Budget with Currency Conversion
    # A. Prefix currency (e.g. $500,000, €1,000,000)
    pfx_m = re.search(r"([$€£])\s*([0-9,.]+)\s*(k|m|million|mln)?", msg_low)
    if pfx_m:
        symbol = pfx_m.group(1)
        val = float(pfx_m.group(2).replace(",", ""))
        unit = (pfx_m.group(3) or "").lower()
        if unit in ["m", "million", "mln"] or val < 500:
            val *= 1_000_000
        elif unit == "k":
            val *= 1_000
        rate = FX_RATES.get(symbol, 35.0)
        filters["max_price"] = val * rate

    # B. Suffix currency (e.g. 4.5 million PLN, 1 million EUR, 30,000,000 THB)
    if "max_price" not in filters:
        sfx_m = re.search(r"([0-9,.]+)\s*(k|m|million|mln|milionów|ล้าน)?\s*(pln|eur|usd|thb|baht|บาท|zł|zl|dollars|euro|zlote|złotych)", msg_low)
        if sfx_m:
            val = float(sfx_m.group(1).replace(",", ""))
            unit = (sfx_m.group(2) or "").lower()
            curr = (sfx_m.group(3) or "").lower()
            if unit in ["m", "million", "mln", "milionów", "ล้าน"] or (val < 500 and curr in ["pln", "eur", "usd", "thb"]):
                val *= 1_000_000
            elif unit == "k":
                val *= 1_000
            rate = FX_RATES.get(curr, 1.0)
            filters["max_price"] = val * rate

    # C. Keyword-based budget without explicit currency (defaults to THB)
    if "max_price" not in filters:
        p_match = re.search(r"(?:under|below|max|budget|up to|poniżej|ponizej|do|maksymalnie|budżet|budzet|ไม่เกิน|ต่ำกว่า|งบ|สูงสุด|ไม่เกินงบ)\s*(?:is|\s*[:=]|\s+around|\s+about)?\s*([0-9,.]+)\s*(m|million|mln|milionów|ล้าน|ล้านบาท|k)?\s*(?:thb|baht|บาท)?", msg_low)
        if p_match and "bed" not in p_match.group(0) and "ห้อง" not in p_match.group(0):
            val = float(p_match.group(1).replace(",", ""))
            unit = (p_match.group(2) or "").lower()
            if unit in ["m", "million", "mln", "milionów", "ล้าน", "ล้านบาท"] or val < 500:
                val *= 1_000_000
            elif unit == "k":
                val *= 1_000
            filters["max_price"] = val

    # Min Price
    min_match = re.search(r"(?:from|above|minimum|min|od|powyżej|powyzej|ตั้งแต่|มากกว่า|เริ่มต้น)\s*(?:is|\s*[:=])?\s*([0-9,.]+)\s*(m|million|mln|milionów|ล้าน|ล้านบาท|k)?\s*(?:thb|baht|บาท)?", msg_low)
    if min_match and "bed" not in min_match.group(0) and "ห้อง" not in min_match.group(0):
        val = float(min_match.group(1).replace(",", ""))
        unit = (min_match.group(2) or "").lower()
        if unit in ["m", "million", "mln", "milionów", "ล้าน", "ล้านบาท"] or val < 500:
            val *= 1_000_000
        elif unit == "k":
            val *= 1_000
        filters["min_price"] = val

    # Amenities
    if "pool" in msg_low or "basen" in msg_low or "สระว่ายน้ำ" in msg_low or "สระ" in msg_low or "พูล" in msg_low:
        filters["has_pool"] = True
    if "beach" in msg_low or "beachfront" in msg_low or "plaż" in msg_low or "plaz" in msg_low or "ติดหาด" in msg_low or "ชายหาด" in msg_low or "ติดทะเล" in msg_low:
        filters["beach_access"] = True
    if "sea view" in msg_low or "widok na morze" in msg_low or "วิวทะเล" in msg_low:
        filters["sea_view"] = True

    return filters
