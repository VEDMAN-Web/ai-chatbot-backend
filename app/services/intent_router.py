import re
import json
import logging
from difflib import SequenceMatcher, get_close_matches
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
    r"^any\s+more(?:\s+options|\s+villas|\s+properties)?\??$",
    r"^more$",
    # Alternatives / reject-current follow-ups (must beat details + off-topic)
    r"\b(?:show\s+me\s+)?(?:any|some)\s+other(?:\s+(?:propert(?:y|ies)|villas?|condos?|options?|ones?|listings?))?\b",
    r"\bshow\s+me\s+(?:any\s+)?(?:other|others|different)(?:\s+\w+){0,6}\b",
    r"\b(?:different|other)\s+(?:ones?|options?|propert(?:y|ies)|villas?|condos?|listings?)\b",
    r"\bsomething\s+(?:else|different)\b",
    r"\binstead\s+of\s+(?:this|that|it|these|the\s+current)\b",
    r"\bi\s+(?:don'?t|do\s+not)\s+like\s+(?:this|that|it)\b",
    r"\bnot\s+(?:this|that)\s+(?:one|property|villa|condo)?\b",
    r"^any\s+other\??$",
    r"^others?\??$",
    r"^something\s+else\??$",
    r"^(?:pokaż|pokaz|zobacz|daj\s+mi)?\s*(?:więcej|wiecej|kolejne)(?:\s+ofert|\s+willi|\s+apartamentów|\s+wyników)?$",
    r"^(?:następna|nastepna)\s*(?:strona|oferta)?$",
    r"^więcej$",
    r"^wiecej$",
    r"\b(?:inne|innych|coś\s+innego|cos\s+innego|inne\s+oferty|inne\s+propozycje)\b",
    r"\bnie\s+podoba\s+mi\s+się\s+(?:to|ta|ten)\b",
    r"^(?:ดูเพิ่มเติม|เพิ่มเติม|หน้าถัดไป|ข้อเสนอถัดไป|แสดงเพิ่ม|ขอเพิ่ม|ดูเพิ่ม)$",
    r"^(?:ดู|ขอ)?\s*(?:เพิ่มเติม|เพิ่มอีก|รายการอื่น)$",
    r"\b(?:อย่างอื่น|รายการอื่น|ตัวเลือกอื่น|ไม่เอาอันนี้)\b",
    r"\b(?:aur\s+dikhao|aur\s+dikhaiye|biju\s+batavo|vadhu\s+batavo|vadhu\s+dekhad)\b",
]

_SHOW_MORE_LEAD = re.compile(
    r"^(?:(?:no|nope|nah|okay|ok|yes|yeah|please|hey|hmm|then|and|so|can you|could you)[,!\s]+)+",
    re.I,
)
_SHOW_MORE_TRAIL = re.compile(r"(?:[,!\s]+(?:please|thanks|thank you|again))+\s*$", re.I)
_SHOW_MORE_CORE = re.compile(
    r"^(?:"
    r"(?:show|see|give|load|fetch|display|list)\s+(?:me\s+)?(?:some\s+|the\s+)?"
    r"(?:more|next|other|another)(?:\s+\w+){0,4}"
    r"|more(?:\s+please)?|next(?:\s+ones?)?|others?|another(?:\s+one)?|continue|keep going"
    r"|more\s+results|more\s+options|see\s+more|show\s+me\s+some\s+more"
    r"|give\s+me\s+more\s+options|show\s+me\s+the\s+next\s+ones|next\s+ones"
    r"|aur\s+dikhao|biju\s+batavo|vadhu\s+batavo"
    r")$",
    re.I,
)


def strip_show_more_fillers(message: str) -> str:
    """Drop leading 'no / okay / please / can you' and trailing 'please / thanks'."""
    text = re.sub(r"[?!.]+$", "", (message or "").strip().lower()).strip()
    previous = None
    while previous != text:
        previous = text
        text = _SHOW_MORE_LEAD.sub("", text).strip()
        text = _SHOW_MORE_TRAIL.sub("", text).strip()
    return text


