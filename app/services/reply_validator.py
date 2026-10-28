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


_AMENITY_CLAIM_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # Affirmative amenity claims only — not bare words inside titles like "Luxury Pool Villas"
    ("spa", re.compile(
        r"(?:spa\s*:\s*(?:yes|✓|available|included))|\b(?:has|with|includes?|offers?)\s+(?:a\s+)?spa\b|\bspa\s+yes\b",
        re.I,
    )),
    ("gym", re.compile(
        r"(?:gym\s*:\s*(?:yes|✓))|\b(?:has|with|includes?|offers?)\s+(?:a\s+)?(?:gym|fitness(?:\s+center)?)\b",
        re.I,
    )),
    ("pool", re.compile(
        r"(?:(?:private\s+)?pool\s*:\s*(?:yes|✓))|\b(?:has|with|includes?|offers?)\s+(?:a\s+)?(?:private\s+)?pool\b",
        re.I,
    )),
    ("parking", re.compile(
        r"(?:parking\s*:\s*(?:yes|✓))|\b(?:has|with|includes?|offers?)\s+parking\b",
        re.I,
    )),
    ("sea_view", re.compile(
        r"\b(?:has|with|includes?|offers?)\s+(?:a\s+)?(?:sea|ocean)\s*view\b|"
        r"(?:sea|ocean)\s*view\s*:\s*(?:yes|✓)",
        re.I,
    )),
    ("beach_access", re.compile(
        r"\b(?:has|with|includes?|offers?)\s+beach(?:\s*access|\s*front)?\b|"
        r"beach\s*(?:access|front)?\s*:\s*(?:yes|✓)",
        re.I,
    )),
    ("concierge", re.compile(
        r"\b(?:has|with|includes?|offers?)\s+(?:a\s+)?concierge\b|concierge\s*:\s*(?:yes|✓)",
        re.I,
    )),
]

_OWNERSHIP_CLAIM = re.compile(
    r"\bfreehold\s+for\s+foreigners?\b|\bforeign(?:er)?\s+freehold\b|"
    r"\bfreehold\b|\bleasehold\b|\bforeign\s+quota\b",
    re.I,
)

_YIELD_CLAIM = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%\s*(?:rental\s+)?(?:yield|roi|return|net)|"
    r"(?:rental\s+)?(?:yield|roi|return)\s*(?:of\s*)?(\d+(?:[.,]\d+)?)\s*%|"
    r"\bguaranteed?\s+(?:rental\s+)?(?:income|yield|return)\b|"
    r"\bappreciation\b",
    re.I,
)

_COMPLETION_CLAIM = re.compile(
    r"\b(?:completion|completes?|handover|ready)\s*(?:in\s*)?(?:q[1-4]\s*)?(\d{4})\b|"
    r"\b(q[1-4]\s*\d{4})\b|"
    r"\bcompletion\s*:\s*([^\n.]+)",
    re.I,
)

_DISTANCE_CLAIM = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:km|kilometers?|minutes?|mins?)\b",
    re.I,
)

_AREA_CLAIM = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:sq\.?\s*m|sqm|m²|m2)\b",
    re.I,
)

_BED_CLAIM = re.compile(
    r"(\d+)\s*(?:-?\s*)?(?:bedrooms?|beds?|br)\b",
    re.I,
)


def _normalize_number_tokens(blob: str) -> set:
    """Collect comparable numeric tokens (raw digits and million-scaled)."""
    out: set = set()
    text = blob or ""
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*[Mm](?:illion)?\b", text):
        try:
            v = float(m.group(1).replace(",", ""))
            out.add(str(int(v * 1_000_000)))
            out.add(f"{v:g}m")
        except ValueError:
            pass
    for m in re.finditer(r"\d+(?:[.,]\d+)?", text):
        raw = m.group(0).replace(",", "")
        try:
            f = float(raw)
        except ValueError:
            continue
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            out.add(digits)
        if f >= 1000:
            out.add(str(int(f)))
        # Also store million form
        if f >= 1_000_000:
            out.add(f"{f / 1_000_000:g}m")
        elif 1 <= f < 500 and "m" in text[max(0, m.start() - 2): m.end() + 8].lower():
            out.add(str(int(f * 1_000_000)))
    return out


