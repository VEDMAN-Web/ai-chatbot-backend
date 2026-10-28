"""
Canonical amenity map + helpers for cards, detail answers, and search filters.

Sources consulted (never invent): tags, amenities, highlights, features, specs,
has_* flags, and property description text. Description matches are kept
traceable via AmenityFact.sources.
"""

from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Canonical key -> aliases (EN / PL / TH / RU transliterated slugs / common variants)
CANONICAL_AMENITY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "gym": (
        "gym", "fitness", "fitness-center", "fitness centre", "fitness-centre",
        "personal gym", "private gym", "workout", "siłownia", "silownia",
        "фитнес", "fitnes", "trenazhernyy-zal", "trenazhernyj-zal",
        "ฟิตเนส", "ยิม", "ห้องออกกำลังกาย",
    ),
    "pool": (
        "pool", "swimming-pool", "swimming pool", "swimmingpool", "infinity-pool",
        "infinity pool", "jacuzzi", "water-park", "waterpark", "basen",
        "basseyn", "plavatelnyy-basseyn", "plavatelnyj-basseyn",
        "สระ", "สระว่ายน้ำ", "shared pool", "communal pool",
        "common pool", "basen wspólny", "สระส่วนกลาง",
    ),
    "pool_private": (
        "private pool", "private-pool", "private_pool", "prywatny basen", "สระส่วนตัว",
    ),
    "spa": (
        "spa", "sauna", "wellness", "massage", "hammam", "spa-center",
        "spa-центр", "спа", "สปา", "ซาวน่า",
    ),
    "parking": (
        "parking", "garage", "car-park", "carpark", "parking-space",
        "parking spaces", "covered parking", "garaż", "garaz", "parkingowy",
        "parkovka", "podzemnaya-parkovka", "podzemnaja-parkovka",
        "ที่จอด", "ที่จอดรถ", "โรงจอดรถ",
    ),
    "restaurant": (
        "restaurant", "cafe", "café", "dining", "ресторан", "restoran",
        "ресторан", "кафе", "kafe", "restauracja", "kawiarnia",
        "ร้านอาหาร", "คาเฟ่",
    ),
    "kids_zone": (
        "kids", "kids-zone", "kids zone", "playground", "children", "kids club",
        "детская", "detskaya-ploshchadka", "detskaja-ploshchadka",
        "plac zabaw", "dzieci", "โซนเด็ก", "สนามเด็กเล่น",
    ),
    "lounge": (
        "lounge", "lobby", "co-working", "coworking", "laundzh", "lounge-area",
        "лаунж", "холл", "salon", "เลานจ์",
    ),
    "running_track": (
        "running track", "running-track", "jogging", "jog track",
        "begovaya-dorozhka", "begovaja-dorozhka", "bieżnia", "bieznia",
        "ลู่วิ่ง",
    ),
    "garden": (
        "garden", "gardens", "park", "landscaped", "sad", "сад", "ogród", "ogrod",
        "สวน",
    ),
    "security": (
        "security", "24h security", "cctv", "concierge", "guard",
        "ochrona", "bezpečnosť", "охрана", "okhrana", "รปภ", "รักษาความปลอดภัย",
    ),
    "beach_access": (
        "beach", "beach-access", "beach access", "beachfront", "beach-front",
        "sea access", "plaz", "plaża", "plaza", "пляж", "plyazh",
        "ชายหาด", "ใกล้ชายหาด", "ติดทะเล",
    ),
    "sea_view": (
        "sea-view", "sea view", "seaview", "ocean-view", "ocean view",
        "widok na morze", "вид на море", "vid-na-more", "วิวทะเล",
    ),
}

# Display labels (EN default; used on cards)
AMENITY_LABELS_EN: Dict[str, str] = {
    "gym": "Gym",
    "pool": "Pool",
    "pool_private": "Private pool",
    "spa": "Spa",
    "parking": "Parking",
    "restaurant": "Restaurant",
    "kids_zone": "Kids zone",
    "lounge": "Lounge",
    "running_track": "Running track",
    "garden": "Garden",
    "security": "Security",
    "beach_access": "Beach access",
    "sea_view": "Sea view",
}