def message_requests_show_more(message: str, *, listings_were_shown: bool = False) -> bool:
    """
    True for pagination, including 'no, show me more' and 'okay show me more'.
    After listings, a short follow-up that only says more/next/other is also show-more.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    for pat in SHOW_MORE_PATTERNS:
        if re.search(pat, low):
            return True
    stripped = strip_show_more_fillers(raw)
    if _SHOW_MORE_CORE.match(stripped):
        return True
    if listings_were_shown and not re.search(r"\b(about|tell|details|info|specs)\b", stripped):
        words = re.findall(r"[a-zA-Z']+", stripped)
        if 0 < len(words) <= 6 and not re.search(
            r"\b(prime minister|president|recipe|homework|who is|capital of)\b", stripped
        ):
            if any(w in words for w in ("more", "next", "other", "others", "another", "else", "again")):
                return True
    return False

# Reject the currently focused property and ask for alternatives
REJECT_ACTIVE_PATTERNS = [
    r"\binstead\s+of\s+(?:this|that|it|these|the\s+current)\b",
    r"\bi\s+(?:don'?t|do\s+not)\s+like\s+(?:this|that|it)\b",
    r"\bnot\s+(?:this|that)\s+(?:one|property|villa|condo)?\b",
    r"\bnie\s+podoba\s+mi\s+się\s+(?:to|ta|ten)\b",
    r"\bไม่เอาอันนี้\b",
]

# Canonical Thailand destinations for exact + fuzzy location resolution
LOCATION_ALIASES: List[Tuple[str, str]] = [
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
    ("koh samui", "Koh Samui"), ("samui", "Koh Samui"), ("เกาะสมุย", "Koh Samui"), ("สมุย", "Koh Samui"),
    ("koh phangan", "Koh Phangan"), ("phangan", "Koh Phangan"), ("เกาะพะงัน", "Koh Phangan"), ("พะงัน", "Koh Phangan"),
    ("pattaya", "Pattaya"), ("พัทยา", "Pattaya"),
    ("bangkok", "Bangkok"), ("กรุงเทพ", "Bangkok"), ("กรุงเทพฯ", "Bangkok"),
    ("hua hin", "Hua Hin"), ("หัวหิน", "Hua Hin"),
    ("krabi", "Krabi"), ("กระบี่", "Krabi"),
    ("chiang mai", "Chiang Mai"), ("เชียงใหม่", "Chiang Mai"),
    ("phang nga", "Phang Nga"), ("พังงา", "Phang Nga"),
    # Cambodia (priority market) and Maldives. Polish stems cover declensions (Kambodży, Kambodzie).
    ("phnom penh", "Phnom Penh"), ("phnom-penh", "Phnom Penh"), ("pnom pen", "Phnom Penh"),
    ("sihanoukville", "Sihanoukville"), ("siem reap", "Siem Reap"), ("kampot", "Kampot"),
    ("cambodia", "Cambodia"), ("kambodż", "Cambodia"), ("kambodz", "Cambodia"),
    ("maldives", "Maldives"), ("malediw", "Maldives"),
]

_FUZZY_LOCATION_STOPWORDS = frozenset({
    "okay", "ok", "yes", "yeah", "please", "thanks", "thank", "the", "and", "for",
    "with", "this", "that", "show", "me", "find", "want", "looking", "villa", "villas",
    "condo", "property", "properties", "budget", "under", "more", "other", "any",
    "something", "else", "instead", "like", "dont", "not", "in", "at", "on",
    "near", "around", "about", "how", "what", "where", "who", "can", "could", "would",
    "new", "newest", "latest", "free", "current", "chat", "previous", "earlier", "shown",
})


def fuzzy_resolve_location(message: str, cutoff: float = 0.74) -> Optional[str]:
    """
    Resolve a Thailand city/area from exact substring or fuzzy token match.
    Returns canonical location (e.g. 'Phuket') or None. Never invents non-catalog places.
    """
    msg = (message or "").lower().strip()
    if not msg:
        return None

    for kw, canon in sorted(LOCATION_ALIASES, key=lambda kv: len(kv[0]), reverse=True):
        if kw in msg:
            return canon

    latin_aliases = [(kw, canon) for kw, canon in LOCATION_ALIASES if re.search(r"[a-z]", kw)]
    alias_keys = [kw for kw, _ in latin_aliases]
    tokens = re.findall(r"[a-zA-Z]{3,}", msg)
    bigrams = [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]

    candidates: List[Tuple[float, str]] = []
    for tok in tokens + bigrams:
        if tok in _FUZZY_LOCATION_STOPWORDS:
            continue
        close = get_close_matches(tok, alias_keys, n=1, cutoff=cutoff)
        if close:
            score = SequenceMatcher(None, tok, close[0]).ratio()
            candidates.append((score, close[0]))
            continue
        best_kw, best_r = None, 0.0
        for kw in alias_keys:
            if abs(len(kw) - len(tok)) > 3:
                continue
            r = SequenceMatcher(None, tok, kw).ratio()
            if r > best_r:
                best_r, best_kw = r, kw
        if best_kw and best_r >= cutoff:
            candidates.append((best_r, best_kw))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], -len(x[1])))
    best_kw = candidates[0][1]
    for kw, canon in latin_aliases:
        if kw == best_kw:
            return canon
    return None


def message_rejects_active_property(message: str) -> bool:
    msg = (message or "").lower().strip()
    return any(re.search(p, msg) for p in REJECT_ACTIVE_PATTERNS)

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
    # UI / quick-reply: "View details for isla-kata-sea-view-villa"
    r"(?:view\s+)?details\s+for\s+([a-z0-9][a-z0-9\-_]{2,120})",
    # Ordinals (singular + plural): "first property", "first properties", "the first ones"
    r"(?:tell\s+me\s+more\s+about|tell\s+me\s+about|details\s+for|specs\s+for|more\s+(?:details|info)\s+(?:about|on)|info\s+on)\s+(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+(?:ones?|propert(?:y|ies)|villas?|condos?|listings?)",
    r"^(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+(?:ones?|propert(?:y|ies)|villas?|condos?|listings?)?\s*[\?\!\.]*$",
    # Named property follow-ups: "can you tell me more about Mono Champaca?" / "ok show me more about Pirak Villa"
    r"(?:(?:can\s+you|could\s+you|please|ok|okay)\s+)?(?:tell\s+me\s+more\s+about|tell\s+me\s+about|show\s+me\s+more\s+about|more\s+(?:details|info)\s+(?:about|on|for)|details\s+(?:about|for|on)|specs\s+for|info\s+(?:about|on))\s+(?:the\s+)?([a-z0-9][\w\s\-'&.]{2,80}?)\s*[\?\!\.]*$",
    r"(?:pokaż|pokaz|szczegóły|szczegoly|informacje\s+o)\s+(PROP-[A-Z0-9\-]+)",
    r"(?:pokaż|pokaz|szczegóły|szczegoly|więcej\s+o|opowiedz\s+o|powiedz\s+więcej\s+o)\s+(?:tę|ta|tą|o\s+)?([a-ząćęłńóśźż0-9][\w\s\-'&.]{2,80}?)\s*[\?\!\.]*$",
    r"(?:pokaż|pokaz|szczegóły|szczegoly)\s+(?:tę|ta|tą)\s+(?:pierwszą|druga|trzecią|1|2|3)",
    r"^(?:ta\s+)?(pierwsza|druga|trzecia)\s+oferta\s*[\?\!\.]*$",
    r"(?:ดูรายละเอียด|ขอรายละเอียด|ข้อมูล)\s+(PROP-[A-Z0-9\-]+)",
    r"(?:ดูรายละเอียด|ขอข้อมูล|บอกเพิ่มเติมเกี่ยวกับ|รายละเอียด)\s+([a-z0-9ก-๙][\w\s\-'&.ก-๙]{2,80}?)\s*[\?\!\.]*$",
    r"(?:ดูรายละเอียด|ขอข้อมูล|ดู)\s+(?:อันแรก|อันที่สอง|อันที่สาม|1|2|3)",
    r"^(?:อันแรก|อันที่สอง|อันที่สาม)\s*[\?\!\.]*$"
]

REFINE_PATTERNS = [
    r"(?:show\s+me\s+)?cheaper(?:\s+ones|\s+villas|\s+options)?",
    r"(?:make\s+it\s+)?cheaper",
    r"(?:under|below|max|budget|only\s+under)\s*(\d+(?:\.\d+)?)\s*(?:m|million)?\s*(?:thb|baht)?",
    r"(?:with|add)\s+(?:a\s+)?(?:private\s+)?pool",
    r"(?:which|that|ones?\s+that)\s+have\s+(?:a\s+)?(?:private\s+)?pool",
    r"(?:which|that)\s+have\s+(?:a\s+)?(?:private\s+)?(?:gym|parking|sea\s*view)",
    r"(?:have|has)\s+(?:a\s+)?(?:private\s+)?pool\b",
    r"(?:private\s+)?pool\s*\??$",
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


# Words that are filter vocabulary, not project names (EN / PL / TH)
_DETAIL_SKIP_NAMES = {
    "phuket", "samui", "koh samui", "bangkok", "pattaya", "thailand", "tajlandia",
    "villas", "villa", "condo", "condominium", "properties", "property", "more",
    "options", "results", "listings", "first properties", "second properties",
    "third properties", "first ones", "second ones", "third ones",
    "willa", "wille", "willi", "willę", "willą", "willy",
    "apartament", "apartamenty", "apartamentów", "apartamentow",
    "mieszkanie", "mieszkania", "nieruchomości", "nieruchomosci",
    "oferty", "ofert", "kondominium",
}

_FILTER_TYPE_TOKENS = {
    "villa", "villas", "willa", "wille", "willi", "willę", "willy", "willą", "willem",
    "condo", "condos", "condominium", "condominiums",
    "apartment", "apartments", "apartament", "apartamenty", "apartamentów", "apartamentow",
    "mieszkanie", "mieszkania", "mieszkań", "mieszkán",
    "property", "properties", "nieruchomość", "nieruchomosc", "nieruchomości", "nieruchomosci",
    "listing", "listings", "oferta", "oferty", "ofert",
    "house", "houses", "dom", "domy",
    "penthouse", "studio", "studios",
    "วิลล่า", "คอนโด", "คอนโดมิเนียม", "อพาร์ทเมนท์", "บ้าน",
}

# Amenity / feature words — never treat as a property name
_AMENITY_NAME_TOKENS = {
    "private", "pool", "pools", "gym", "fitness", "parking", "garage", "sea", "view",
    "seaview", "beach", "beachfront", "spa", "sauna", "elevator", "lift", "balcony",
    "terrace", "garden", "furnished", "unfurnished", "wifi", "internet",
    "basen", "prywatny", "prywatnym", "siłownia", "silownia", "garaż", "garaz",
    "widok", "morze", "plaża", "plaza",
    "สระ", "สระว่ายน้ำ", "ยิม", "ฟิตเนส", "ที่จอด", "วิวทะเล", "ชายหาด",
    "which", "ones", "have", "has", "with", "without", "any", "that",
}

# Question / generic words that can never be a property name (EN / PL / TH)
_PROPERTY_NAME_STOPWORDS = frozenset({
    "you", "your", "yours", "there", "anything", "something", "everything", "nothing",
    "any", "one", "ones", "it", "this", "that", "these", "those",
    "what", "which", "who", "where", "how", "do", "does", "have", "has", "got",
    "is", "are", "some", "other", "others", "more", "else",
    "bigger", "smaller", "cheaper", "better", "largest", "smallest",
    "properties", "property", "villas", "villa", "condos", "condo", "apartments",
    "listings", "options", "places", "homes", "listing", "apartment",
    # Polish
    "masz", "macie", "czy", "coś", "cos", "jest", "są", "sa", "jakie", "jaki", "jaka",
    "czegoś", "czegos", "nic", "wszystko", "inne", "innych",
    # Thai
    "มี", "ไหม", "คุณ", "อะไร", "ที่ไหน", "ไหมครับ", "ไหมคะ",
})

_FILTER_WORD_EXTRAS = frozenset({
    "bedroom", "bedrooms", "bed", "beds", "br", "sypialnia", "sypialnie", "sypialni",
    "ห้องนอน", "ห้อง", "budget", "under", "below", "above", "over", "million", "mln",
    "thb", "baht", "pool", "private", "sea", "view", "gym", "parking", "beach",
    "or", "more", "fewer", "least", "most", "minimum", "maximum", "between", "up", "to",
    "with", "without", "a", "an", "the", "plus", "lub", "więcej", "wiecej", "mniej",
    "co", "najmniej", "od", "do", "ขึ้นไป", "อย่างน้อย",
    "phuket", "samui", "bangkok", "pattaya", "thailand", "layan", "karon",
})


def is_valid_property_name_candidate(named: str) -> bool:
    """
    True when `named` may be treated as a property name:
    (a) name-index confident match, or (b) slug / PROP- id, or
    (c) contains at least one token that is not a stop/filter word.
    Candidates made only of stop + filter words are rejected.
    """
    raw = (named or "").strip(" .,?!")
    if not raw or len(raw) < 2:
        return False
    low = raw.lower()
    if low in _PROPERTY_NAME_STOPWORDS or low in _DETAIL_SKIP_NAMES:
        return False
    if re.fullmatch(r"prop-[a-z0-9\-]+", low) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", low):
        return True
    # Whole phrase of stop words only ("there anything")
    tokens = [t for t in re.split(r"[^\wąćęłńóśźżก-๙]+", low, flags=re.I) if t]
    if not tokens:
        return False
    if all(
        t in _PROPERTY_NAME_STOPWORDS
        or t in _DETAIL_SKIP_NAMES
        or t in _FILTER_TYPE_TOKENS
        or t in _AMENITY_NAME_TOKENS
        or t in _FILTER_WORD_EXTRAS
        or t.isdigit()
        for t in tokens
    ):
        return False
    try:
        from app.services.property_name_index import match_property_name
        mr = match_property_name(raw)
        if mr.confident is not None:
            return True
    except Exception:
        pass
    # At least one distinctive non-filter token (proper-name-ish)
    return any(
        t not in _PROPERTY_NAME_STOPWORDS
        and t not in _DETAIL_SKIP_NAMES
        and t not in _FILTER_TYPE_TOKENS
        and t not in _AMENITY_NAME_TOKENS
        and t not in _FILTER_WORD_EXTRAS
        and not t.isdigit()
        and len(t) >= 2
        for t in tokens
    )

_SHOW_SEARCH_VERBS = re.compile(
    r"\b(?:show\s+me|find|search|looking\s+for|list|pokaż|pokaz|zobacz|wyświetl|wyswietl|"
    r"szukam|szukamy|chcę|chce|daj\s+mi|znajdź|znajdz|ค้นหา|แสดง|ขอดู|หา)\b",
    re.IGNORECASE,
)

# Fact / amenity questions about one property (must beat amenity→search routing)
_FACT_QUESTION_RE = re.compile(
    r"(?:^|\b)(?:does|do|has|have|is|are|what(?:'s|\s+is)|how\s+much|price\s+of|cena|czy|ile|มี|ราคา)",
    re.IGNORECASE,
)
_FACT_TOPIC_RE = re.compile(
    r"(?:pool|spa|gym|parking|brochure|pdf|swimming|beach|price|cost|bedroom|bedrooms|"
    r"area|size|sqm|location|status|amenit|available|availability|basen|sypialn|"
    r"cena|สระ|ห้องนอน|ราคา|ทำเล|ที่จอด|ownership|tenure)",
    re.IGNORECASE,
)
_NAMED_FACT_CAPTURE_RES = [
    # "What is the price of X?" / "price of X" — before generic how-much (which would swallow "does")
    re.compile(
        r"(?:what(?:'s|\s+is|\s+are)\s+)?(?:the\s+)?(?:prices?|cost)\s+(?:of|for)\s+(?:the\s+)?(.+?)\s*[?.!]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"how\s+much\s+(?:does|is|do|are)\s+(?:the\s+|a\s+|an\s+)?(.+?)(?:\s+cost)?\s*[?.!]*$",
        re.IGNORECASE,
    ),
    # "does Pirak Villa have a pool?" — subject must not be you/it/there (handled separately)
    re.compile(
        r"(?:does|do|has|have|is|are)\s+(?!you\b|it\b|there\b|this\b|that\b|they\b|we\b)"
        r"(.+?)\s+(?:have|has|include|with)\s+(?:a\s+|an\s+)?"
        r"(?:spa|gym|parking|pool|brochure|beach|bedroom|price|status|location|pdf)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:does|do|has|have|is|are|what(?:'s|\s+is)|price\s+of|cena|czy)\s+"
        r"(?!you\b|it\b|there\b|this\b|that\b)"
        r"(.+?)\s+"
        r"(?:have|has|include|with|cost|price|pool|beach|bedroom|spa|gym|parking|brochure|อยู่|มี)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:does|do)\s+(?!you\b|it\b|there\b)(.+?)\s+have\s+",
        re.IGNORECASE,
    ),
]


def _is_amenity_only_name_candidate(named: str) -> bool:
    """True when every token is amenity/filter vocabulary — never a property name."""
    named_l = (named or "").strip().lower()
    if not named_l:
        return True
    if named_l in _AMENITY_NAME_TOKENS or named_l in _DETAIL_SKIP_NAMES:
        return True
    tokens = [t for t in re.split(r"[^\w]+", named_l) if t and len(t) > 1]
    if not tokens:
        return True
    return all(
        t in _AMENITY_NAME_TOKENS
        or t in _FILTER_TYPE_TOKENS
        or t in _DETAIL_SKIP_NAMES
        or t in {"w", "in", "at", "under", "with", "a", "an", "the", "also", "too", "and", "or"}
        or t.isdigit()
        for t in tokens
    )


def extract_named_property_fact_candidate(message: str) -> Optional[str]:
    """Pull a property-name span from a fact question like 'is Above Element Villas have spa?'."""
    text = (message or "").strip()
    if not text:
        return None
    # Plural filter questions ("Which have a private pool?") are not named-property facts
    if re.search(
        r"\b(?:which|what)\s+(?:ones?\s+)?(?:have|has|with)\b|\bwhich\s+have\b",
        text,
        re.IGNORECASE,
    ):
        return None
    for cre in _NAMED_FACT_CAPTURE_RES:
        m = cre.search(text)
        if not m:
            continue
        named = m.group(1).strip(" .,?!")
        named = re.sub(r"^(?:the|a|an|does|do|is|are)\s+", "", named, flags=re.I).strip()
        if named and len(named) >= 3 and named.lower() not in _DETAIL_SKIP_NAMES:
            named_l = named.lower()
            # Pronoun / anaphoric follow-ups ("does it also have a pool?") use active context
            if re.fullmatch(
                r"(?:it|this|that|these|those|one|ones?)(?:\s+(?:also|too|still|even))?",
                named_l,
            ):
                continue
            if _is_amenity_only_name_candidate(named):
                continue
            if not is_valid_property_name_candidate(named):
                continue
            # Category / place searches ("villa in Phuket", "villas in Phuket") are not names
            if re.search(r"\b(?:in|at|near|around|w)\b", named_l) and not looks_like_project_name(named):
                continue
            # Skip pure filter vocabulary ("villas in Phuket")
            tokens = [t for t in re.split(r"[^\w]+", named_l) if t and len(t) > 1]
            if tokens and all(
                t in _FILTER_TYPE_TOKENS
                or t in _DETAIL_SKIP_NAMES
                or t in {"w", "in", "at", "under", "with", "a", "an", "the", "also", "too"}
                or t.isdigit()
                for t in tokens
            ):
                continue
            return named
    return None


def try_named_property_fact_intent(
    message: str,
    *,
    active_property_id: Optional[str] = None,
    reference_map: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[IntentType, Dict[str, Any]]]:
    """
    Before search routing: fact question about a named property → PROPERTY_DETAILS
    only when the candidate matches the name index or the active property.
    Amenity words (private, pool, gym, …) are never treated as names.
    """
    text = (message or "").strip()
    if not text:
        return None
    # Genuine catalogue searches stay searches
    if _SHOW_SEARCH_VERBS.search(text) and message_looks_like_filter_search(text):
        return None
    if not _FACT_QUESTION_RE.search(text):
        return None
    if not _FACT_TOPIC_RE.search(text):
        return None

    candidate = extract_named_property_fact_candidate(text)
    if not candidate:
        return None
    if _is_amenity_only_name_candidate(candidate) or not is_valid_property_name_candidate(candidate):
        return None

    focus = text
    # A listing already shown in this session wins over a fresh name-index lookup.
    for _ref, _rid in (reference_map or {}).items():
        if str(_ref).strip().lower() == candidate.lower():
            return IntentType.PROPERTY_DETAILS, {
                "property_id": str(_rid),
                "property_name": candidate,
                "focus_question": focus,
                "named_fact": True,
                "from_property_context": True,
                "ignore_stored_filters": True,
            }
    try:
        from app.services.property_name_index import match_property_name
        mr = match_property_name(candidate)
        if mr.confident is not None:
            return IntentType.PROPERTY_DETAILS, {
                "property_id": mr.confident.doc_id,
                "property_name": mr.confident.display_name,
                "focus_question": focus,
                "named_fact": True,
                "ignore_stored_filters": True,
            }
        # Suggestions only — not confident enough; do not invent a name lookup
    except Exception as e:
        logger.warning("named property fact name-index lookup failed: %s", e)

    # Active property only when the candidate clearly refers to it (pronouns already skipped)
    if active_property_id and candidate.lower() in {
        str(active_property_id).lower(),
        str(active_property_id).lower().replace("-", " "),
    }:
        return IntentType.PROPERTY_DETAILS, {
            "property_id": active_property_id,
            "focus_question": focus,
            "named_fact": True,
            "from_property_context": True,
            "ignore_stored_filters": True,
        }

    # Proper-looking unknown project name → details path for honest not-found
    # (never amenities / stop words; never invent a search from stored filters)
    if (
        candidate.split()[0].lower() not in {"the", "a", "an", "does", "do", "is", "are", "have", "has"}
        and is_valid_property_name_candidate(candidate)
        and looks_like_project_name(candidate)
        and not re.search(r"\b(?:in|at|near|around|w)\b", candidate, flags=re.I)
    ):
        return IntentType.PROPERTY_DETAILS, {
            "property_name": candidate,
            "property_id": candidate,
            "focus_question": focus,
            "named_fact": True,
            "ignore_stored_filters": True,
        }

    # Weak / amenity-like fragment → let refine/search handle it
    return None


# Everyday words that can appear in a search sentence without being part of a project name.
_SEARCH_SENTENCE_WORDS = frozenset("""
a an the in at on of for to from with without and or near around by under below over above up max min about
me my our we you your i is are do does have has any some all what which where who how many much more most
tell show find search looking look want need like see give get list take
ok okay maybe perhaps please just now then also still
cheap cheaper cheapest best top luxury premium nice good new modern affordable beachfront sea ocean beach view views
pool private shared sea-view gym parking garden furnished ready budget price prices cost costs
bed beds bedroom bedrooms bath bathroom bathrooms sqm million mln thb baht usd eur pln euro dollars
villa villas condo condos condominium condominiums apartment apartments penthouse house houses property properties
listing listings project projects development developments resort residence residences
thailand thai island city area region
w we na do od z za po przy dla o i a u ile kosztuje cena ceny koszt mam macie czy jakie jaki jaka jakie
pokaż pokaz zobacz szukam szukamy chcę chce znajdź znajdz opowiedz powiedz mi o
willa wille willi willę willy apartament apartamenty mieszkanie mieszkania nieruchomość nieruchomości nieruchomosci
oferta oferty ofert basen basenem sypialnie sypialni sypialnia widok morze morza plaża plaza tanie tanią tani najtańsze
mln milionów milionow tys
""".split())


def looks_like_project_name(message: str) -> bool:
    """
    True for a short message that contains words which are NOT search vocabulary, e.g.
    "Azure Sky Residences Phuket" or "Paragon Villas Bophut". Such text is a project name that
    merely contains a place word, so it must be looked up by name, not run as a location search.
    A message with an explicit search verb or a number (budget / bedrooms) is always a search.
    """
    text = (message or "").strip()
    if not text or len(text) > 90 or _SHOW_SEARCH_VERBS.search(text):
        return False
    if re.search(r"\d", text) and re.search(r"(?:mln|million|m\b|thb|bed|sypial|budget|under|do\s+\d)", text.lower()):
        return False
    tokens = re.findall(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ][a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ'\-]{2,}", text)
    if not tokens:
        return False
    loc_words = {w for kw, _ in LOCATION_ALIASES for w in kw.lower().replace("-", " ").split()}
    alias_keys = [kw for kw, _ in LOCATION_ALIASES if re.search(r"[a-z]", kw)]
    unexplained = 0
    for tok in tokens:
        t = tok.lower().strip("'-")
        if t in _SEARCH_SENTENCE_WORDS or t in loc_words:
            continue
        if get_close_matches(t, alias_keys, n=1, cutoff=0.8):
            continue  # typo of a place (puket)
        unexplained += 1
    return unexplained >= 1


def message_looks_like_filter_search(message: str) -> bool:
    """
    True when the message is a location/type/budget/bedroom search (EN or PL),
    not a bare property name like "Anava" or "Satoshi Residence".
    Used to stop Polish "Pokaż wille w Phuket..." from being treated as a name lookup.
    """
    text = (message or "").strip()
    if not text:
        return False
    from app.services.conversation_controls import has_property_search_signal
    filters = extract_search_filters_deterministic(text)
    # Non-Thailand places route as search only when the user is asking about property.
    if filters.get("foreign_location") and has_property_search_signal(text):
        return True
    if looks_like_project_name(text):
        return False
    has_loc = bool(filters.get("location") or filters.get("is_broad_thailand"))
    has_type = bool(filters.get("property_type") or filters.get("is_multiple_types"))
    has_budget = filters.get("max_price") is not None or filters.get("min_price") is not None
    has_beds = (
        filters.get("bedrooms") is not None
        or filters.get("min_bedrooms") is not None
        or filters.get("max_bedrooms") is not None
    )
    has_amenity = bool(
        filters.get("required_amenities")
        or filters.get("has_pool")
        or filters.get("beach_access")
        or filters.get("sea_view")
    )
    has_show = bool(_SHOW_SEARCH_VERBS.search(text))

    # Strong signals: location + (type|budget|beds|amenity)
    if has_loc and (has_type or has_budget or has_beds or has_amenity):
        return True
    # Type + budget/beds without needing location
    if has_type and (has_budget or has_beds):
        return True
    # Bedroom-only asks ("4 or more bedrooms", "20 or more bedrooms")
    if has_beds:
        return True
    # Budget-only asks ("under 5 million", "budget 20M to 30M")
    if has_budget:
        return True
    # "Pokaż/Show me" + any structured filter
    if has_show and (has_loc or has_type or has_budget or has_beds or has_amenity):
        return True
    # Captured phrase is only filter vocabulary (e.g. "wille w Phuket")
    tokens = [t for t in re.split(r"[^\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+", text.lower()) if t]
    content = [t for t in tokens if t not in {
        "w", "we", "na", "do", "od", "z", "za", "po", "przy", "dla", "o", "i", "a", "the", "a", "an",
        "in", "at", "for", "under", "below", "up", "to", "me", "my", "some", "any",
        "mln", "m", "million", "milionów", "milionow", "miliony", "thb", "baht", "eur", "usd", "pln",
        "pokaż", "pokaz", "zobacz", "show", "find", "search",
    } and not t.isdigit()]
    if content and all(t in _FILTER_TYPE_TOKENS or t in {
        "phuket", "samui", "bangkok", "pattaya", "phangan", "thailand", "tajlandia",
        "koh", "layan", "karon", "maenam", "ภูเก็ต", "สมุย",
    } for t in content):
        if has_loc or has_type:
            return True
    return False


_PROPERTY_ID_RE = re.compile(r"\bprop-[a-z0-9\-]+\b|\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{18,24}\b", re.IGNORECASE)

_CARD_ACTION_RE = re.compile(
    r"\b(?:brochure|pdf|e-?mail|e-?mailed|tour|viewing|inquir\w*|enquir\w*|callback|call\s+me|contact\s+me|schedule)\b"
    r"|broszur|umów|umow|spotkani|นัดชม|โบรชัวร์",
    re.IGNORECASE,
)


def _is_card_action_message(msg_clean: str) -> bool:
    """PDF / email / tour / inquiry requests (from the card buttons or typed) are routed by their own rules."""
    if re.search(r"[\w\.-]+@[\w\.-]+\.\w+", msg_clean):
        return True
    if any(re.search(pat, msg_clean.lower()) for pat in PDF_EXPORT_PATTERNS):
        return True
    return bool(_CARD_ACTION_RE.search(msg_clean))


def _mention_intent(
    msg_clean: str, reference_map: Optional[Dict[str, Any]] = None
) -> Optional[Tuple[IntentType, Dict[str, Any]]]:
    """
    A listing name anywhere in the message (typos allowed) decides the intent: the visitor is asking about THAT
    listing. A confident name goes straight to its details; an uncertain one becomes a "Did you mean ...?".
    Real filter searches ("3 bedroom villas under 10M", "villas near Anava") are left alone.
    """
    low = msg_clean.lower()
    if re.search(r"\b(?:compare|comparison|versus|vs)\b|porówn|porown|เปรียบเทียบ", low):
        return None
    try:
        from app.services.property_name_index import get_property_name_index
        mention = get_property_name_index().find_mention(msg_clean)
    except Exception:
        return None
    if not (mention.confident or mention.candidates):
        return None
    f = extract_search_filters_deterministic(msg_clean)
    # Budget / bedroom counts mean a real search ("3 bedroom villas near Anava under 10M"). Types, places and
    # amenities are normal around a name ("does Raintree Villa Phuket have a pool?").
    constrained = (f.get("max_price") is not None or f.get("min_price") is not None
                   or f.get("bedrooms") is not None or bool(f.get("is_multiple_types")))
    if constrained and not (mention.confident and mention.n_core >= 2):
        return None
    if mention.confident:
        entry = mention.confident
        name = entry.display_name
        # An id the session already holds for this listing is more exact than the display name.
        for _ref, _rid in (reference_map or {}).items():
            if str(_ref).strip().lower() == name.lower():
                return IntentType.PROPERTY_DETAILS, {
                    "property_name": name, "property_id": str(_rid), "focus_question": msg_clean,
                    "from_property_context": True,
                }
        pid = entry.doc_id or name
        # Bare typed name alone ("Anava") — leave for later name-index / agent resolution.
        # In-sentence mentions ("tell me more about Anava") still become PROPERTY_DETAILS.
        span = (getattr(mention, "span", None) or name or "").strip()
        msg_compact = re.sub(r"\s+", " ", msg_clean).strip(" .,?!")
        if msg_compact.lower() in {span.lower(), name.lower(), (pid or "").lower()}:
            return None
        return IntentType.PROPERTY_DETAILS, {
            "property_name": name,
            "property_id": pid,
            "focus_question": msg_clean,
        }
    msg_digits = set(re.findall(r"\b\d+\b", low))
    digit_hit = bool(msg_digits) and any(
        msg_digits & set(re.findall(r"\b\d+\b", a)) for c in mention.candidates[:3]
        for a in (getattr(c, "aliases_norm", None) or [])
    )
    if len(mention.span.replace(" ", "")) >= 6 and (mention.typo or mention.n_core >= 2 or digit_hit):
        # Not sure which listing: hand the visitor's own words to the lookup, which answers "Did you mean ...?"
        return IntentType.PROPERTY_DETAILS, {
            "property_name": msg_clean, "property_id": msg_clean, "focus_question": msg_clean,
            "name_uncertain": True,
        }
    return None


def _name_lookup_after_search_verb(msg_clean: str) -> Optional[Tuple[IntentType, Dict[str, Any]]]:
    """
    "Show me satoshi paradise" / "find Anava Samui" / "Show me aura condominium": a search verb followed by a
    project name is a name lookup. Run as a search it returns arbitrary listings (or just the same type/place).
    Returns a PROPERTY_DETAILS intent by name, or None when the text is a real filter search.
    """
    if not _SHOW_SEARCH_VERBS.search(msg_clean):
        return None
    rem = re.sub(r"\s+", " ", _SHOW_SEARCH_VERBS.sub(" ", msg_clean)).strip(" .,?!")
    rem = re.sub(r"^(?:me|us|the|a|an|please|for|ok|okay)\s+", "", rem, flags=re.I).strip()
    # "show me more about X" → extract X (DETAILS phrasing left after stripping the search verb)
    _more = re.match(r"^(?:more\s+about|details?\s+(?:about|for|on)|info\s+(?:about|on))\s+(.+)$", rem, flags=re.I)
    if _more:
        rem = _more.group(1).strip(" .,?!")
    if len(rem) < 3 or not looks_like_project_name(rem):
        return None  # only search vocabulary / places / numbers: a genuine filter search
    if re.search(r"\b(?:in|at|near|around|w|we|na|z|ze|do|dla|nad|ใน)\b", rem, flags=re.I):
        return None  # "a castle in Phuket", "czegoś z widokiem na morze w Phuket": a descriptive search
    try:
        from app.services.property_name_index import match_property_name
        mr = match_property_name(rem)
        known = bool(mr.confident or mr.candidates)
        # An unknown Capitalised name ("Show me Atlantis Bay Villas") is also a name lookup, so the
        # visitor hears "not found" instead of being shown arbitrary villas.
        if known or re.search(r"[A-Z]", rem):
            pid = mr.confident.doc_id if mr.confident is not None else rem
            name = mr.confident.display_name if mr.confident is not None else rem
            return IntentType.PROPERTY_DETAILS, {
                "property_name": name,
                "property_id": pid,
                "focus_question": msg_clean,
            }
    except Exception:
        pass
    return None


def _looks_like_phone(text: str) -> bool:
    t = text.strip()
    digits = re.sub(r"\D", "", t)
    if not 7 <= len(digits) <= 15:
        return False
    if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}[:.]\d{2})?", t):
        return False  # an ISO date (+ time)
    if re.fullmatch(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}", t):
        return False  # a dd-mm-yyyy date
    return True


def _lead_target_ref(msg_clean: str) -> Optional[str]:
    """The listing a viewing / email request is about: a listing id in the message, else a confidently named listing."""
    m = _PROPERTY_ID_RE.search(msg_clean)
    if m:
        return m.group(0)
    try:
        from app.services.property_name_index import get_property_name_index
        mention = get_property_name_index().find_mention(msg_clean)
        if mention.confident:
            return mention.confident.display_name
    except Exception:
        pass
    return None


_PL_ORDINAL_ONLY_RE = re.compile(
    r"^(pierwsz\w*|drug\w*|trzec\w*|czwart\w*|pi[ąa]t\w*)"
    r"(?:\s+(?:ofert\w*|nieruchomo\w*|willa|will\w*|apartament\w*|projekt\w*|opcj\w*))?$",
    re.IGNORECASE,
)


def _pl_ordinal_index(target_raw: str) -> Optional[int]:
    """'pierwszy', 'drugą', 'trzecim', 'pierwszej oferty' -> 0/1/2 (only when the WHOLE target is an ordinal)."""
    m = _PL_ORDINAL_ONLY_RE.match((target_raw or "").strip(" .,?!"))
    if not m:
        return None
    stem = m.group(1).lower()
    for prefix, idx in (("pierwsz", 0), ("drug", 1), ("trzec", 2), ("czwart", 3), ("pi", 4)):
        if stem.startswith(prefix):
            return idx
    return None


def _is_brochure_search_phrase(text: str) -> bool:
    """True when the captured 'name' is a search description, not a project title."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if re.search(
        r"\b(?:listings?|properties|under|over|below|above|budget|cheaper|anything|everything)\b",
        t,
    ):
        return True
    if t.startswith("all ") and len(t.split()) >= 3:
        return True
    return False


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

    from app.services.conversation_controls import (
        has_property_search_signal,
        is_advisor_request,
        is_sort_only_request,
        is_weather_question,
        normalize_command_text,
    )
    msg_clean = normalize_command_text(msg_clean)
    msg_low = msg_clean.lower()

    # Sort requests are not property names ("most expensive ones").
    if is_sort_only_request(msg_clean) or is_weather_question(msg_clean):
        pass
    # A listing named anywhere in the message (typos allowed) decides the intent before any pattern guessing --
    # except card actions (PDF / email / tour / inquiry), which carry the name only to say WHICH listing.
    elif len(msg_clean) >= 4 and not _is_card_action_message(msg_clean):
        _pc0 = state.get("property_context") if isinstance(state.get("property_context"), dict) else {}
        _by_mention = _mention_intent(msg_clean, reference_map=_pc0.get("reference_map"))
        if _by_mention:
            return _by_mention
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
    if phone_match and not _looks_like_phone(phone_match.group(0)):
        phone_match = None  # "2026-11-02", "10:00", a price: not a phone number
    if is_weather_question(msg_clean):
        return None, {"weather": True}

    if is_advisor_request(msg_clean) and not (email_match or phone_match):
        return IntentType.LEAD_CAPTURE, {
            "email": None,
            "phone": None,
            "name": None,
            "advisor_request": True,
            "is_viewing": False,
            "location": None,
        }

    if email_match or phone_match or (has_viewing_intent and any(k in msg_low for k in ["name", "email", "phone", "contact", "telefon", "mail"])):
        name_match = re.search(r"(?:my name is|i am|nazywam się|imię:|name:)\s+([A-Z][a-zA-Z\'-]+(?:\s+[A-Z][a-zA-Z\'-]+)+)", message, re.I)
        search_f = extract_search_filters_deterministic(message)
        return IntentType.LEAD_CAPTURE, {
            "email": email_match.group(0) if email_match else None,
            "phone": phone_match.group(0).strip() if phone_match else None,
            "name": name_match.group(1).strip() if name_match else None,
            "advisor_request": bool(is_advisor_request(msg_clean)),
            "location": search_f.get("location"),
            "property_type": search_f.get("property_type", "Villa"),
            "budget": search_f.get("max_price"),
            "is_viewing": has_viewing_intent,
            "wants_specs": bool(re.search(r"\b(?:e-?mail|send)\b.*\b(?:brochure|specs?|specifications|floor\s*plans?|details)\b", msg_low)),
            "target_ref": _lead_target_ref(msg_clean),
        }

    # 0.2 Inventory asks: "Do you have …?", "Is there …?", "Have you got …?"
    # Never treat "you" / "there" / "anything" as a property name.
    _inv = re.match(
        r"^(?:do\s+you\s+have|have\s+you\s+got|is\s+there|are\s+there|"
        r"czy\s+(?:masz|macie|jest)|มี(?:ไหม)?)\s+"
        r"(?:a\s+|an\s+|any\s+|some\s+)?(.+?)\s*[?.!]*$",
        msg_clean,
        flags=re.IGNORECASE,
    )
    if _inv:
        rem = _inv.group(1).strip(" .,?!")
        rem = re.sub(r"^(?:a|an|any|some|the)\s+", "", rem, flags=re.I).strip()
        extracted_inv = extract_search_filters_deterministic(message)
        _has_filters = any(
            extracted_inv.get(k) is not None
            for k in (
                "bedrooms", "min_bedrooms", "max_bedrooms", "location", "property_type",
                "max_price", "min_price", "has_pool", "required_amenities", "beach_access",
                "sea_view", "foreign_location",
            )
        )
        # Strip trailing filter phrases so "Pirak Villa under 30M" still resolves the name
        rem_name = re.split(
            r"\b(?:with|under|below|over|above|between|or\s+more|or\s+fewer|"
            r"at\s+least|at\s+most|up\s+to|in|w|do|od)\b",
            rem,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" .,?!")
        if rem_name and is_valid_property_name_candidate(rem_name):
            try:
                from app.services.property_name_index import match_property_name
                mr = match_property_name(rem_name)
                if mr.confident is not None:
                    return IntentType.PROPERTY_DETAILS, {
                        "property_id": mr.confident.doc_id,
                        "property_name": mr.confident.display_name,
                        "focus_question": msg_clean,
                        "ignore_stored_filters": True,
                    }
            except Exception:
                pass
            if looks_like_project_name(rem_name):
                return IntentType.PROPERTY_DETAILS, {
                    "property_name": rem_name,
                    "property_id": rem_name,
                    "focus_question": msg_clean,
                    "ignore_stored_filters": True,
                }
        if _has_filters or message_looks_like_filter_search(msg_clean) or not is_valid_property_name_candidate(rem):
            if _has_filters or message_looks_like_filter_search(msg_clean):
                return IntentType.PROPERTY_SEARCH, extracted_inv
            # "Do you have anything?" with no filters → soft search / clarify via search path
            return IntentType.PROPERTY_SEARCH, extracted_inv or {}

    # 1. Show All Properties (Reset all filters)
    for pat in SHOW_ALL_PATTERNS:
        if re.search(pat, msg_low):
            return IntentType.PROPERTY_SEARCH, {"show_all": True}

    # 1.1 Show More / Pagination / "any other" alternatives
    _shown = bool(state.get("last_result_ids") or state.get("seen_property_ids"))
    if message_requests_show_more(message, listings_were_shown=_shown):
            curr_offset = state.get("pagination_offset", 0)
            # NEW filter values in the same message override stored ones
            # (e.g. "show me other under 20 million" must not keep a 40M budget).
            extracted = extract_search_filters_deterministic(message)
            override_keys = (
                "location",
                "property_type",
                "bedrooms",
                "min_bedrooms",
                "max_bedrooms",
                "max_price",
                "min_price",
                "has_pool",
                "required_amenities",
                "beach_access",
            )
            filter_overrides = {
                k: extracted[k]
                for k in override_keys
                if k in extracted and extracted[k] is not None
            }
            filters_changed = bool(filter_overrides)
            return IntentType.SHOW_MORE, {
                # New filters → fresh result list (offset 0); else paginate
                "offset": 0 if filters_changed else (curr_offset + state.get("limit", 4)),
                "limit": state.get("limit", 4),
                "reject_active": message_rejects_active_property(msg_low),
                "reuse_active_filters": True,
                "filter_overrides": filter_overrides,
                "filters_changed": filters_changed,
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
                        # "brochure for all Phuket listings under 10M" is a search, not a name
                        if _is_brochure_search_phrase(prop_name):
                            prop_id = None
                            prop_name = None
                        elif prop_name.lower() in {"it", "this", "that", "this one", "that one", "the property"}:
                            prop_name = None
                            prop_id = (
                                (pctx.get("active_property_id") if pctx else None)
                                or (selected_ids[0] if selected_ids else None)
                                or (last_result_ids[0] if len(last_result_ids) == 1 else None)
                            )
                        else:
                            prop_id = prop_name  # resolve via Firestore/SQL by name
                    elif pctx.get("active_property_id"):
                        prop_id = pctx.get("active_property_id")
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

    _filter_search_message = message_looks_like_filter_search(msg_clean)
    _ORDINAL_WORD_TO_IDX = {
        "FIRST": 0, "1ST": 0, "1": 0, "อันแรก": 0,
        "SECOND": 1, "2ND": 1, "2": 1, "อันที่สอง": 1,
        "THIRD": 2, "3RD": 2, "3": 2, "อันที่สาม": 2,
        "FOURTH": 3, "4TH": 3, "4": 3,
        "FIFTH": 4, "5TH": 4, "5": 4,
    }
    for pat in DETAILS_PATTERNS:
        m = re.search(pat, msg_low, flags=re.IGNORECASE)
        if m:
            target_raw = (m.group(1) or "").strip()
            target = target_raw.upper()
            target_id = None
            if target.startswith("PROP-"):
                target_id = target
            elif target in _ORDINAL_WORD_TO_IDX or _pl_ordinal_index(target_raw) is not None:
                idx = _ORDINAL_WORD_TO_IDX[target] if target in _ORDINAL_WORD_TO_IDX else _pl_ordinal_index(target_raw)
                if len(last_result_ids) > idx:
                    target_id = last_result_ids[idx]
                else:
                    # Ordinal intent even if prefs empty — public_agent hydrates from history cards
                    return IntentType.PROPERTY_DETAILS, {
                        "property_id": None,
                        "ordinal_index": idx,
                        "focus_question": msg_clean,
                        "from_property_context": True,
                    }
            elif target_raw and target_raw.lower() not in _DETAIL_SKIP_NAMES and len(target_raw) >= 3:
                # Polish/EN filter searches ("Pokaż wille w Phuket…") must not become name lookups
                if _filter_search_message:
                    break
                # Named project / slug follow-up — keep explicit token; live lookup overrides active context
                name_match = re.search(pat, msg_clean, flags=re.IGNORECASE)
                named = (name_match.group(1).strip() if name_match else target_raw).strip(" .,?!")
                named_l = named.lower()
                if _is_amenity_only_name_candidate(named):
                    break
                # Skip if captured "name" is only type/location filter words
                named_tokens = [t for t in re.split(r"[^\wąćęłńóśźż]+", named_l) if t and len(t) > 1]
                if named_tokens and all(
                    t in _FILTER_TYPE_TOKENS
                    or t in _DETAIL_SKIP_NAMES
                    or t in _AMENITY_NAME_TOKENS
                    or t in {"w", "do", "od", "na", "in", "under", "mln", "thb", "baht", "million"}
                    or t.isdigit()
                    for t in named_tokens
                ):
                    break
                mapped_id = None
                ref_map = (pctx or {}).get("reference_map") or {}
                slug_like = bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", named_l))
                # Prefer exact slug/id hit in session ids before fuzzy name map
                for rid in (last_result_ids or []):
                    if named_l == str(rid).lower() or named_l.replace(" ", "-") == str(rid).lower():
                        mapped_id = str(rid)
                        break
                if not mapped_id and not slug_like:
                    for ref, rid in sorted(ref_map.items(), key=lambda kv: len(str(kv[0])), reverse=True):
                        ref_l = str(ref).lower()
                        if len(ref_l) >= 3 and (named_l == ref_l or named_l in ref_l or ref_l in named_l):
                            mapped_id = str(rid)
                            break
                if not mapped_id and not slug_like:
                    for rid in (last_result_ids or []):
                        if named_l.replace(" ", "-") in str(rid).lower() or all(
                            t in str(rid).lower() for t in named_l.split() if len(t) >= 3
                        ):
                            mapped_id = str(rid)
                            break
                # Prefer name-index id; unknown-but-plausible project names still go to DETAILS (honest not-found)
                if not mapped_id and not slug_like:
                    try:
                        from app.services.property_name_index import match_property_name
                        mr = match_property_name(named)
                        if mr.confident is not None:
                            mapped_id = mr.confident.doc_id
                            named = mr.confident.display_name
                        elif not looks_like_project_name(named):
                            break
                    except Exception:
                        if not looks_like_project_name(named):
                            break
                return IntentType.PROPERTY_DETAILS, {
                    "property_name": named,
                    "property_id": mapped_id or named,
                    # Slug/exact name must not be overwritten by active property fallback
                    "from_property_context": not slug_like and not bool(mapped_id),
                    "explicit_property_token": named,
                }

            if target_id:
                return IntentType.PROPERTY_DETAILS, {
                    "property_id": target_id,
                    "from_property_context": True,
                }

    active_id = (selected_ids[0] if selected_ids else None) or (pctx.get("active_property_id") if pctx else None)

    # Named-property fact question (spa/pool/parking/…) — before amenity→search
    named_fact_intent = try_named_property_fact_intent(
        msg_clean, active_property_id=active_id, reference_map=(pctx or {}).get("reference_map")
    )
    if named_fact_intent is not None:
        return named_fact_intent

    # Amenity refine on an active search ("Which have a private pool?") beats single-property facts
    _plural_amenity_filter = bool(re.search(
        r"\b(?:which|what)\s+(?:ones?\s+)?(?:have|has|with)\b|"
        r"\bwhich\s+have\b|"
        r"\b(?:any|ones?)\s+with\b",
        msg_low,
    ))
    _has_active_search = bool(
        state.get("active_filters") or state.get("location") or state.get("last_result_ids")
    )

    # Anaphoric + active property context (supports 100+ follow-ups without renaming the property)
    if not _plural_amenity_filter and any(phrase in msg_low for phrase in [
        "that one", "give me that", "show that", "tell me about it", "อันนั้น", "ขออันนี้", "ดูอันนั้น",
        "this property", "that property", "this villa", "that villa", "its price", "its amenities",
        "its location", "does it", "is it ", "has it", "what about it", "does it also",
        "and does it", "and has it", "where is it", "where is this", "located",
        "have pool", "has pool", "have a pool", "has a parking", "have parking",
        "how many bedroom", "bedrooms in this", "bedrooms in it",
    ]) or (
        active_id
        and not _plural_amenity_filter
        and re.search(r"\b(pool|parking|price|bedroom|location|beach|amenit)", msg_low)
        and not (_has_active_search and re.search(r"\b(?:which|any|ones?)\b", msg_low))
    ):
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
        r"(?:pool|spa|gym|parking|brochure|pdf|swimming|beach|price|cost|bedroom|bedrooms|"
        r"area|size|sqm|location|status|amenit|available|availability|basen|sypialn|"
        r"cena|สระ|ห้องนอน|ราคา|ทำเล|ที่จอด)",
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
        # "What is the price of X?" / "How much does X cost?" / "price of X": take X as the name.
        # (The generic pattern below would capture "the" or "does x" for these phrasings.)
        named_fact = None
        _price_q = (
            re.search(r"(?:what(?:'s| is| are)\s+)?(?:the\s+)?(?:prices?|cost)\s+(?:of|for)\s+(?:the\s+)?(.+?)\s*[?.!]*$", msg_low)
            or re.search(r"how much\s+(?:does|is|do|are)\s+(?:the\s+|a\s+|an\s+)?(.+?)(?:\s+cost)?\s*[?.!]*$", msg_low)
        )
        if _price_q and looks_like_project_name(_price_q.group(1)):
            named_fact = _price_q
        if not named_fact:
            named_fact = re.search(
                r"(?:does|do|has|have|is|are|what(?:'s| is)|price of|cena|czy)\s+(.+?)\s+"
                r"(?:have|has|include|with|cost|price|pool|beach|bedroom|spa|gym|parking|brochure|อยู่|มี)",
                msg_low
            )
        if not named_fact:
            named_fact = re.search(
                r"(?:does|do|is)\s+(.+?)\s+have\s+",
                msg_low
            )
        if named_fact and not _plural_amenity_filter:
            named = named_fact.group(1).strip(" .,?!")
            named = re.sub(r"^(?:the|a|an|does|do|is|are)\s+", "", named).strip()
            if (
                named
                and len(named) >= 3
                and named.lower() not in _DETAIL_SKIP_NAMES
                and "property" not in named.lower()
                and named.split()[0].lower() not in {"the", "a", "an", "does", "do", "is", "are"}
                and not _is_amenity_only_name_candidate(named)
                and not (
                    re.search(r"\b(?:in|at|near|around|w)\b", named.lower())
                    and not looks_like_project_name(named)
                )
            ):
                # A listing already shown in this session wins over a fresh name-index lookup.
                for _ref, _rid in ((pctx or {}).get("reference_map") or {}).items():
                    if str(_ref).strip().lower() == named.lower():
                        return IntentType.PROPERTY_DETAILS, {
                            "property_id": str(_rid),
                            "property_name": named,
                            "focus_question": msg_clean,
                            "from_property_context": True,
                        }
                try:
                    from app.services.property_name_index import match_property_name
                    mr = match_property_name(named)
                    if mr.confident is not None:
                        return IntentType.PROPERTY_DETAILS, {
                            "property_id": mr.confident.doc_id,
                            "property_name": mr.confident.display_name,
                            "focus_question": msg_clean,
                            "named_fact": True,
                            "ignore_stored_filters": True,
                        }
                except Exception:
                    pass
                # Unknown-but-plausible project name → still PROPERTY_DETAILS (honest not-found)
                if looks_like_project_name(named):
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
    # A full price range is a search, not a one-sided "budget 20" refine.
    has_refine_clue = not _extract_price_range(msg_low) and (
        any(re.search(p, msg_low) for p in REFINE_PATTERNS) or (
            _plural_amenity_filter and bool(re.search(r"\b(?:pool|gym|parking|sea\s*view|basen)\b", msg_low))
        )
    )
    is_short = len(msg_low.split()) <= 12
    if has_refine_clue and is_short and (state.get("active_filters") or state.get("location") or state.get("property_type") or state.get("bedrooms") or state.get("last_result_ids")):
        refinements = {}
        # Cheaper delta
        if "cheaper" in msg_low or "tańsze" in msg_low or "ถูกกว่า" in msg_low:
            shown = [float(p) for p in (state.get("last_shown_prices") or []) if p]
            if shown:
                refinements["max_price"] = min(shown) - 1
                refinements["max_price_exclusive"] = True
            else:
                curr_max = state.get("max_price") or state.get("budget_max")
                if curr_max:
                    refinements["max_price"] = float(curr_max) * 0.85

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
            # A message that also names bedrooms or a type is a new search, not a location-only tweak.
            extracted_now = extract_search_filters_deterministic(message)
            extra_slots = ("bedrooms", "min_bedrooms", "max_bedrooms", "property_type", "min_price", "status")
            if any(extracted_now.get(slot) is not None and slot not in refinements for slot in extra_slots):
                pass
            else:
                return IntentType.REFINE_FILTER, refinements

    # 5. Named listing fact lookup BEFORE general legal/ROI topics
    # e.g. "Tell me the price, bedrooms and ownership of Fantasea Condo"
    _named_fact_keys = (
        "price", "bedroom", "ownership", "status", "area", "location", "pool",
        "cena", "sypialn", "własno", "wlasno", "ราคา", "ห้องนอน", "กรรมสิทธิ์", "สถานะ"
    )
    if any(k in msg_low for k in _named_fact_keys):
        named = None
        m_of = re.search(
            r"(?:of|for|about|regarding|o|dla|ของ|เกี่ยวกับ)\s+([A-Za-z0-9][\w\s\-'&.]{2,80}?)\s*[\?\!\.]*$",
            msg_clean,
            flags=re.IGNORECASE,
        )
        if m_of:
            named = m_of.group(1).strip(" .,?!")
            named = re.sub(r"^(?:the|a|an)\s+", "", named, flags=re.I).strip()
        if not named:
            # Title-case project name inside the message
            m_title = re.search(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){1,6})\b", msg_clean)
            if m_title:
                named = m_title.group(1).strip()
        if not named and pctx:
            ref_map = pctx.get("reference_map") or {}
            for ref in sorted(ref_map.keys(), key=lambda x: len(str(x)), reverse=True):
                ref_s = str(ref).strip()
                if len(ref_s) >= 4 and ref_s.lower() in msg_low and ref_s.lower() not in (
                    "first property", "second property", "third property", "fourth property", "fifth property"
                ):
                    named = ref_s
                    break
        # A phrase made only of search words ("villas in Phuket") is a search, not a project name
        # (unless the session already knows it as a listing).
        _ctx_names = {str(r).lower() for r in ((pctx or {}).get("reference_map") or {})}
        if named and named.lower() not in _ctx_names and not looks_like_project_name(named):
            named = None
        if (
            named
            and len(named) >= 3
            and named.lower() not in _DETAIL_SKIP_NAMES
            and not _is_amenity_only_name_candidate(named)
        ):
            # Prefer known session id when available
            mapped_id = None
            ref_map = (pctx or {}).get("reference_map") or {}
            named_l = named.lower()
            for ref, rid in sorted(ref_map.items(), key=lambda kv: len(str(kv[0])), reverse=True):
                ref_l = str(ref).lower()
                if len(ref_l) >= 3 and (named_l == ref_l or named_l in ref_l or ref_l in named_l):
                    mapped_id = str(rid)
                    break
            if not mapped_id:
                try:
                    from app.services.property_name_index import match_property_name
                    mr = match_property_name(named)
                    if mr.confident is not None:
                        mapped_id = mr.confident.doc_id
                        named = mr.confident.display_name
                    else:
                        named = None
                except Exception:
                    named = None
            if named:
                return IntentType.PROPERTY_DETAILS, {
                    "property_name": named,
                    "property_id": mapped_id or named,
                    "focus_question": msg_clean,
                    "from_property_context": True,
                }

    # 6. General Real Estate Informational Questions (only when no named listing)
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

    # 7. Direct search keywords and structured filter presence check
    search_keywords = [
        "show me", "find", "search", "looking for", "villas in", "condos in",
        "properties in", "apartments in", "pool villa", "luxury villa", "penthouse",
        "villa in", "condo in", "houses in", "apartments in", "buy a villa", "buy a condo",
        "cheapest", "lowest price", "most affordable", "najtańszy", "najtanszy",
        "anything in", "something in", "options in", "what about", "how about",
        "okay in", "ok in", "maybe in", "perhaps in",
        "coś w", "cos w", "cokolwiek w", "oferty w",
        # Polish show/find + type phrases (must beat DETAILS "pokaż <name>" capture)
        "pokaż wille", "pokaz wille", "pokaż willę", "pokaz willę", "pokaż willi",
        "pokaż apartament", "pokaz apartament", "pokaż mieszkani", "pokaz mieszkani",
        "wille w", "willę w", "willi w", "apartamenty w", "mieszkania w",
        "szukam will", "szukam apartament", "szukam mieszkani", "szukam nieruchom",
        "ค้นหา", "แสดง", "ขอดู", "หาวิลล่า", "หาคอนโด", "พูลวิลล่าใน", "คอนโดใน",
        "วิลล่าใน", "อสังหาริมทรัพย์ใน", "ถูกที่สุด", "ราคาถูกที่สุด", "มีอะไรใน"
    ]
    _by_name = _name_lookup_after_search_verb(msg_clean)
    if _by_name:
        return _by_name
    # Last-chance: amenity words must not force a search when this is a named fact Q
    named_fact_late = try_named_property_fact_intent(
        msg_clean, active_property_id=active_id, reference_map=(pctx or {}).get("reference_map")
    )
    if named_fact_late is not None:
        return named_fact_late

    if message_looks_like_filter_search(msg_clean):
        return IntentType.PROPERTY_SEARCH, extract_search_filters_deterministic(message)
    if (
        looks_like_project_name(msg_clean)
        and not is_sort_only_request(msg_clean)
        and not has_property_search_signal(msg_clean)
        and not re.search(
            r"\b(?:okay|ok|maybe|perhaps|what about|how about|anything|something|options)\s+in\b", msg_low
        )
    ):
        return None, {}  # a project name that contains a place word: resolve by name, not by location
    if _PROPERTY_ID_RE.search(msg_clean):
        return None, {}  # a message carrying a listing id ("I am interested in <id> ...") is about THAT listing, not a place
    extracted_test = extract_search_filters_deterministic(message)
    location_followup = bool(extracted_test.get("location")) and (
        re.search(r"\b(?:in|at|around|near|w|ใน)\b", msg_low)
        or len(msg_low.split()) <= 6
        or bool(extracted_test.get("location_fuzzy"))
        or bool(state.get("active_filters") or state.get("location") or state.get("last_result_ids"))
    )
    # Amenity alone is NOT search intent when it looks like a named-property fact
    amenity_alone = bool(extracted_test.get("required_amenities")) and not (
        extracted_test.get("location")
        or extracted_test.get("max_price")
        or extracted_test.get("bedrooms")
        or _SHOW_SEARCH_VERBS.search(msg_low)
    )
    has_search_intent = any(k in msg_low for k in search_keywords) or (
        ("location" in extracted_test) and (
            "property_type" in extracted_test
            or "bedrooms" in extracted_test
            or "min_bedrooms" in extracted_test
            or "max_bedrooms" in extracted_test
            or "max_price" in extracted_test
            or location_followup
        )
    ) or ("property_type" in extracted_test and any(k in msg_low for k in ["cheapest", "lowest", "affordable", "najtańszy", "najtanszy", "ถูกที่สุด", "ราคาต่ำสุด"])) or (
        bool(extracted_test.get("foreign_location")) and has_property_search_signal(msg_clean)
    ) or (
        bool(extracted_test.get("required_amenities")) and not amenity_alone
    ) or (
        extracted_test.get("bedrooms") is not None
        or extracted_test.get("min_bedrooms") is not None
        or extracted_test.get("max_bedrooms") is not None
    ) or (
        extracted_test.get("max_price") is not None
        or extracted_test.get("min_price") is not None
    )
    if has_search_intent:
        _has_filter = any(
            extracted_test.get(k) not in (None, "", [], False)
            for k in ("location", "property_type", "bedrooms", "min_bedrooms", "max_bedrooms",
                      "max_price", "min_price", "required_amenities",
                      "foreign_location", "is_broad_thailand", "is_multiple_types", "has_pool", "beach_access",
                      "sea_view", "show_all")
        )
        if not _has_filter and not re.search(
            r"cheapest|lowest|affordable|najta|ถูกที่สุด|anything|something|options|oferty|coś|cos w|cokolwiek", msg_low
        ):
            return None, {}  # a search with no filters would return arbitrary listings: let the assistant clarify
        return IntentType.PROPERTY_SEARCH, extracted_test

    if is_sort_only_request(msg_clean) or detect_price_sort(msg_clean):
        return IntentType.PROPERTY_SEARCH, extract_search_filters_deterministic(message)

    return None, {}