def _facts_amenity_keys(cards: List[Dict[str, Any]], facts_text: str) -> set:
    keys: set = set()
    low = (facts_text or "").lower()
    try:
        from app.services.amenity_service import collect_property_amenities, AMENITY_LABELS_EN
        for c in cards:
            facts = collect_property_amenities(c)
            for k, f in facts.items():
                if getattr(f, "present", False):
                    keys.add(k)
                    keys.add(AMENITY_LABELS_EN.get(k, k).lower())
    except Exception:
        pass
    for k in ("pool", "private pool", "gym", "spa", "parking", "sea view", "beach", "concierge"):
        if k in low:
            keys.add(k.replace(" ", "_") if " " in k else k)
            keys.add(k)
    return keys


def verify_claims(
    reply_text: str,
    cards: Optional[List[Dict[str, Any]]] = None,
    *,
    facts_text: str = "",
    tool_results: Optional[List[Any]] = None,
    allowed_property_ids: Optional[set] = None,
    allowed_property_names: Optional[set] = None,
) -> Tuple[bool, List[str]]:
    """
    Extract factual claims from reply and check against this-turn FACTS/cards.
    Pure Python, no network. Returns (ok, reasons).
    """
    reasons: List[str] = []
    text = reply_text or ""
    cards = [c for c in (cards or []) if isinstance(c, dict)]
    facts_blob = (facts_text or "") + " " + _allowed_text_blob(cards, tool_results)
    facts_low = facts_blob.lower()
    num_ok = _normalize_number_tokens(facts_blob)

    allowed_ids = {str(x).lower() for x in (allowed_property_ids or set()) if x}
    allowed_names = {str(x).lower().strip() for x in (allowed_property_names or set()) if x}
    for c in cards:
        for k in ("property_id", "id", "slug"):
            if c.get(k):
                allowed_ids.add(str(c.get(k)).lower())
        for k in ("name", "property_name"):
            if c.get(k):
                allowed_names.add(str(c.get(k)).lower().strip())

    # --- Property names / Title Case projects ---
    _GENERIC = {
        "luxury", "pool", "villas", "villa", "condo", "condos", "apartment", "beachfront",
        "sea", "view", "phuket", "samui", "bangkok", "thailand", "property", "properties",
        "residences", "residence", "estate", "verified", "listings", "lake",
    }
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9\-]+){1,6})\b", text):
        name = m.group(1).strip()
        low = name.lower()
        toks = [t for t in re.split(r"[^a-z0-9]+", low) if t and t not in _GENERIC]
        if not toks:
            continue
        if any(low == n or low in n or n in low for n in allowed_names):
            continue
        if all(t in facts_low for t in toks):
            continue
        if any(k in low for k in ("villa", "villas", "condo", "tower", "towers", "residence", "residences", "estate")):
            reasons.append(f"invented_property_name:{name}")

    # --- Amenity claims ---
    am_keys = _facts_amenity_keys(cards, facts_blob)
    for canon, pat in _AMENITY_CLAIM_PATTERNS:
        if not pat.search(text):
            continue
        # Negation / "not in verified" is fine
        for m in pat.finditer(text):
            window = text[max(0, m.start() - 40): m.end() + 40].lower()
            if any(x in window for x in ("not listed", "not in our", "not documented", "brak w", "ไม่ระบุ", "confirm with")):
                continue
            aliases = {canon, canon.replace("_", " "), canon.replace("_", "")}
            if canon == "pool":
                aliases.update({"private pool", "pool_private"})
            if not (aliases & am_keys) and canon not in facts_low and canon.replace("_", " ") not in facts_low:
                # Only fail affirmative claims
                if re.search(r"(?:yes|✓|has|with|includes?|offers?)\b", window) or ":" in m.group(0):
                    reasons.append(f"amenity_not_in_facts:{canon}")

    # --- Ownership ---
    for m in _OWNERSHIP_CLAIM.finditer(text):
        claim = m.group(0).lower()
        window = text[max(0, m.start() - 50): m.end() + 50].lower()
        if any(x in window for x in ("confirm", "advisor", "not documented", "not in our", "doradca")):
            continue
        own_blob = " ".join(
            str(c.get("ownership") or c.get("tenure") or "") for c in cards
        ).lower() + " " + facts_low
        if "freehold" in claim and "freehold" not in own_blob:
            reasons.append(f"ownership_not_in_facts:{m.group(0)[:40]}")
        elif "leasehold" in claim and "leasehold" not in own_blob:
            reasons.append(f"ownership_not_in_facts:{m.group(0)[:40]}")
        elif "foreign" in claim and "foreign" not in own_blob and "freehold" not in own_blob:
            reasons.append(f"ownership_not_in_facts:{m.group(0)[:40]}")

    # --- Yield / ROI words without a verified yield figure ---
    from app.services.approved_statements import DEVELOPER_MARKETING_CLAIM_LABEL
    if _YIELD_CLAIM.search(text) or re.search(r"\b(?:roi|rental\s+yield|guaranteed\s+return)\b", text, re.I):
        window_all = text.lower()
        if not any(k in facts_low for k in ("yield", "roi", "return")) and "advisor" not in window_all:
            if not any(x in window_all for x in ("don't quote", "do not quote", "not quote", "can't guarantee", "cannot guarantee")):
                reasons.append("yield_or_roi_not_in_facts:advisory")

    # --- Yield / ROI / appreciation / stray % — never unless in verified yield data ---
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*%", text):
        window = text[max(0, m.start() - 48): m.end() + 48].lower()
        pct_token = m.group(0).lower().replace(" ", "")
        in_yield_facts = (
            pct_token in facts_low.replace(" ", "")
            and any(k in facts_low for k in ("yield", "roi", "return", "%"))
        )
        approved = any(pct_token in str(s).lower().replace(" ", "") for s in (APPROVED_STATEMENTS or []))
        labelled = DEVELOPER_MARKETING_CLAIM_LABEL.lower() in window
        yieldish = bool(_YIELD_CLAIM.search(window)) or any(
            k in window for k in ("yield", "roi", "return", "guaranteed", "marketing claim", "zwrot", "gwarant")
        )
        # Any percentage tied to investment language (or left after sanitiser) must be in FACTS
        if yieldish or labelled:
            if not in_yield_facts and not approved:
                reasons.append(f"yield_or_roi_not_in_facts:{m.group(0)[:50]}")
        elif not in_yield_facts and not approved and labelled:
            reasons.append(f"yield_or_roi_not_in_facts:{m.group(0)[:50]}")

    # --- Completion dates / years ---
    for m in _COMPLETION_CLAIM.finditer(text):
        year_bits = [g for g in m.groups() if g]
        token = (year_bits[0] if year_bits else m.group(0)).strip()
        if token.lower() in facts_low or any(tok in facts_low for tok in re.findall(r"\d{4}", token)):
            continue
        # Generic "under construction" without a year is ok if status matches
        if re.search(r"under\s+construction|off-?plan|completed", m.group(0), re.I) and not re.search(r"\d{4}", m.group(0)):
            continue
        if re.search(r"\d{4}|q[1-4]", m.group(0), re.I):
            reasons.append(f"completion_date_not_in_facts:{m.group(0)[:40]}")

    # --- Distances ---
    for m in _DISTANCE_CLAIM.finditer(text):
        num = m.group(1).replace(",", ".")
        window = text[max(0, m.start() - 30): m.end() + 20].lower()
        if any(x in window for x in ("not in our", "not documented", "confirm")):
            continue
        digs = re.sub(r"[^\d]", "", num)
        if digs and digs not in re.sub(r"[^\d]", "", facts_blob) and f"{num} km" not in facts_low and f"{num} min" not in facts_low:
            reasons.append(f"distance_not_in_facts:{m.group(0)[:30]}")

    # --- Areas ---
    for m in _AREA_CLAIM.finditer(text):
        num = m.group(1).replace(",", "")
        digs = re.sub(r"[^\d]", "", num)
        if digs and digs not in re.sub(r"[^\d]", "", facts_blob):
            reasons.append(f"area_not_in_facts:{m.group(0)[:30]}")

    # --- Bedroom counts ---
    for m in _BED_CLAIM.finditer(text):
        n = m.group(1)
        if n in re.findall(r"\d+", facts_blob) or n in facts_low:
            continue
        # Allow ordinals / "first bedroom" style rare; fail clear inventory claims
        window = text[max(0, m.start() - 20): m.end() + 10].lower()
        if any(x in window for x in ("has", "with", "bedrooms:", "beds:", "•")):
            reasons.append(f"bedrooms_not_in_facts:{m.group(0)[:30]}")

    # --- Prices / large money (reuse digit check with million normalisation) ---
    for m in re.finditer(
        r"(?:THB|USD|EUR|PLN|฿|€|\$)\s*[\d,.]+(?:\s*[Mm](?:illion)?)?|"
        r"[\d,.]+\s*(?:M\s*)?(?:THB|USD|EUR|PLN|฿)|"
        r"(\d+(?:\.\d+)?)\s*[Mm]\b",
        text,
        flags=re.IGNORECASE,
    ):
        chunk = m.group(0)
        window = text[max(0, m.start() - 24): m.end() + 12].lower()
        # Budget / filter phrasing ("under 5M", "below 10M", "20M-30M") is not a listing price claim
        if re.search(r"\b(?:under|below|up to|max(?:imum)?|budget|less than|poniżej|do)\b", window):
            continue
        around = text[max(0, m.start() - 48): m.end() + 48].lower()
        if re.search(r"\bbetween\b", around) or re.search(r"\d+(?:\.\d+)?\s*m\s*-\s*\d+(?:\.\d+)?\s*m", around):
            continue
        if "already the cheapest" in around:
            continue
        claim_nums = _normalize_number_tokens(chunk)
        if not claim_nums:
            continue
        if claim_nums & num_ok:
            continue
        # Millions soft match: 12.5M vs 12500000
        ok_soft = False
        for cn in claim_nums:
            if cn.endswith("m"):
                try:
                    mill = float(cn[:-1])
                    approx = str(int(mill * 1_000_000))
                    if approx in num_ok or approx[:4] in {x[:4] for x in num_ok if x.isdigit() and len(x) >= 4}:
                        ok_soft = True
                except ValueError:
                    pass
        if not ok_soft:
            reasons.append(f"price_not_in_facts:{chunk[:40]}")

    # Deduplicate while preserving order
    seen_r: set = set()
    uniq: List[str] = []
    for r in reasons:
        if r not in seen_r:
            seen_r.add(r)
            uniq.append(r)
    return (len(uniq) == 0, uniq)


def validate_reply(
    reply_text: str,
    cards: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Any]] = None,
    applied_filters: Optional[Dict[str, Any]] = None,
    language: str = "en",
    tenant_advisor_email: Optional[str] = None,
    facts_text: str = "",
    allowed_property_ids: Optional[set] = None,
    allowed_property_names: Optional[set] = None,
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
    if facts_text:
        allowed = f"{allowed} {facts_text}"

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
    if allowed_property_names:
        known_names = list(known_names) + [
            str(n).strip().lower() for n in allowed_property_names if n
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

    # d. Free-text claim verification against this-turn FACTS / cards
    claim_ok, claim_reasons = verify_claims(
        reply_text=text,
        cards=cards,
        facts_text=facts_text or allowed,
        tool_results=tool_results,
        allowed_property_ids=allowed_property_ids,
        allowed_property_names=allowed_property_names,
    )
    if not claim_ok:
        reasons.extend(claim_reasons)

    return (len(reasons) == 0, reasons)
