"""
Listing presentation helpers for the public chatbot.
- Display names (prefer name.en in English chats)
- Short locations
- Ownership only from verified data
- Strip / label investment marketing claims
- Clean highlight labels (canonical amenities only)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.amenity_service import (
    AMENITY_LABELS_EN,
    collect_property_amenities,
    map_token_to_canonical,
)
from app.services.approved_statements import DEVELOPER_MARKETING_CLAIM_LABEL

# Investment / guarantee marketing — never repeat as bot facts
_MARKETING_CLAIM_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"zero\s+construction\s+risk",
        r"immediate\s+rental\s+income(?:\s+from\s+day\s+one)?",
        r"premium\s+weekly\s+rates?",
        r"gwarantowane?\s+\d+(?:[.,]\d+)?\s*%\s*(?:roi|zwrotu?)?(?:\s*netto)?",
        r"guaranteed?\s+\d+(?:[.,]\d+)?\s*%\s*(?:roi|net(?:to)?|return)?",
        r"\d+(?:[.,]\d+)?\s*%\s*(?:roi|netto|net\s+roi|guaranteed?\s+return)",
        r"guaranteed?\s+(?:rental\s+)?(?:income|yield|return)",
        r"assured\s+(?:rental\s+)?(?:income|yield|return)",
        r"risk[- ]free\s+(?:investment|return)",
        r"no\s+construction\s+risk",
        # Yield / ROI figures: the production data holds none (all 0), so a % from marketing text is unverified
        r"(?:(?:rental|net|gross|annual|expected|projected|total)\s+)*(?:yields?|roi|returns?)\s*(?:projections?\s*)?(?:of|at|up\s+to|around|from|:)?\s*(?:~|about|approximately|up\s+to)?\s*\d+(?:[.,]\d+)?\s*(?:[-–]\s*\d+(?:[.,]\d+)?\s*)?%",
        r"\d+(?:[.,]\d+)?\s*(?:[-–]\s*\d+(?:[.,]\d+)?\s*)?%\s*(?:(?:rental|net|gross|annual|expected|projected)\s+)*(?:yields?|returns?)",
    )
]

_POLISH_MARKETING_WORDS = re.compile(
    r"\b(?:gwarantowane?|netto|zwrot|inwestycyjn\w*|ekskluzywn\w*|luksusow\w*)\b",
    re.IGNORECASE,
)

_OWNERSHIP_FIELD_KEYS = (
    "ownership", "ownership_type", "ownershipType", "tenure", "tenureType",
    "legalStructure", "legal_structure", "titleType", "title_type",
)

_OWNERSHIP_DESC_PATTERNS = [
    re.compile(
        r"(?:30[-\s]?year(?:\s+registered)?\s+lease(?:hold)?(?:\s+with\s+(?:a\s+)?freehold\s+option)?)",
        re.I,
    ),
    re.compile(r"(?:foreign\s+freehold(?:\s+condo(?:minium)?)?)", re.I),
    re.compile(r"(?:freehold\s+(?:building|villa)|leasehold\s+(?:land|villa))", re.I),
    re.compile(r"(?:chanote|freehold|leasehold)", re.I),
]


def slug_to_title(slug: str) -> str:
    s = (slug or "").strip()
    if not s:
        return ""
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(w.capitalize() for w in s.split() if w)

# Location slugs / lower-case names -> display form. Display only: stored ids, slugs and filter keys are never changed.
_LOCATION_DISPLAY = {
    "samui": "Koh Samui", "koh samui": "Koh Samui", "ko samui": "Koh Samui",
    "phangan": "Koh Phangan", "koh phangan": "Koh Phangan", "ko phangan": "Koh Phangan",
    "phnom penh": "Phnom Penh", "siem reap": "Siem Reap", "sihanoukville": "Sihanoukville",
    "hua hin": "Hua Hin", "koh rong": "Koh Rong", "koh chang": "Koh Chang", "koh tao": "Koh Tao",
}
_THAI_SCRIPT = re.compile(r"[\u0E00-\u0E7F]")
_EMPTY_PARTS = {"none", "null", "nan", "undefined"}


def display_location(loc: Any) -> str:
    """Display form of a location name or slug, part by part (comma separated).

    'phuket' -> 'Phuket', 'samui' -> 'Koh Samui', 'hua-hin' -> 'Hua Hin', 'phnom penh' -> 'Phnom Penh'.
    A part that already has a capital letter, or any Thai-script text, is left untouched; None/null parts are
    dropped and repeated neighbours ('samui, Koh Samui') collapse. Never use the result as a filter key or id.
    """
    if loc is None:
        return ""
    out: List[str] = []
    for raw in str(loc).split(","):
        part = raw.strip()
        if not part or part.lower() in _EMPTY_PARTS:
            continue
        if not _THAI_SCRIPT.search(part) and part == part.lower():
            spaced = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", part)).strip()
            part = _LOCATION_DISPLAY.get(spaced) or " ".join(w[:1].upper() + w[1:] for w in spaced.split(" "))
        if not out or out[-1].lower() != part.lower():
            out.append(part)
    return ", ".join(out)


def _looks_like_long_polish_marketing(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 40:
        return False
    # Many Polish diacritics or marketing keywords in a long sentence
    pl_chars = len(re.findall(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", t))
    if pl_chars >= 3 and (" " in t) and len(t.split()) >= 6:
        return True
    if _POLISH_MARKETING_WORDS.search(t) and len(t.split()) >= 5:
        return True
    return False


def resolve_display_name(
    data: Dict[str, Any],
    doc_id: str = "",
    language: str = "en",
) -> str:
    """
    Prefer name.en for English chats. If empty, use cleaned slug title or a
    non-marketing fallback — never dump long Polish marketing sentences in EN.
    """
    lang = (language or "en").lower()
    raw = data.get("name") or data.get("propertyName") or data.get("projectName") or data.get("title") or ""
    en = pl = th = ""
    if isinstance(raw, dict):
        en = str(raw.get("en") or "").strip()
        pl = str(raw.get("pl") or "").strip()
        th = str(raw.get("th") or "").strip()
        other = next((str(v).strip() for v in raw.values() if v and str(v).strip()), "")
    else:
        other = str(raw).strip() if raw else ""

    slug_val = data.get("slug")
    slug_str = ""
    if isinstance(slug_val, dict):
        slug_str = str(slug_val.get("en") or slug_val.get("pl") or next((str(v) for v in slug_val.values() if v), "")).strip()
    elif slug_val:
        slug_str = str(slug_val).strip()
    slug_title = slug_to_title(slug_str or doc_id)

    plain = ""
    for key in ("name", "propertyName", "projectName", "title"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            plain = value.strip()
            break

    if lang == "en":
        if en and not _looks_like_long_polish_marketing(en):
            return sanitize_marketing_claims(en, mode="title")
        if plain and not _looks_like_long_polish_marketing(plain) and len(plain) <= 80:
            return sanitize_marketing_claims(plain, mode="title")
        # Prefer readable slug over long PL marketing title
        if slug_title and (not pl or _looks_like_long_polish_marketing(pl) or len(pl) > 60):
            return sanitize_marketing_claims(slug_title, mode="title")
        if pl and not _looks_like_long_polish_marketing(pl):
            return sanitize_marketing_claims(pl, mode="title")
        if other and not _looks_like_long_polish_marketing(other):
            return sanitize_marketing_claims(other, mode="title")
        return sanitize_marketing_claims(slug_title or other or doc_id, mode="title")

    if lang == "pl":
        pick = pl or en or other or slug_title or doc_id
    elif lang == "th":
        pick = th or en or other or slug_title or doc_id
    else:
        pick = en or pl or other or slug_title or doc_id
    return sanitize_marketing_claims(pick, mode="title")


def _shorten_location_raw(
    *,
    district: Optional[str] = None,
    city: Optional[str] = None,
    province: Optional[str] = None,
    location: Optional[str] = None,
    address: Optional[str] = None,
) -> str:
    """
    Prefer a short place label like 'Bang Por, Koh Samui' over mixed Google-style addresses.
    """
    parts: List[str] = []
    for p in (district, city):
        s = str(p or "").strip()
        if s and s.lower() not in {x.lower() for x in parts}:
            # Skip if it looks like a full street address (digits + long)
            if re.search(r"\d{2,}", s) and len(s) > 40:
                continue
            if len(s) > 60:
                continue
            parts.append(s)
    if not parts and province:
        parts.append(str(province).strip())
    if parts:
        # If only district and province is Thailand-ish, append island/city when known from location blob
        blob = " ".join(str(x or "") for x in (location, address, province)).lower()
        extras = []
        for island in ("koh samui", "samui", "phuket", "koh phangan", "phangan", "pattaya", "bangkok"):
            if island in blob:
                label = {
                    "koh samui": "Koh Samui",
                    "samui": "Koh Samui",
                    "phuket": "Phuket",
                    "koh phangan": "Koh Phangan",
                    "phangan": "Koh Phangan",
                    "pattaya": "Pattaya",
                    "bangkok": "Bangkok",
                }[island]
                if label.lower() not in {x.lower() for x in parts}:
                    extras.append(label)
                break
        out = ", ".join(parts[:2] + extras[:1])
        return out

    # Fallback: take first short comma segment from location
    loc = str(location or "").strip()
    if loc:
        segs = [s.strip() for s in loc.split(",") if s.strip()]
        short = []
        for s in segs:
            if re.search(r"\d{3,}", s) and len(s) > 25:
                continue
            if len(s) > 50:
                continue
            short.append(s)
            if len(short) >= 2:
                break
        if short:
            return ", ".join(short)
    return loc or (str(province).strip() if province else "Thailand")


def shorten_location(
    *,
    district: Optional[str] = None,
    city: Optional[str] = None,
    province: Optional[str] = None,
    location: Optional[str] = None,
    address: Optional[str] = None,
) -> str:
    """Short place label for display ('Bang Por, Koh Samui'), with lower-case slugs capitalised."""
    return display_location(_shorten_location_raw(
        district=district, city=city, province=province, location=location, address=address,
    ))


def extract_ownership_from_property(prop: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return ownership/tenure text only when present in data or clearly stated in description."""
    if not prop or not isinstance(prop, dict):
        return None
    raw = prop.get("raw_firestore_data") if isinstance(prop.get("raw_firestore_data"), dict) else {}

    for src in (prop, raw):
        for key in _OWNERSHIP_FIELD_KEYS:
            val = src.get(key)
            if isinstance(val, dict):
                text = val.get("en") or val.get("pl") or next((str(v) for v in val.values() if v), "")
            else:
                text = str(val).strip() if val not in (None, "", [], False) else ""
            if text and text.lower() not in ("n/a", "na", "none", "unknown"):
                # Reject our own former hardcoded placeholder if somehow stored
                if text.strip().lower() == "foreign freehold / leasehold available":
                    continue
                return sanitize_marketing_claims(text.strip(), mode="fact")

    desc = str(prop.get("description") or raw.get("description") or "")
    if isinstance(prop.get("description"), dict):
        d = prop["description"]
        desc = str(d.get("en") or d.get("pl") or "")
    for pat in _OWNERSHIP_DESC_PATTERNS:
        m = pat.search(desc)
        if m:
            # Prefer a slightly wider snippet when lease+freehold option appears
            snippet = m.group(0).strip()
            # Expand around match for "30-year lease with freehold option"
            window = desc[max(0, m.start() - 20) : m.end() + 40]
            m2 = re.search(
                r"30[-\s]?year[^.]{0,80}(?:freehold|leasehold)[^.]{0,40}",
                window,
                flags=re.I,
            )
            if m2:
                snippet = m2.group(0).strip(" ,.;")
            return sanitize_marketing_claims(snippet, mode="fact")
    return None