_PLACE_BLOB: Dict[str, Any] = {"at": 0.0, "text": None}
_NOT_A_PLACE = frozenset({
    "thailand", "thai", "asia", "the", "english", "polish", "any", "all", "my", "our", "this", "that", "it",
    "budget", "total", "general", "particular", "stock", "case", "time", "order", "fact", "addition", "short",
})


def _catalog_place_text() -> Optional[str]:
    """Lower-cased text of every place/name/address in the catalogue (15 min cache). None if unavailable."""
    import time as _t
    now = _t.time()
    if _PLACE_BLOB["text"] is not None and now - _PLACE_BLOB["at"] < 900:
        return _PLACE_BLOB["text"]
    try:
        from app.services.firestore_service import fetch_properties_from_firestore
        props = fetch_properties_from_firestore(limit=5000, active_only=True, target="user") or []
        if not props:
            return _PLACE_BLOB["text"]
        parts = []
        for p in props:
            for k in ("location", "address", "district", "province", "name", "city", "area"):
                v = p.get(k)
                if v:
                    parts.append(str(v).lower().replace("-", " "))
        _PLACE_BLOB.update({"at": now, "text": " | ".join(parts)})
    except Exception:
        return _PLACE_BLOB["text"]
    return _PLACE_BLOB["text"]


