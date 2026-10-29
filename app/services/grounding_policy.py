"""
Grounding policy (Module 3): what the public bot may state without live listing data.

Facts about a listing (price, bedrooms, area, yield, status) must come from the listing data and are
checked by reply_validator. Anything an admin types in by hand (taught answers) must therefore not carry
those facts, and must not make promises the business has not approved. This module is the single place
that decides that. Pure Python, no network.
"""
from __future__ import annotations

import re
from typing import List

_MONEY = re.compile(
    r"(?:฿|\$|€|£|\b(?:thb|usd|eur|pln|baht|zł)\b)\s*\d"
    r"|\d[\d.,]*\s*(?:฿|\$|€|£|thb|usd|eur|pln|baht|zł|million|mln)\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"\d+(?:[.,]\d+)?\s*%")
_GUARANTEE = re.compile(
    r"\b(?:guarantee[sd]?|guaranteed|risk[- ]free|assured\s+(?:return|income|yield)|"
    r"promise[sd]?\s+(?:a\s+)?(?:return|income|yield|profit)|gwarantowan\w*)\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions|system\s+prompt|developer\s+message|"
    r"you\s+are\s+now\s+(?:a|an)\b|act\s+as\s+(?:a|an)\b|jailbreak",
    re.IGNORECASE,
)

NO_DATA_REPLIES = {
    "en": "I don't have confirmed information on that. Our team can confirm it for you; share your contact details and they will get back to you.",
    "pl": "Nie mam potwierdzonych informacji na ten temat. Nasz zespół może to potwierdzić; zostaw dane kontaktowe, a skontaktujemy się z Tobą.",
    "th": "ขออภัย ยังไม่มีข้อมูลที่ยืนยันได้ในเรื่องนี้ ทีมงานสามารถยืนยันให้ได้ กรุณาฝากข้อมูลติดต่อไว้ แล้วเราจะติดต่อกลับ",
}


def vet_taught_answer(text: str) -> List[str]:
    """Reasons this text may not be served as a fixed answer. Empty list means it is acceptable."""
    value = text or ""
    reasons: List[str] = []
    if _MONEY.search(value):
        reasons.append("contains a price or currency figure (prices must come from live listings)")
    if _PERCENT.search(value):
        reasons.append("contains a percentage (yield, ROI and similar must come from listing data)")
    if _GUARANTEE.search(value):
        reasons.append("contains a guarantee or promise the business has not approved")
    if _marketing_claim(value):
        reasons.append("contains an unverified marketing claim")
    if _EMAIL.search(value) or _PHONE.search(value):
        reasons.append("contains personal contact details")
    if _INJECTION.search(value):
        reasons.append("looks like a prompt-injection phrase")
    return reasons


def _marketing_claim(text: str) -> bool:
    try:
        from app.services.reply_validator import _UNVERIFIED_MARKETING_PATTERNS
    except Exception:
        return False
    return any(p.search(text) for p in _UNVERIFIED_MARKETING_PATTERNS)


def no_data_reply(language: str = "en") -> str:
    return NO_DATA_REPLIES.get((language or "en").lower()[:2], NO_DATA_REPLIES["en"])