def sanitize_marketing_claims(text: str, mode: str = "description") -> str:
    """
    Strip investment promises / guaranteed returns, or label them.
    mode=title: remove claim phrases entirely from titles.
    mode=description/fact: replace with developer-marketing label or drop sentence.
    """
    if not text:
        return text
    out = str(text)
    for pat in _MARKETING_CLAIM_PATTERNS:
        if mode == "title":
            out = pat.sub("", out)
        else:
            out = pat.sub(DEVELOPER_MARKETING_CLAIM_LABEL, out)
    # Clean leftover punctuation / spaces
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    out = out.strip(" -–,;|")
    return out


def clean_highlights_for_display(
    highlights: Sequence[Any],
    prop: Optional[Dict[str, Any]] = None,
    language: str = "en",
    max_items: int = 5,
) -> List[str]:
    """
    Map known amenity tags/slugs to canonical labels; hide unknown raw transliterated slugs.
    Also strip marketing claims from highlight strings.
    """
    out: List[str] = []
    facts = collect_property_amenities(prop) if prop else {}
    # Prefer canonical amenity labels from facts first
    for key in ("pool_private", "pool", "beach_access", "parking", "gym", "spa", "garden", "security", "lounge", "running_track", "kids_zone", "restaurant", "sea_view"):
        fact = facts.get(key)
        if fact and fact.present:
            label = AMENITY_LABELS_EN.get(key, key)
            if key == "pool" and facts.get("pool_private") and facts["pool_private"].present:
                continue  # private already covered
            if key == "pool" and not (facts.get("pool_private") and facts["pool_private"].present):
                # shared / unspecified pool
                if "private" not in " ".join(fact.matched_tokens).lower():
                    label = "Shared pool" if any(
                        "shared" in t.lower() or "communal" in t.lower() for t in fact.matched_tokens
                    ) else "Pool"
            if label not in out:
                out.append(label)
            if len(out) >= max_items:
                return out

    for h in highlights or []:
        s = str(h or "").strip()
        if not s:
            continue
        s = sanitize_marketing_claims(s, mode="title")
        if not s:
            continue
        canon = map_token_to_canonical(s)
        if canon:
            label = AMENITY_LABELS_EN.get(canon, canon)
            if canon == "pool_private":
                label = "Private pool"
            elif canon == "pool":
                low = s.lower()
                if "private" in low or "prywat" in low:
                    label = "Private pool"
                elif "shared" in low or "communal" in low:
                    label = "Shared pool"
                else:
                    label = "Pool"
            if label not in out:
                out.append(label)
            continue
        # Hide raw transliterated slugs (hyphenated latin without spaces, no digits needed)
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", s.lower()) and not re.search(r"[ąćęłńóśźż]", s, re.I):
            continue
        if _looks_like_long_polish_marketing(s):
            continue
        if s not in out:
            out.append(s)
        if len(out) >= max_items:
            break
    return out[:max_items]


