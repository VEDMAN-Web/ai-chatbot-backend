"""
Claude-assisted brochure copy for the user-side single-property PDF only.

Uses BROCHURE_LLM_MODEL (default claude-sonnet-5-5). Does not touch OPENAI_MODEL
or the admin chatbot. Falls back to None on any failure so the deterministic
brochure path remains unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.config import settings
from app.services.brochure_data import BrochureData

logger = logging.getLogger(__name__)

_BANNED_PHRASES = (
    "guaranteed",
    "risk-free",
    "risk free",
    "best price",
    "limited time",
    "only x left",
    "tax-free",
    "tax free",
    "no tax",
    "gwarantowane",
    "bez ryzyka",
    "najlepsza cena",
    "ograniczony czas",
    "bez podatku",
)

_COPY_KEYS = (
    "headline",
    "intro",
    "highlights",
    "location_blurb",
    "lifestyle_blurb",
    "investment_blurb",
    "closing_line",
)

_LIMITS = {
    "headline": 80,
    "intro": 420,
    "highlights_item": 90,
    "highlights_count_min": 3,
    "highlights_count_max": 6,
    "location_blurb": 300,
    "lifestyle_blurb": 300,
    "investment_blurb": 300,
    "closing_line": 120,
}

# In-memory TTL cache: key -> (expires_at_epoch, BrochureCopy)
_COPY_CACHE: Dict[str, Tuple[float, "BrochureCopy"]] = {}


@dataclass
class BrochureCopy:
    headline: str = ""
    intro: str = ""
    highlights: List[str] = field(default_factory=list)
    location_blurb: str = ""
    lifestyle_blurb: str = ""
    investment_blurb: str = ""
    closing_line: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_copy_input(data: BrochureData) -> Dict[str, Any]:
    """
    Whitelist of verified public listing fields only.
    Never includes emails, phones, internal notes, developer contact, or raw DB ids.
    """
    nearby: List[Dict[str, str]] = []
    for p in data.nearby_places or []:
        item: Dict[str, str] = {}
        if p.name:
            item["name"] = str(p.name)
        if p.category:
            item["category"] = str(p.category)
        if p.distance_text:
            item["distance"] = str(p.distance_text)
        if item:
            nearby.append(item)

    units: List[Dict[str, Any]] = []
    for u in data.unit_types or []:
        item: Dict[str, Any] = {}
        if u.name:
            item["name"] = str(u.name)
        if u.size_text:
            item["size"] = str(u.size_text)
        if u.beds is not None:
            item["beds"] = u.beds
        if u.baths is not None:
            item["baths"] = u.baths
        if u.units is not None:
            item["units"] = u.units
        if item:
            units.append(item)

    payload: Dict[str, Any] = {
        "name": data.name or None,
        "location_text": data.location_text or None,
        "district": data.district or None,
        "province": data.province or None,
        "country": data.country or None,
        "property_type": data.property_type or None,
        "status": data.status or None,
        "completion": data.completion or None,
        "bedrooms_display": data.bedrooms_display or None,
        "bathrooms": data.bathrooms if data.bathrooms is not None else None,
        "area_display": data.area_display or None,
        "price_display": data.price_display or None,
        "price_min": data.price_min,
        "price_max": data.price_max,
        "currency": data.currency or None,
        "ownership_type": data.ownership_type or None,
        "amenities": list(data.amenities or []),
        "facilities": list(data.facilities or []),
        "highlights": list(data.highlights or []),
        "nearby_places": nearby,
        "unit_types": units,
        "description": data.description or None,
        "units_count": data.units,
        "total_floors": data.total_floors,
        "parking": data.parking or None,
        "developer_name": data.developer_name or None,
        "investment_info": data.investment_info or None,
    }
    # Drop empties for a compact prompt
    return {k: v for k, v in payload.items() if v not in (None, "", [], {})}


def _input_hash(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _cache_key(property_id: Optional[str], language: str, payload: Dict[str, Any]) -> str:
    pid = str(property_id or "unknown").strip().lower()
    return f"{pid}|{(language or 'en').lower()}|{_input_hash(payload)}"


def clear_copy_cache() -> None:
    _COPY_CACHE.clear()


def collect_allowed_tokens(payload: Dict[str, Any]) -> Set[str]:
    """
    Number / currency tokens allowed in generated copy (from verified input only).
    Includes formatted forms like 30000000, 30,000,000 and 30M.
    """
    tokens: Set[str] = set()

    def _add_number(n: float) -> None:
        if n != n:  # NaN
            return
        as_int = int(n) if abs(n - int(n)) < 1e-9 else None
        if as_int is not None:
            s = str(as_int)
            tokens.add(s)
            tokens.add(f"{as_int:,}")
            tokens.add(f"{as_int:,}".replace(",", " "))
            if as_int >= 1_000_000:
                mill = as_int / 1_000_000
                tokens.add(f"{mill:g}m")
                tokens.add(f"{mill:g}M")
                if mill == int(mill):
                    tokens.add(f"{int(mill)}m")
                    tokens.add(f"{int(mill)}M")
        else:
            tokens.add(f"{n:g}")
            tokens.add(f"{n:.2f}".rstrip("0").rstrip("."))

    def _walk(obj: Any) -> None:
        if obj is None:
            return
        if isinstance(obj, bool):
            return
        if isinstance(obj, (int, float)):
            _add_number(float(obj))
            return
        if isinstance(obj, str):
            text = obj.strip()
            if not text:
                return
            # percentages
            for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*%", text):
                tokens.add(m.group(0).replace(" ", "").lower())
                tokens.add(m.group(1).replace(",", ".") + "%")
            # millions
            for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*[Mm](?:illion)?\b", text):
                try:
                    v = float(m.group(1).replace(",", ""))
                    _add_number(v * 1_000_000)
                    tokens.add(f"{v:g}m")
                    tokens.add(f"{v:g}M")
                except ValueError:
                    pass
            # plain numbers / prices
            for m in re.finditer(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", text):
                raw = m.group(0)
                try:
                    _add_number(float(raw.replace(",", "")))
                except ValueError:
                    pass
            return
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
            return
        if isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v)

    _walk(payload)
    return {t.lower() if isinstance(t, str) else t for t in tokens}


def _extract_claim_numbers(text: str) -> List[str]:
    """Numeric / % / currency-like tokens claimed in generated text."""
    found: List[str] = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*%", text):
        found.append(m.group(0).replace(" ", "").lower())
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*[Mm](?:illion)?\b", text):
        found.append(m.group(0).replace(" ", "").lower())
        try:
            v = float(m.group(1).replace(",", ""))
            found.append(str(int(v * 1_000_000)))
        except ValueError:
            pass
    for m in re.finditer(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", text):
        raw = m.group(0)
        found.append(raw.lower())
        found.append(raw.replace(",", "").lower())
    return found


def _numbers_supported(text: str, allowed: Set[str]) -> bool:
    if not text:
        return True
    allowed_l = {a.lower() for a in allowed}
    for tok in _extract_claim_numbers(text):
        t = tok.lower().replace(" ", "")
        if t in allowed_l:
            continue
        # soft match million forms
        m = re.match(r"^(\d+(?:\.\d+)?)m$", t)
        if m:
            try:
                approx = str(int(float(m.group(1)) * 1_000_000))
                if approx in allowed_l or approx in {a for a in allowed_l if a.isdigit()}:
                    continue
            except ValueError:
                pass
        digits = re.sub(r"[^\d]", "", t)
        if digits and digits in allowed_l:
            continue
        if digits and any(digits == re.sub(r"[^\d]", "", a) for a in allowed_l):
            continue
        # Ignore lone year-like 4-digit if in allowed as completion
        if len(digits) == 4 and digits in allowed_l:
            continue
        return False
    return True


def _has_banned_phrase(text: str) -> bool:
    low = (text or "").lower()
    for phrase in _BANNED_PHRASES:
        if phrase in low:
            # "only X left" pattern
            if phrase == "only x left":
                if re.search(r"\bonly\s+\d+\s+left\b", low):
                    return True
                continue
            return True
    return False


def _mentions_other_property(text: str, own_name: Optional[str]) -> bool:
    if not text:
        return False
    try:
        from app.services.property_name_index import get_property_name_index, match_property_name
    except Exception:
        return False
    own = (own_name or "").strip().lower()
    # Title-Case multi-word phrases that look like project names
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9\-]+){1,5})\b", text):
        name = m.group(1).strip()
        low = name.lower()
        if own and (low == own or low in own or own in low):
            continue
        try:
            mr = match_property_name(name)
            if mr and getattr(mr, "confident", None) is not None:
                conf_name = str(
                    getattr(mr.confident, "name", None)
                    or getattr(mr.confident, "property_name", None)
                    or ""
                ).lower()
                if conf_name and own and (conf_name == own or own in conf_name or conf_name in own):
                    continue
                if conf_name and conf_name != own:
                    return True
        except Exception:
            continue
    # Also scan index for longer names contained in text
    try:
        idx = get_property_name_index()
        names = getattr(idx, "names", None) or getattr(idx, "_by_norm", None) or {}
        if isinstance(names, dict):
            iterable = names.keys()
        else:
            iterable = list(names) if names else []
        text_l = text.lower()
        for n in iterable:
            nl = str(n).lower().strip()
            if len(nl) < 5:
                continue
            if own and (nl == own or nl in own or own in nl):
                continue
            if nl in text_l:
                return True
    except Exception:
        pass
    return False


def _strip_unsupported_sentences(text: str, allowed: Set[str], own_name: Optional[str]) -> str:
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    kept: List[str] = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if _has_banned_phrase(p):
            continue
        if not _numbers_supported(p, allowed):
            continue
        if _mentions_other_property(p, own_name):
            continue
        kept.append(p)
    return " ".join(kept).strip()


def _validate_copy(
    raw: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    has_investment: bool,
) -> Optional[BrochureCopy]:
    if not isinstance(raw, dict):
        return None
    allowed = collect_allowed_tokens(payload)
    own_name = str(payload.get("name") or "")

    def _str_field(key: str, limit: int) -> str:
        val = raw.get(key, "")
        if val is None:
            return ""
        if not isinstance(val, str):
            val = str(val)
        val = val.strip()
        if len(val) > limit:
            val = val[:limit].rstrip()
        return val

    headline = _str_field("headline", _LIMITS["headline"])
    intro = _str_field("intro", _LIMITS["intro"])
    location_blurb = _str_field("location_blurb", _LIMITS["location_blurb"])
    lifestyle_blurb = _str_field("lifestyle_blurb", _LIMITS["lifestyle_blurb"])
    investment_blurb = _str_field("investment_blurb", _LIMITS["investment_blurb"])
    closing_line = _str_field("closing_line", _LIMITS["closing_line"])

    hl_raw = raw.get("highlights") or []
    if not isinstance(hl_raw, list):
        hl_raw = []
    highlights: List[str] = []
    for item in hl_raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        if len(s) > _LIMITS["highlights_item"]:
            s = s[: _LIMITS["highlights_item"]].rstrip()
        if _has_banned_phrase(s):
            continue
        if not _numbers_supported(s, allowed):
            continue
        if _mentions_other_property(s, own_name):
            continue
        highlights.append(s)
    highlights = highlights[: _LIMITS["highlights_count_max"]]

    # Clean prose fields
    headline = _strip_unsupported_sentences(headline, allowed, own_name) or (
        headline if headline and _numbers_supported(headline, allowed) and not _has_banned_phrase(headline) and not _mentions_other_property(headline, own_name) else ""
    )
    # Headline is often without sentence punctuation — re-check whole string
    if headline and (
        _has_banned_phrase(headline)
        or not _numbers_supported(headline, allowed)
        or _mentions_other_property(headline, own_name)
    ):
        headline = ""

    intro = _strip_unsupported_sentences(intro, allowed, own_name)
    location_blurb = _strip_unsupported_sentences(location_blurb, allowed, own_name)
    lifestyle_blurb = _strip_unsupported_sentences(lifestyle_blurb, allowed, own_name)
    if has_investment:
        investment_blurb = _strip_unsupported_sentences(investment_blurb, allowed, own_name)
    else:
        investment_blurb = ""
    closing_line = _strip_unsupported_sentences(closing_line, allowed, own_name)
    if closing_line and (
        _has_banned_phrase(closing_line)
        or not _numbers_supported(closing_line, allowed)
        or _mentions_other_property(closing_line, own_name)
    ):
        # closing may be a short line without period
        if not _numbers_supported(closing_line, allowed) or _has_banned_phrase(closing_line):
            closing_line = ""

    if not headline and not intro:
        return None
    if len(highlights) < _LIMITS["highlights_count_min"]:
        # keep whatever survived; do not fail whole copy solely for highlight count
        pass

    return BrochureCopy(
        headline=headline,
        intro=intro,
        highlights=highlights,
        location_blurb=location_blurb,
        lifestyle_blurb=lifestyle_blurb,
        investment_blurb=investment_blurb,
        closing_line=closing_line,
    )


def _system_prompt(language: str) -> str:
    lang = (language or "en").lower()
    lang_name = {"en": "English", "pl": "Polish", "th": "Thai"}.get(lang, "English")
    return (
        f"You write premium real-estate brochure copy in {lang_name}. "
        "Use ONLY facts present in the user JSON. Never invent or change prices, areas, "
        "bedroom counts, dates, ROI/yield figures, ownership rules, distances, awards or "
        "developer claims. Never mention a number that is not in the JSON. If a field is "
        "missing, leave that sentence out instead of guessing. No legal or tax promises. "
        "No comparisons with other properties. No urgency or scarcity claims unless present "
        "in the data. Neutral professional tone. Output JSON only with keys: "
        "headline, intro, highlights, location_blurb, lifestyle_blurb, investment_blurb, "
        "closing_line. highlights must be a JSON array of 3-6 short strings. "
        "investment_blurb must be an empty string when investment_info is absent."
    )


def _parse_model_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def _safe_anthropic_create(client: Any, **kwargs: Any) -> Any:
    """Same pattern as admin: strip temperature if the model rejects it."""
    call_kwargs = dict(kwargs)
    try:
        return client.messages.create(**call_kwargs)
    except Exception as te:
        if "temperature" in str(te).lower():
            call_kwargs.pop("temperature", None)
            return client.messages.create(**call_kwargs)
        raise


def _extract_text(response: Any) -> str:
    if not response or not getattr(response, "content", None):
        return ""
    parts = []
    for b in response.content:
        if getattr(b, "type", "") == "text" and hasattr(b, "text"):
            parts.append(b.text)
    return "".join(parts).strip()


def _call_sonnet(payload: Dict[str, Any], language: str) -> Optional[Dict[str, Any]]:
    api_key = (getattr(settings, "ANTHROPIC_API_KEY", None) or "").strip()
    if not api_key:
        return None
    model = str(getattr(settings, "BROCHURE_LLM_MODEL", "claude-sonnet-5-5") or "claude-sonnet-5-5")
    max_tokens = int(getattr(settings, "BROCHURE_LLM_MAX_TOKENS", 1200) or 1200)
    timeout = float(getattr(settings, "BROCHURE_LLM_TIMEOUT", 25) or 25)

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    user_content = (
        "Verified listing JSON (only source of facts):\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
        + "\n\nReturn brochure copy JSON now."
    )
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        temperature=0.3,
        system=_system_prompt(language),
        messages=[{"role": "user", "content": user_content}],
    )

    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            env = str(getattr(settings, "ENVIRONMENT", "development") or "").lower()
            if env != "production":
                logger.debug("brochure_copy prompt_chars=%s model=%s", len(user_content), model)
            resp = _safe_anthropic_create(client, **kwargs)
            text = _extract_text(resp)
            if env != "production":
                logger.debug("brochure_copy response_chars=%s", len(text or ""))
            return _parse_model_json(text)
        except Exception as e:
            last_err = e
            err_l = str(e).lower()
            # Retry only on connection errors — not timeouts / validation
            is_conn = any(
                k in err_l
                for k in ("connection", "connect", "network", "temporarily unavailable", "broken pipe")
            )
            is_timeout = "timeout" in err_l or "timed out" in err_l
            if attempt == 0 and is_conn and not is_timeout:
                continue
            raise
    if last_err:
        raise last_err
    return None


def write_brochure_copy(data: BrochureData, language: str) -> Optional[BrochureCopy]:
    """
    Ask Claude for brochure prose. Returns None to keep the deterministic brochure.
    """
    if not bool(getattr(settings, "BROCHURE_LLM_ENABLED", True)):
        return None
    if not (getattr(settings, "ANTHROPIC_API_KEY", None) or "").strip():
        return None

    lang = (language or data.language or "en").lower()
    if lang not in ("en", "pl", "th"):
        lang = "en"

    payload = build_copy_input(data)
    if not payload.get("name") and not payload.get("description"):
        return None

    key = _cache_key(data.property_id, lang, payload)
    now = time.time()
    cached = _COPY_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    has_investment = bool(payload.get("investment_info"))
    try:
        raw = _call_sonnet(payload, lang)
    except Exception as e:
        logger.info("brochure_copy sonnet_error type=%s", type(e).__name__)
        return None

    if not raw:
        return None

    copy = _validate_copy(raw, payload, has_investment=has_investment)
    if copy is None:
        return None

    ttl_h = float(getattr(settings, "BROCHURE_LLM_CACHE_TTL_HOURS", 24) or 24)
    _COPY_CACHE[key] = (now + ttl_h * 3600.0, copy)
    # Bound cache size
    if len(_COPY_CACHE) > 500:
        expired = [k for k, (exp, _) in _COPY_CACHE.items() if exp <= now]
        for k in expired:
            _COPY_CACHE.pop(k, None)
    return copy


def ai_footer_note(language: str) -> str:
    lang = (language or "en").lower()
    if lang == "pl":
        return "Opisy przygotowane z pomocą AI na podstawie zweryfikowanych danych oferty."
    if lang == "th":
        return "คำอธิบายจัดทำด้วยความช่วยเหลือของ AI จากข้อมูลประกาศที่ยืนยันแล้ว"
    return "Descriptions prepared with AI assistance from verified listing data."
