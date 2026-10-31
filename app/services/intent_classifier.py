"""
Intent classifier for chat log enrichment and teaching detection.

classify_query(text, agent_type="public") -> IntentResult
  - Returns a dataclass with: intent (str), confidence (float), detected_language (str)
  - Intents: property_search, price_inquiry, schedule_viewing, off_topic,
             admin_teach_knowledge, create_policy_rule, general_faq,
             answer_correction_request, document_request, report_request,
             greeting, link_ingestion
  - Language detection: "en", "pl", "th" (simple heuristic, no external API)
  - No external LLM call — pure regex/keyword heuristic is fine for MVP.
  - Teaching phrases trigger admin_teach_knowledge: "remember:", "learn that", "rule:",
    "note:", "from now on", "always respond", "teach", "pamiętaj:", "zapamiętaj"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class IntentResult:
    intent: str
    confidence: float
    detected_language: str


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
_POLISH_CHARS = set("ąęóśźżćńłĄĘÓŚŹŻĆŃŁ")


_POLISH_WORDS = {"wille", "willa", "szukam", "zakup", "sprzedaż", "sprzedaz", "polsce", "polski", "pokoje", "cena", "cudzoziemiec"}

def _detect_language(text: str) -> str:
    """
    Heuristic language detection — no external API.
    Priority: Thai > Polish > English.
    """
    if not text:
        return "en"
    if _THAI_RE.search(text):
        return "th"
    if any(ch in _POLISH_CHARS for ch in text):
        return "pl"
    low = text.lower()
    words = set(re.findall(r"\w+", low))
    if words & _POLISH_WORDS:
        return "pl"
    return "en"


# ---------------------------------------------------------------------------
# Intent rules: list of (intent_name, patterns, base_confidence)
# Patterns are matched case-insensitively against the whole text.
# First matching rule wins (highest-priority rules appear first).
# ---------------------------------------------------------------------------

_TEACHING_PHRASES: List[str] = [
    r"\bremember\s*:",
    r"\blearn\s+that\b",
    r"\brule\s*:",
    r"\bnote\s*:",
    r"\bfrom\s+now\s+on\b",
    r"\balways\s+respond\b",
    r"\bteach\b",
    r"pamiętaj\s*:",
    r"zapamiętaj\b",
    r"zarejestruj\b",
    r"ustaw\s+regułę\b",
]

_POLICY_PHRASES: List[str] = [
    r"\bcreate\s+(?:a\s+)?(?:new\s+)?(?:policy|rule)\b",
    r"\bset\s+(?:a\s+)?(?:policy|rule)\b",
    r"\badd\s+(?:a\s+)?(?:policy|rule)\b",
    r"\bnew\s+(?:policy|rule)\b",
]

_CORRECTION_PHRASES: List[str] = [
    r"\bthat\s+(?:answer|response)\s+(?:is|was)\s+(?:wrong|incorrect)\b",
    r"\byou\s+(?:got\s+it|are)\s+wrong\b",
    r"\bcorrect\s+your(?:self)?\b",
    r"\bwrong\s+answer\b",
    r"\bincorrect\s+(?:answer|response)\b",
    r"\bplease\s+correct\b",
    r"\bfix\s+your\s+(?:answer|response)\b",
]

_PROPERTY_SEARCH_PHRASES: List[str] = [
    r"\b(?:find|search|show|list|looking\s+for|browse|explore)\s+(?:me\s+)?(?:a\s+|some\s+)?(?:properties?|listings?|homes?|villas?|condos?|apartments?|houses?)\b",
    r"\b(?:properties?|listings?|homes?|villas?|condos?|apartments?|houses?)\s+(?:in|near|around|for\s+sale|for\s+rent|available)\b",
    r"\bproperty\s+(?:search|listing|available)\b",
    r"\b(?:for\s+sale|for\s+rent)\b",
    r"\b(?:buy|rent)\s+(?:a\s+)?(?:property|home|villa|condo|apartment|house)\b",
    r"\bbedroom(?:s)?\s+(?:in|near|with|for)?\b",
    r"\b(?:\d+\s*(?:bed|bedroom|bath|bathroom))\b",
    r"\b(?:pool|sea\s*view|beachfront|ocean\s*view)\s+(?:property|villa|home|condo)?\b",
]

_PRICE_INQUIRY_PHRASES: List[str] = [
    r"\b(?:how\s+much|what\s+(?:is\s+the\s+)?(?:price|cost|rate))\b",
    r"\b(?:price|cost|fee|rate|budget|afford|pricing|value)\b",
    r"\b(?:cheap|expensive|affordable)\b",
    r"\b(?:thb|baht|฿|\$|usd|eur|gbp)\s*[\d,.]+\b",
    r"\b[\d,.]+\s*(?:thb|baht|฿|\$|usd|eur|gbp)\b",
    r"\bper\s+(?:month|night|year|sqm|sq\.m)\b",
]

_SCHEDULE_VIEWING_PHRASES: List[str] = [
    r"\b(?:schedule|book|arrange|request|set\s+up)\s+(?:a\s+)?(?:viewing|visit|tour|appointment|showing)\b",
    r"\b(?:can\s+I|could\s+I|I\s+(?:want|would\s+like)\s+to)\s+(?:visit|see|view|tour)\b",
    r"\b(?:viewing|visit|tour|appointment)\s+(?:request|booking|schedule)\b",
    r"\bwhen\s+(?:can|could)\s+I\s+(?:see|visit|view|tour)\b",
    r"\bshow\s+me\s+(?:the\s+)?property\b",
]

_DOCUMENT_REQUEST_PHRASES: List[str] = [
    r"\b(?:brochure|leaflet|flyer|pdf|document|spec\s+sheet|floor\s+plan|floor\s*plan|blueprint)\b",
    r"\bsend\s+(?:me\s+)?(?:the\s+)?(?:details?|info|information|documents?)\b",
    r"\b(?:download|export)\s+(?:the\s+)?(?:details?|document|report|brochure)\b",
]

_REPORT_REQUEST_PHRASES: List[str] = [
    r"\b(?:generate|create|produce|run|export|give\s+me)\s+(?:a\s+)?(?:report|summary|analysis|analytics|stats?|statistics|overview)\b",
    r"\b(?:monthly|weekly|daily|annual|quarterly)\s+(?:report|summary|stats?)\b",
    r"\b(?:performance|sales|lead|revenue|conversion)\s+(?:report|stats?|summary|data|numbers)\b",
    r"\bshow\s+(?:me\s+)?(?:the\s+)?(?:stats?|statistics|analytics|numbers|data)\b",
]

_LINK_INGESTION_PHRASES: List[str] = [
    r"https?://\S+",
    r"\b(?:ingest|index|crawl|import|add)\s+(?:this\s+)?(?:url|link|page|website|site)\b",
    r"\b(?:read|fetch|scrape)\s+(?:this\s+)?(?:url|link|page)\b",
]

_GREETING_PHRASES: List[str] = [
    r"^\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|howdy|greetings|sup|what'?s\s+up)\b",
    r"^\s*(?:cześć|witaj|dzień\s+dobry|dobry\s+(?:ranek|wieczór))\b",
    r"^\s*(?:สวัสดี|หวัดดี)\b",
    r"^\s*(?:how\s+are\s+you|how\s+do\s+you\s+do)\b",
]

_FAQ_PHRASES: List[str] = [
    r"\b(?:what|who|where|when|why|how)\b",
    r"\b(?:tell\s+me|explain|describe|info(?:rmation)?|detail(?:s)?)\b",
]

# ---------------------------------------------------------------------------
# Compiled rule table: (intent, compiled_patterns, confidence)
# ---------------------------------------------------------------------------

def _compile(patterns: List[str]) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE | re.UNICODE)


_RULES: List[Tuple[str, re.Pattern, float]] = [
    ("admin_teach_knowledge",    _compile(_TEACHING_PHRASES),      0.95),
    ("create_policy_rule",       _compile(_POLICY_PHRASES),        0.92),
    ("answer_correction_request",_compile(_CORRECTION_PHRASES),    0.90),
    ("link_ingestion",           _compile(_LINK_INGESTION_PHRASES),0.88),
    ("schedule_viewing",         _compile(_SCHEDULE_VIEWING_PHRASES), 0.88),
    ("document_request",         _compile(_DOCUMENT_REQUEST_PHRASES), 0.87),
    ("report_request",           _compile(_REPORT_REQUEST_PHRASES),0.87),
    ("price_inquiry",            _compile(_PRICE_INQUIRY_PHRASES), 0.85),
    ("property_search",          _compile(_PROPERTY_SEARCH_PHRASES), 0.85),
    ("greeting",                 _compile(_GREETING_PHRASES),      0.82),
    ("general_faq",              _compile(_FAQ_PHRASES),           0.60),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_query(text: str, agent_type: str = "public") -> IntentResult:
    """
    Classify a user query into a canonical intent string and detect language.

    Parameters
    ----------
    text : str
        The raw message text.
    agent_type : str
        "public" or "admin". Teaching intents are only flagged for admin agents
        by default; for public agents the confidence is dampened so downstream
        callers can decide how to handle them.

    Returns
    -------
    IntentResult
        dataclass with intent, confidence, and detected_language.
    """
    clean = (text or "").strip()
    detected_language = _detect_language(clean)

    if not clean:
        return IntentResult(
            intent="off_topic",
            confidence=0.5,
            detected_language=detected_language,
        )

    for intent, pattern, base_confidence in _RULES:
        if pattern.search(clean):
            # Dampen teaching/policy intents for public-facing agents
            confidence = base_confidence
            if agent_type == "public" and intent in ("admin_teach_knowledge", "create_policy_rule"):
                confidence = round(base_confidence * 0.4, 4)
            return IntentResult(
                intent=intent,
                confidence=round(confidence, 4),
                detected_language=detected_language,
            )

    return IntentResult(
        intent="off_topic",
        confidence=0.45,
        detected_language=detected_language,
    )