def pool_display_label(prop: Optional[Dict[str, Any]], language: str = "en") -> Optional[str]:
    """Return Private pool / Shared pool / Pool, or None if no pool data."""
    from app.services.conversation_controls import normalize_pool_label
    conflict = normalize_pool_label(prop if isinstance(prop, dict) else None)
    if conflict:
        return conflict
    facts = collect_property_amenities(prop or {})
    if facts.get("pool_private") and facts["pool_private"].present:
        if language == "pl":
            return "Prywatny basen"
        if language == "th":
            return "สระส่วนตัว"
        return "Private pool"
    if facts.get("pool") and facts["pool"].present:
        tokens = " ".join(facts["pool"].matched_tokens).lower()
        shared = any(k in tokens for k in ("shared", "communal", "common", "wspólny", "ส่วนกลาง"))
        # Condos without private marker → do not claim Private pool
        ptype = str((prop or {}).get("property_type") or "").lower()
        if shared or ("condo" in ptype or "apart" in ptype):
            if language == "pl":
                return "Basen wspólny" if shared or "condo" in ptype else "Basen"
            if language == "th":
                return "สระส่วนกลาง" if shared or "condo" in ptype else "สระว่ายน้ำ"
            return "Shared pool" if (shared or "condo" in ptype) else "Pool"
        if language == "pl":
            return "Basen"
        if language == "th":
            return "สระว่ายน้ำ"
        return "Pool"
    return None