def _unknown_place_in_message(message: str) -> Optional[str]:
    """
    "...villa in Antarctica": a capitalised place after in/near/at that is neither a known alias nor
    anywhere in the catalogue. Fails open (None) when the catalogue is unavailable.
    """
    m = re.search(r"\b(?:in|near|at)\s+(?:the\s+)?([A-Z][A-Za-z'\-]{3,}(?:\s+[A-Z][A-Za-z'\-]{2,})?)", message or "")
    if not m:
        return None
    cand = m.group(1).strip()
    low = cand.lower()
    if low in _NOT_A_PLACE or any(w in _NOT_A_PLACE for w in low.split()[:1]):
        return None
    for kw, _ in LOCATION_ALIASES:
        if kw in low or low in kw:
            return None
    if get_close_matches(low, [kw for kw, _ in LOCATION_ALIASES if re.search(r"[a-z]", kw)], n=1, cutoff=0.8):
        return None
    blob = _catalog_place_text()
    if not blob:
        return None
    if low.replace("-", " ") in blob or low.split()[0] in blob:
        return None
    return cand


def _parse_million_amount(raw: str, unit: str = "") -> float:
    val = float(str(raw).replace(",", ""))
    unit_l = (unit or "").lower()
    if unit_l in ("m", "million", "mln", "milionów", "ล้าน") or (not unit_l and val < 500):
        val *= 1_000_000
    return val