# User-message request phrases -> canonical
_REQUEST_PHRASES: List[Tuple[str, str]] = [
    (r"\b(?:personal\s+)?gym\b|\bfitness(?:\s+center|\s+centre)?\b|\bsiłowni[ae]?\b|\bฟิตเนส\b|\bยิม\b", "gym"),
    (r"\b(?:private\s+)?pool\b|\bswimming(?:\s+pool)?\b|\bbasen\b|\bสระ(?:ว่ายน้ำ)?\b", "pool"),
    (r"\bprivate\s+pool\b|\bprywatn\w*\s+basen\b|\bสระส่วนตัว\b", "pool_private"),
    (r"\bspa\b|\bsauna\b|\bwellness\b|\bสปา\b", "spa"),
    (r"\bparking\b|\bgarage\b|\bgaraż\b|\bgaraz\b|\bที่จอด\b", "parking"),
    (r"\brestaurant\b|\bcafe\b|\bcafé\b|\brestauracj\w*\b|\bร้านอาหาร\b", "restaurant"),
    (r"\bkids(?:\s+zone|\s+club)?\b|\bplayground\b|\bplac\s+zabaw\b|\bโซนเด็ก\b", "kids_zone"),
    (r"\blounge\b|\bcoworking\b|\bco-working\b|\bเลานจ์\b", "lounge"),
    (r"\brunning\s+track\b|\bjogging\b|\bbieżni[ae]?\b|\bลู่วิ่ง\b", "running_track"),
    (r"\bgarden\b|\bogród\b|\bogrod\b|\bสวน\b", "garden"),
    (r"\bsecurity\b|\bcctv\b|\bconcierge\b|\bochrona\b|\bรปภ\b", "security"),
    (r"\bbeach(?:\s*access|\s*front)?\b|\bbeachfront\b|\bpla[zż][ay]?\b|\bชายหาด\b|\bติดทะเล\b", "beach_access"),
    (r"\bsea\s*view\b|\bocean\s*view\b|\bwidok\s+na\s+morze\b|\bวิวทะเล\b", "sea_view"),
]


@dataclass
class AmenityFact:
    canonical: str
    present: bool = False
    sources: List[str] = field(default_factory=list)
    matched_tokens: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical": self.canonical,
            "present": self.present,
            "sources": list(self.sources),
            "matched_tokens": list(self.matched_tokens),
        }


@lru_cache(maxsize=100_000)
def _norm_token(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.replace("_", "-").replace("/", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _flatten_strings(value: Any) -> List[str]:
    out: List[str] = []
    if value is None:
        return out
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_strings(v))
        return out
    if isinstance(value, (list, tuple, set)):
        for v in value:
            out.extend(_flatten_strings(v))
        return out
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        return [str(value)]
    s = str(value).strip()
    if s:
        out.append(s)
    return out


@lru_cache(maxsize=100_000)
def map_token_to_canonical(token: str) -> Optional[str]:
    """Map a single tag/slug/phrase to a canonical amenity key.

    Longer aliases win so e.g. \"private-pool\" maps to pool_private, not pool.
    """
    n = _norm_token(token)
    if not n:
        return None
    n_compact = n.replace(" ", "-").replace("  ", "-")
    candidates: List[Tuple[int, str]] = []
    for canon, aliases in CANONICAL_AMENITY_ALIASES.items():
        for alias in aliases:
            a = _norm_token(alias)
            a_compact = a.replace(" ", "-")
            if n == a or n_compact == a_compact:
                candidates.append((len(a), canon))
            elif len(a) >= 4 and (a in n or a_compact in n_compact or n in a):
                candidates.append((len(a), canon))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1]


def _alias_present_in_text(text: str, aliases: Sequence[str]) -> Optional[str]:
    low = _norm_token(text)
    if not low:
        return None
    for alias in aliases:
        a = _norm_token(alias)
        if len(a) < 3:
            continue
        # word-ish / slug match
        if a in low or a.replace(" ", "-") in low.replace(" ", "-"):
            return alias
        # (a word-boundary regex check used to follow here; it can only match when `a in low` already did)
    return None


def collect_property_amenities(prop: Optional[Dict[str, Any]]) -> Dict[str, AmenityFact]:
    """
    Collect amenity facts from ALL available fields on a property/card dict
    (including nested raw_firestore_data when present).
    """
    facts: Dict[str, AmenityFact] = {
        k: AmenityFact(canonical=k) for k in CANONICAL_AMENITY_ALIASES
    }
    if not prop or not isinstance(prop, dict):
        return facts

    raw = prop.get("raw_firestore_data") if isinstance(prop.get("raw_firestore_data"), dict) else {}

    def _mark(canon: str, source: str, token: str) -> None:
        fact = facts[canon]
        fact.present = True
        if source not in fact.sources:
            fact.sources.append(source)
        tok = str(token).strip()
        if tok and tok not in fact.matched_tokens:
            fact.matched_tokens.append(tok)

    # has_* flags used on cards
    flag_map = {
        "has_pool": "pool",
        "beach_access": "beach_access",
        "has_parking": "parking",
        "sea_view": "sea_view",
        "has_gym": "gym",
        "has_spa": "spa",
    }
    for flag, canon in flag_map.items():
        for src in (prop, raw):
            if src.get(flag) is True:
                _mark(canon, flag, flag)

    # List-like fields
    list_fields = (
        ("highlights", prop.get("highlights") or raw.get("highlights")),
        ("amenities", prop.get("amenities") or raw.get("amenities")),
        ("tags", prop.get("tags") or raw.get("tags")),
        ("features", prop.get("features") or raw.get("features")),
        ("specs", prop.get("specs") or raw.get("specs") or prop.get("specifications") or raw.get("specifications")),
    )
    for source_name, val in list_fields:
        for item in _flatten_strings(val):
            canon = map_token_to_canonical(item)
            if canon:
                _mark(canon, source_name, item)

    # Scalar parking fields
    for key in ("parking", "parking_spaces", "parkingSpaces"):
        for src, sname in ((prop, key), (raw, f"raw.{key}")):
            v = src.get(key) if isinstance(src, dict) else None
            if v in (None, "", [], False):
                continue
            if v is True or (isinstance(v, (int, float)) and v > 0):
                _mark("parking", sname, str(v))
            elif isinstance(v, str) and v.strip():
                _mark("parking", sname, v)
            elif isinstance(v, list) and v:
                _mark("parking", sname, ",".join(str(x) for x in v[:3]))

    # Description text (traceable source)
    desc_parts = []
    for key in ("description", "shortDescription", "_descriptionRu", "_shortDescriptionRu"):
        for src in (prop, raw):
            desc_parts.extend(_flatten_strings(src.get(key)))
    desc_text = " ".join(desc_parts)
    if desc_text:
        for canon, aliases in CANONICAL_AMENITY_ALIASES.items():
            hit = _alias_present_in_text(desc_text, aliases)
            if hit:
                _mark(canon, "description", hit)

    # Pool private implies pool
    if facts["pool_private"].present and not facts["pool"].present:
        facts["pool"].present = True
        facts["pool"].sources.extend(
            s for s in facts["pool_private"].sources if s not in facts["pool"].sources
        )

    return facts