def apply_presentation_to_card(
    card: Dict[str, Any],
    *,
    language: str = "en",
    raw_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Mutate/return card with cleaned name, location, highlights, ownership, description."""
    if not isinstance(card, dict):
        return card
    data = raw_data or card.get("raw_firestore_data") or card
    doc_id = str(card.get("property_id") or "")
    card["name"] = resolve_display_name(data if isinstance(data, dict) else card, doc_id=doc_id, language=language)
    card["location"] = shorten_location(
        district=card.get("district"),
        city=card.get("district") or (data.get("cityId") if isinstance(data, dict) else None),
        province=card.get("province"),
        location=card.get("location"),
        address=card.get("address"),
    )
    ownership = extract_ownership_from_property(card)
    if ownership:
        card["ownership"] = ownership
        card["ownership_type"] = ownership
    else:
        card.pop("ownership", None)
        # Do not leave hardcoded ownership_type placeholders
        ot = card.get("ownership_type")
        if ot and str(ot).strip().lower() in (
            "foreign freehold / leasehold available",
            "freehold / leasehold",
        ):
            card.pop("ownership_type", None)

    if card.get("description"):
        card["description"] = sanitize_marketing_claims(str(card["description"]), mode="description")
    card["highlights"] = clean_highlights_for_display(
        card.get("highlights") or [],
        prop=card,
        language=language,
    )
    facts = collect_property_amenities(card)
    pool_lab = pool_display_label(card, language=language)
    card["has_pool"] = bool(facts["pool"].present or facts["pool_private"].present)
    card["has_private_pool"] = bool(facts["pool_private"].present)
    if pool_lab:
        card["pool_label"] = pool_lab
    return card