def _extract_price_range(msg_low: str) -> Optional[Tuple[float, float]]:
    """Inclusive min and max from 'budget 20M to 30M' and the same shape in Polish."""
    patterns = (
        r"(?:budget|budżet|budzet)\s+(\d+(?:\.\d+)?)\s*m?\s*(?:to|-|–|and)\s*(\d+(?:\.\d+)?)\s*m",
        r"between\s+(\d+(?:\.\d+)?)\s*(?:and|to|-|–)\s*(\d+(?:\.\d+)?)\s*(?:million|m|mln)\b",
        r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*m\b",
        r"from\s+(\d+(?:\.\d+)?)\s*m?\s+to\s+(\d+(?:\.\d+)?)\s*m\b",
        r"(\d+(?:\.\d+)?)\s*million\s*[-–]\s*(\d+(?:\.\d+)?)\s*million",
        r"od\s+(\d+(?:\.\d+)?)\s+do\s+(\d+(?:\.\d+)?)\s*mln",
    )
    for pat in patterns:
        found = re.search(pat, msg_low)
        if not found:
            continue
        left = _parse_million_amount(found.group(1), "m")
        right = _parse_million_amount(found.group(2), "m")
        return (min(left, right), max(left, right))
    return None


def _extract_search_filters_deterministic_impl(message: str) -> Dict[str, Any]:
    """
    Extracts structured search criteria from natural language without calling an LLM.
    Supports English, Polish, and Thai expressions, including real-time currency conversion (EUR, USD, PLN -> THB).
    """
    from app.services.conversation_controls import normalize_command_text
    message = normalize_command_text(message)
    msg_low = message.lower().strip()
    filters: Dict[str, Any] = {}

    FX_RATES = {
        "pln": 8.5, "zł": 8.5, "zl": 8.5, "zlote": 8.5, "złotych": 8.5,
        "eur": 38.0, "euro": 38.0, "€": 38.0,
        "usd": 35.0, "dollars": 35.0, "$": 35.0,
        "thb": 1.0, "baht": 1.0, "บาท": 1.0
    }

    # Location (English / Polish / Thai) — exact then fuzzy (puket → Phuket)
    for kw, val in sorted(LOCATION_ALIASES, key=lambda kv: len(kv[0]), reverse=True):
        if kw in msg_low:
            filters["location"] = val
            break
    if not filters.get("location"):
        fuzzy_loc = fuzzy_resolve_location(message)
        if fuzzy_loc:
            filters["location"] = fuzzy_loc
            filters["location_fuzzy"] = True

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

    # A place we know nothing about ("villa in Antarctica"): do not silently search somewhere else.
    if not filters.get("location") and not filters.get("foreign_location"):
        unknown = _unknown_place_in_message(message)
        if unknown:
            filters["foreign_location"] = unknown

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
    has_villa = any(k in msg_low for k in [
        "villa", "villas", "willa", "wille", "willi", "willę", "willą", "willy",
        "วิลล่า", "พูลวิลล่า", "บ้าน",
    ])
    has_condo = any(k in msg_low for k in [
        "condo", "condos", "condominium", "condominiums", "apartment", "apartments",
        "apartament", "apartamenty", "apartamentów", "apartamentow", "kondominium",
        "mieszkanie", "mieszkania", "คอนโด", "คอนโดมิเนียม", "อพาร์ทเมนท์", "อพาร์ตเมนต์",
    ])
    if has_villa and has_condo:
        filters["property_type"] = None
        filters["is_multiple_types"] = True
    elif has_villa:
        filters["property_type"] = "Villa"
    elif has_condo:
        filters["property_type"] = "Condominium"

    # Amenity requests (gym, pool, spa, parking, …) → canonical keys for search
    try:
        from app.services.amenity_service import extract_requested_amenities
        req_am = extract_requested_amenities(message)
        if req_am:
            filters["required_amenities"] = req_am
            if "pool" in req_am or "pool_private" in req_am:
                filters["has_pool"] = True
            if "beach_access" in req_am:
                filters["beach_access"] = True
    except Exception:
        pass

    # Bedroom ranges / min / max BEFORE exact count
    # "4 or more", "4+", "at least 4", "more than 3" (→ min 4), "over 3" (→ min 4)
    # "3 or fewer", "up to 3", "at most 3"
    # "between 3 and 5", "3-5 bedrooms", "5-6 beds"
    # PL: "co najmniej 4 sypialnie", "4 lub więcej", "od 3 do 5"
    # TH: "4 ห้องนอนขึ้นไป", "อย่างน้อย 4 ห้องนอน"
    _bed_unit = r"(?:bedrooms?|beds?|br|sypialni[aeę]?|sypialn|pokoj(?:e|i)?|ห้องนอน|ห้อง)"
    _min_bed = (
        re.search(rf"(\d+)\s*\+\s*{_bed_unit}", msg_low)
        or re.search(rf"(\d+)\s+(?:or\s+more|bedrooms?\s+or\s+more|beds?\s+or\s+more)\b", msg_low)
        or re.search(rf"(?:at\s+least|minimum|min\.?)\s+(\d+)\s*{_bed_unit}", msg_low)
        or re.search(rf"(?:more\s+than|over)\s+(\d+)\s*{_bed_unit}", msg_low)
        or re.search(rf"co\s+najmniej\s+(\d+)\s*{_bed_unit}", msg_low)
        or re.search(rf"(\d+)\s+lub\s+wi[eę]cej\s*{_bed_unit}", msg_low)
        or re.search(rf"(\d+)\s*ห้องนอน\s*ขึ้นไป", msg_low)
        or re.search(rf"อย่างน้อย\s*(\d+)\s*{_bed_unit}", msg_low)
    )
    if _min_bed:
        n = int(_min_bed.group(1))
        # "more than 3" / "over 3" → minimum 4
        if re.search(r"(?:more\s+than|over)\s+\d+", _min_bed.group(0) or ""):
            n = n + 1
        filters["min_bedrooms"] = n

    _max_bed = (
        re.search(rf"(\d+)\s+or\s+fewer\b", msg_low)
        or re.search(rf"(?:up\s+to|at\s+most|maximum|max\.?|no\s+more\s+than)\s+(\d+)\s+{_bed_unit}\b", msg_low)
        or re.search(rf"(?:nie\s+wi[eę]cej\s+ni[zż]|maksymalnie)\s+(\d+)\s+{_bed_unit}\b", msg_low)
    )
    if _max_bed and "min_bedrooms" not in filters:
        filters["max_bedrooms"] = int(_max_bed.group(1))
    if filters.get("max_bedrooms") is not None:
        n_beds = int(filters["max_bedrooms"])
        price_same_number = re.search(
            rf"\b{n_beds}\s*(?:m|million|mln|k|thb|baht|usd)\b",
            msg_low,
        )
        bed_same_number = re.search(rf"\b{n_beds}\s+(?:bedrooms?|beds?|br|sypialn\w*)\b", msg_low)
        if price_same_number and not bed_same_number:
            filters.pop("max_bedrooms", None)

    _range_bed = (
        re.search(rf"between\s+(\d+)\s+and\s+(\d+)\s*{_bed_unit}", msg_low)
        or re.search(rf"od\s+(\d+)\s+do\s+(\d+)\s*{_bed_unit}", msg_low)
        or re.search(rf"(\d+)\s*[-–]\s*(\d+)\s*{_bed_unit}", msg_low)
    )
    if _range_bed:
        a, b = int(_range_bed.group(1)), int(_range_bed.group(2))
        filters["min_bedrooms"] = min(a, b)
        filters["max_bedrooms"] = max(a, b)
        filters.pop("bedrooms", None)

    # Exact bedrooms (allow hyphen: "50-bedroom", "3-bed") — skip if range already set
    if "min_bedrooms" not in filters and "max_bedrooms" not in filters:
        bed_match = re.search(
            r"(\d+)\s*[-–]?\s*(?:bedrooms?|beds?|br|sypialni[aeę]?|sypialn|pokoj(?:e|i)?|ห้องนอน|ห้อง)\b",
            msg_low,
        )
        if bed_match:
            filters["bedrooms"] = int(bed_match.group(1))

    # Bathrooms
    bath_match = re.search(r"(\d+)\s*(?:bath|bathroom|lazienk|łazienk|ห้องน้ำ)", msg_low)
    if bath_match:
        filters["bathrooms"] = int(bath_match.group(1))

    # Inclusive price range before a single cap ("budget 20M to 30M").
    price_range = _extract_price_range(msg_low)
    if price_range:
        filters["min_price"] = price_range[0]
        filters["max_price"] = price_range[1]
        filters["max_price_exclusive"] = False
        if not re.search(r"\b(bed|bedroom|sypial)", msg_low):
            filters.pop("bedrooms", None)
            filters.pop("min_bedrooms", None)
            filters.pop("max_bedrooms", None)

    # Max Price / Budget with Currency Conversion
    # A. Prefix currency (e.g. $500,000, €1,000,000)
    pfx_m = re.search(r"([$€£])\s*([0-9][0-9,.]*)\s*(k|m|million|mln)?", msg_low)
    if pfx_m and "min_price" not in filters:
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
    if "max_price" not in filters and "min_price" not in filters:
        sfx_m = re.search(r"([0-9][0-9,.]*)\s*(tys\.?|tysi[eę]cy|tysiecy|thousand|k|m|million|mln|milionów|ล้าน)?\s*(pln|eur|usd|thb|baht|บาท|zł|zl|dollars|euro|zlote|złotych)", msg_low)
        if sfx_m:
            val = float(sfx_m.group(1).replace(",", ""))
            unit = (sfx_m.group(2) or "").lower()
            curr = (sfx_m.group(3) or "").lower()
            if _is_thousand_unit(unit):
                val *= 1_000
            elif unit in ["m", "million", "mln", "milionów", "ล้าน"] or (val < 500 and curr in ["pln", "eur", "usd", "thb"]):
                val *= 1_000_000
            elif unit == "k":
                val *= 1_000
            rate = FX_RATES.get(curr, 1.0)
            filters["max_price"] = val * rate

    # C. Keyword-based budget without explicit currency (defaults to THB)
    if "max_price" not in filters and "min_price" not in filters:
        for p_match in re.finditer(r"(?:under|below|max|budget|up to|poniżej|ponizej|do|maksymalnie|budżet|budzet|ไม่เกิน|ต่ำกว่า|งบ|สูงสุด|ไม่เกินงบ)\s*(?:is|\s*[:=]|\s+around|\s+about)?\s*([0-9][0-9,.]*)\s*(tys\.?|tysi[eę]cy|tysiecy|thousand|m|million|mln|milionów|ล้าน|ล้านบาท|k)?\s*(?:thb|baht|บาท)?", msg_low):
            tail = msg_low[p_match.end():]
            if re.match(r"\s*(?:bedrooms?|beds?|br\b|sypialn|ห้อง)", tail):
                continue
            if "bed" in p_match.group(0) or "ห้อง" in p_match.group(0):
                continue
            val = float(p_match.group(1).replace(",", ""))
            unit = (p_match.group(2) or "").lower()
            if _is_thousand_unit(unit):
                val *= 1_000
            elif unit in ["m", "million", "mln", "milionów", "ล้าน", "ล้านบาท"] or val < 500:
                val *= 1_000_000
            elif unit == "k":
                val *= 1_000
            filters["max_price"] = val
            break

    # Min Price
    min_match = re.search(r"(?:from|above|minimum|min|od|powyżej|powyzej|ตั้งแต่|มากกว่า|เริ่มต้น)\s*(?:is|\s*[:=])?\s*([0-9][0-9,.]*)\s*(tys\.?|tysi[eę]cy|tysiecy|thousand|m|million|mln|milionów|ล้าน|ล้านบาท|k)?\s*(?:thb|baht|บาท)?", msg_low)
    if "min_price" not in filters and min_match and "bed" not in min_match.group(0) and "ห้อง" not in min_match.group(0):
        val = float(min_match.group(1).replace(",", ""))
        unit = (min_match.group(2) or "").lower()
        if _is_thousand_unit(unit):
            val *= 1_000
        elif unit in ["m", "million", "mln", "milionów", "ล้าน", "ล้านบาท"] or val < 500:
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
    if re.search(r"\bpet[- ]friendly\b", msg_low):
        filters["pet_friendly"] = True
    if re.search(r"\bfurnished\b", msg_low) and "unfurnished" not in msg_low:
        filters["furnished"] = True

    from app.services.conversation_controls import sanitize_extracted_filters
    out = sanitize_extracted_filters(message, filters)
    sort = detect_price_sort(message)
    if sort:
        out["sort_by_price"] = sort
    return out