def property_has_amenity(prop: Optional[Dict[str, Any]], canonical: str) -> bool:
    facts = collect_property_amenities(prop)
    fact = facts.get(canonical)
    if fact and fact.present:
        return True
    # pool_private request also satisfied by pool when asking generically handled by caller
    if canonical == "pool" and facts.get("pool_private") and facts["pool_private"].present:
        return True
    return False


def amenity_status(prop: Optional[Dict[str, Any]], canonical: str) -> AmenityFact:
    facts = collect_property_amenities(prop)
    if canonical in facts:
        return facts[canonical]
    return AmenityFact(canonical=canonical, present=False)


def extract_requested_amenities(message: str) -> List[str]:
    """Detect amenity filters from a user message (with gym, has a pool, spa, …)."""
    msg = (message or "").lower()
    if not msg:
        return []
    found: List[str] = []
    # Prefer more specific private-pool before generic pool
    ordered = sorted(_REQUEST_PHRASES, key=lambda x: (0 if x[1] == "pool_private" else 1, -len(x[0])))
    for pat, canon in ordered:
        if re.search(pat, msg, flags=re.IGNORECASE):
            if canon not in found:
                found.append(canon)
    # If both pool and pool_private, keep both (filter uses all)
    return found


def amenity_label(canonical: str, language: str = "en", matched: bool = True) -> str:
    base = AMENITY_LABELS_EN.get(canonical, canonical.replace("_", " ").title())
    lang = (language or "en").lower()
    if matched:
        if lang == "pl":
            return f"{base} ✓"
        if lang == "th":
            return f"{base} ✓"
        return f"{base} ✓"
    # not listed
    if lang == "pl":
        return f"{base} — brak w ofercie"
    if lang == "th":
        return f"{base} — ไม่ระบุ"
    return f"{base} not listed"


def apply_amenity_labels(
    cards: List[Dict[str, Any]],
    required: Sequence[str],
    language: str = "en",
) -> List[Dict[str, Any]]:
    """Annotate cards with amenity_match / amenity_labels for UI honesty."""
    out = []
    for c in cards:
        card = dict(c)
        labels = []
        all_ok = True
        for canon in required:
            ok = property_has_amenity(card, canon)
            if not ok:
                all_ok = False
            labels.append(amenity_label(canon, language=language, matched=ok))
        card["amenity_match"] = all_ok
        card["amenity_labels"] = labels
        if labels:
            # Surface first label on highlights for card UIs
            highs = list(card.get("highlights") or [])
            for lab in labels:
                if lab not in highs:
                    highs.insert(0, lab)
            card["highlights"] = highs[:5]
        out.append(card)
    return out


def filter_cards_by_amenities(
    cards: Sequence[Dict[str, Any]],
    required: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split into (matching, non_matching)."""
    matching, non_matching = [], []
    for c in cards:
        if required and all(property_has_amenity(c, a) for a in required):
            matching.append(c)
        else:
            non_matching.append(c)
    return matching, non_matching


def enrich_card_amenity_flags(card: Dict[str, Any]) -> Dict[str, Any]:
    """Set has_pool / beach_access / has_parking from the unified helper."""
    if not isinstance(card, dict):
        return card
    facts = collect_property_amenities(card)
    card["has_pool"] = bool(facts["pool"].present or facts["pool_private"].present)
    card["beach_access"] = bool(facts["beach_access"].present)
    card["has_parking"] = bool(facts["parking"].present)
    card["amenities_canonical"] = [k for k, f in facts.items() if f.present]
    card["amenity_facts"] = {k: f.to_dict() for k, f in facts.items() if f.present}
    return card
