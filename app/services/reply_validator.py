"""
Pure, fast reply validation against retrieved listing data.
No network calls. Used by the public chatbot only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.approved_statements import APPROVED_STATEMENTS, DEVELOPER_MARKETING_CLAIM_LABEL

_UNVERIFIED_MARKETING_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"zero\s+construction\s+risk",
        r"immediate\s+rental\s+income(?:\s+from\s+day\s+one)?",
        r"premium\s+weekly\s+rates?",
        r"gwarantowane?\s+\d+(?:[.,]\d+)?\s*%",
        r"guaranteed?\s+\d+(?:[.,]\d+)?\s*%\s*(?:roi|net|return)?",
        r"guaranteed?\s+(?:rental\s+)?(?:income|yield|return)",
        r"no\s+construction\s+risk",
    )
]


def sanitize_listing_line_text(text: str) -> str:
    """Strip / label investment promises so they are not repeated as bot facts."""
    import re
    from app.services.listing_presentation import sanitize_marketing_claims
    out = sanitize_marketing_claims(text or "", mode="description")
    # "(~€confirm with advisor / ~PLN available on request)" placeholders when no conversion exists
    out = re.sub(
        r"\s*\((?:~?\s*€[^()\d]*|~?\s*PLN[^()\d]*)(?:/\s*~?[^()\d]*PLN[^()\d]*)?\)",
        "", out, flags=re.IGNORECASE)
    return out


def _card_price_numeric(card: Dict[str, Any]) -> Optional[float]:
    for key in ("price_numeric_min", "price_min", "min_price", "price_thb", "price_numeric"):
        val = card.get(key)
        if val is None or val == "":
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    raw = str(card.get("price") or card.get("price_display") or "").lower()
    if not raw or "request" in raw or raw in ("n/a", "na"):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\b", raw)
    if m:
        return float(m.group(1)) * 1_000_000
    digits = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _is_price_on_request(card: Dict[str, Any]) -> bool:
    raw = str(card.get("price") or card.get("price_display") or "").strip().lower()
    return (not raw) or ("request" in raw) or raw in ("n/a", "na", "-")


def _location_ok(card: Dict[str, Any], location: Optional[str]) -> bool:
    if not location:
        return True
    want = str(location).strip().lower()
    if want in ("thailand", "tajlandia", "ไทย", "ประเทศไทย"):
        return True
    blob = " ".join(
        str(card.get(k) or "")
        for k in ("location", "city", "province", "district", "name", "property_name")
    ).lower()
    # Allow broader matches (e.g. filter Phuket, card says "Phuket, Layan")
    tokens = [t for t in re.split(r"[^a-z0-9]+", want) if len(t) >= 3]
    if not tokens:
        return True
    return any(t in blob for t in tokens)


def _type_ok(card: Dict[str, Any], property_type: Optional[str]) -> bool:
    if not property_type:
        return True
    want = str(property_type).strip().lower()
    got = str(card.get("property_type") or card.get("type") or "").strip().lower()
    if not got:
        return True  # missing type: do not invent a fail on incomplete records
    return want in got or got in want


def _beds_ok(card: Dict[str, Any], bedrooms: Optional[Any]) -> bool:
    if bedrooms is None or bedrooms == "":
        return True
    try:
        want = int(float(bedrooms))
    except (TypeError, ValueError):
        return True
    raw = card.get("bedrooms")
    if raw is None or raw == "":
        return True
    text = str(raw)
    nums = [int(x) for x in re.findall(r"\d+", text)]
    if not nums:
        return True
    return min(nums) <= want <= max(nums) or want in nums


def _allowed_text_blob(cards: List[Dict[str, Any]], tool_results: Optional[List[Any]]) -> str:
    parts: List[str] = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        for v in c.values():
            if v is None:
                continue
            if isinstance(v, (list, dict)):
                parts.append(str(v))
            else:
                parts.append(str(v))
    for tr in tool_results or []:
        parts.append(str(tr))
    for s in APPROVED_STATEMENTS or []:
        parts.append(str(s))
    return " ".join(parts).lower()


def validate_reply(
    reply_text: str,
    cards: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Any]] = None,
    applied_filters: Optional[Dict[str, Any]] = None,
    language: str = "en",
    tenant_advisor_email: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """
    Returns (ok, reasons). Pure and fast. No network I/O.
    """
    reasons: List[str] = []
    text = reply_text or ""
    cards = [c for c in (cards or []) if isinstance(c, dict)]
    filters = applied_filters or {}
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")
    location = filters.get("location")
    property_type = filters.get("property_type")
    bedrooms = filters.get("bedrooms")
    asked_name = (
        filters.get("query")
        or filters.get("property_name")
        or filters.get("asked_property_name")
        or filters.get("name_query")
    )

    # Named-property search: returned cards must include that property (by name / id).
    # Otherwise random real listings for a name query would incorrectly pass.
    if asked_name and str(asked_name).strip():
        from app.services.property_name_index import (
            distinctive_tokens,
            normalize_property_name,
        )

        asked_norm = normalize_property_name(str(asked_name))
        asked_tokens = distinctive_tokens(str(asked_name))
        if asked_tokens:
            def _card_matches_asked(c: Dict[str, Any]) -> bool:
                blob = normalize_property_name(
                    " ".join(
                        str(c.get(k) or "")
                        for k in ("name", "property_name", "property_id", "id", "slug")
                    )
                )
                if asked_norm and asked_norm == blob:
                    return True
                if asked_norm and asked_norm in blob:
                    return True
                card_tokens = set(distinctive_tokens(blob))
                return bool(asked_tokens) and all(t in card_tokens for t in asked_tokens)

            if cards and not any(_card_matches_asked(c) for c in cards):
                reasons.append(f"named_property_missing:{str(asked_name).strip()[:80]}")
            # If there are cards but none match, also fail even when empty? empty is ok for not-found
            # (honest zero-card reply). Only fail when wrong cards are present.

    try:
        max_p = float(max_price) if max_price is not None else None
    except (TypeError, ValueError):
        max_p = None
    try:
        min_p = float(min_price) if min_price is not None else None
    except (TypeError, ValueError):
        min_p = None

    budget_search = max_p is not None or min_p is not None

    # a. Cards must respect applied filters
    for c in cards:
        pid = c.get("property_id") or c.get("name") or "?"
        price_num = _card_price_numeric(c)
        if budget_search and price_num is None:
            if not _is_price_on_request(c):
                reasons.append(f"null_price_on_budget_search:{pid}")
            elif "price on request" not in text.lower() and "cena na zapytanie" not in text.lower() and "สอบถามราคา" not in text:
                reasons.append(f"price_on_request_unlabelled:{pid}")
        if max_p is not None and price_num is not None and price_num > max_p * 1.001:
            reasons.append(f"over_max_price:{pid}")
        if min_p is not None and price_num is not None and price_num < min_p * 0.999:
            reasons.append(f"under_min_price:{pid}")
        if not _location_ok(c, location):
            reasons.append(f"location_mismatch:{pid}")
        if not _type_ok(c, property_type):
            reasons.append(f"type_mismatch:{pid}")
        if not _beds_ok(c, bedrooms):
            reasons.append(f"bedrooms_mismatch:{pid}")

    allowed = _allowed_text_blob(cards, tool_results)

    # b0. Unverified marketing / guaranteed-return claims must not be stated as facts
    # (allowed only when explicitly labelled as developer marketing claim)
    low_text = text.lower()
    for pat in _UNVERIFIED_MARKETING_PATTERNS:
        for m in pat.finditer(text):
            window = text[max(0, m.start() - 40) : m.end() + 40].lower()
            if DEVELOPER_MARKETING_CLAIM_LABEL.lower() in window:
                continue
            reasons.append(f"unverified_marketing_claim:{m.group(0)[:60]}")

    # b. Percentages / yields only if in retrieved data or APPROVED_STATEMENTS
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", text):
        token = m.group(0).lower().replace(" ", "")
        span_ctx = text[max(0, m.start() - 24): m.end() + 24].lower()
        if any(k in span_ctx for k in ("yield", "roi", "return", "zwrot", "ผลตอบแทน", "gwarant")):
            approved_hit = any(token in str(s).lower().replace(" ", "") for s in (APPROVED_STATEMENTS or []))
            in_data = token in allowed.replace(" ", "")
            labelled = DEVELOPER_MARKETING_CLAIM_LABEL.lower() in span_ctx
            if not approved_hit and not in_data and not labelled:
                reasons.append(f"unapproved_percentage:{m.group(0)}")

    # Currency / large money amounts mentioned must appear in allowed blob (digits)
    for m in re.finditer(
        r"(?:THB|USD|EUR|PLN|฿|€|\$)\s*[\d,.]+(?:\s*[Mm](?:illion)?)?|[\d,.]+\s*(?:M\s*)?(?:THB|USD|EUR|PLN|฿)",
        text,
        flags=re.IGNORECASE,
    ):
        chunk = m.group(0)
        digits = re.sub(r"[^\d]", "", chunk)
        if len(digits) < 3:
            continue
        if digits not in re.sub(r"[^\d]", "", allowed) and chunk.lower() not in allowed:
            # Allow rounded display like 28.5M when 28500000 in data
            ok_round = False
            if re.search(r"(\d+(?:\.\d+)?)\s*m", chunk, flags=re.I):
                mill = float(re.search(r"(\d+(?:\.\d+)?)\s*m", chunk, flags=re.I).group(1))
                approx = int(mill * 1_000_000)
                if str(approx)[:4] in re.sub(r"[^\d]", "", allowed):
                    ok_round = True
            if not ok_round:
                reasons.append(f"price_not_in_data:{chunk[:40]}")

    # Property-name check: Title Case phrases that look like project names.
    # Generic descriptive words never count; only flag when a distinctive leftover
    # remains and that leftover is not present in cards / tool_results.
    known_names = [
        str(c.get("name") or c.get("property_name") or "").strip().lower()
        for c in cards
        if c.get("name") or c.get("property_name")
    ]
    _GENERIC_NAME_TOKENS = {
        "luxury", "pool", "villas", "villa", "condo", "condos", "condominium",
        "apartment", "apartments", "beachfront", "sea", "view", "seaview",
        "sea-view", "hillside", "modern", "private", "premium", "new",
        "off-plan", "offplan", "freehold", "leasehold", "phuket", "samui",
        "bangkok", "pattaya", "thailand", "bang", "tao", "layan", "kamala",
        "karon", "rawai", "surin", "naithon", "cherngtalay", "real", "estate",
        "property", "properties", "koh", "verified", "listings", "listing",
        "criteria", "beach", "access", "residences", "residence",
    }
    _PROJECTISH = ("villa", "villas", "condo", "condos", "residence", "residences", "estate")

    def _distinctive_tokens(phrase_low: str) -> List[str]:
        # Drop multi-word generics first
        cleaned = phrase_low.replace("real estate", " ").replace("sea view", " ").replace("sea-view", " ")
        return [t for t in re.split(r"[^a-z0-9]+", cleaned) if t and t not in _GENERIC_NAME_TOKENS]

    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9\-]+){1,6})\b", text):
        name = m.group(1).strip()
        low = name.lower()
        distinctive = _distinctive_tokens(low)
        if not distinctive:
            continue  # purely generic (e.g. "Luxury Pool Villas", "Phuket Villas")
        if any(low == kn or low in kn or kn in low for kn in known_names):
            continue
        # Distinctive leftover must appear in allowed blob (card names / tool results)
        dist_blob = " ".join(distinctive)
        if dist_blob in allowed or all(tok in allowed for tok in distinctive):
            continue
        if any(k in low for k in _PROJECTISH):
            tag = "invented_property_name" if not known_names else "unknown_property_name"
            reasons.append(f"{tag}:{name}")

    # c. Advisor email must be the current tenant's (if any email is shown)
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if tenant_advisor_email:
        allowed_email = tenant_advisor_email.strip().lower()
        for em in emails:
            if em.lower() != allowed_email:
                reasons.append(f"wrong_advisor_email:{em}")

    return (len(reasons) == 0, reasons)