_SORT_ASC_RE = re.compile(
    r"\b(?:cheapest|lowest[- ]priced?|lowest price|most affordable|least expensive|very cheap|"
    r"free properties|free listings|najta[nń]sz\w*|najmniej drog\w*)\b|ถูกที่สุด|ราคาต่ำสุด",
    re.I,
)
_SORT_DESC_RE = re.compile(
    r"\b(?:most expensive|priciest|highest[- ]priced?|highest price|top end|luxury|najdro[zż]sz\w*)\b|แพงที่สุด|ราคาสูงสุด",
    re.I,
)


def detect_price_sort(message: str) -> Optional[str]:
    """'asc' for 'cheapest / most affordable', 'desc' for 'most expensive'. Otherwise None (relevance order)."""
    msg = message or ""
    if re.search(r"freehold", msg, re.I) and not re.search(r"\b(cheapest|lowest price|very cheap)\b", msg, re.I):
        return None
    if _SORT_ASC_RE.search(msg) and not _SORT_DESC_RE.search(msg):
        return "asc"
    if _SORT_DESC_RE.search(msg) and not _SORT_ASC_RE.search(msg):
        return "desc"
    return None


def _is_thousand_unit(unit: str) -> bool:
    """'100 tys. THB' / '500 thousand' mean thousands; without this a small number was read as millions."""
    return (unit or "").lower().rstrip(".") in ("tys", "tysięcy", "tysiecy", "thousand")


def extract_search_filters_deterministic(message: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Never raises: a malformed number in the text must not break the chat (falls back to no filters)."""
    try:
        return _extract_search_filters_deterministic_impl(message, *args, **kwargs)
    except (ValueError, OverflowError):
        return {}


