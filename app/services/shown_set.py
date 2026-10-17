"""Deterministic answers to questions about the listings currently on screen
("which one is cheapest?", "which has a pool?" / "który jest tańszy?").

No LLM: the answer is computed from the cards the visitor was just shown, so it can never contradict them.
EN + PL only; other languages fall through to the normal flow.
"""
import re
from typing import Any, Dict, List, Optional

from app.core.config import settings

_REF = (
    r"\bwhich\s+(?:one|of\s+(?:these|them|those)|is|are|property|listing)\b"
    r"|(?:który|ktory|która|ktora|które|ktore)\b|\bz\s+nich\b|spośród|posrod"
)
_PLACE = re.compile(
    r"phuket|samui|pattaya|bangkok|krabi|hua\s*hin|phangan|phnom|siem\s*reap|sihanouk|cambodia|kambod|thailand|tajland|vietnam",
    re.I,
)
_CHEAP = re.compile(r"cheap|least\s+expensive|lowest\s+price|most\s+affordable|ta[nń]sz|najta[nń]sz", re.I)
_PRICEY = re.compile(r"most\s+expensive|priciest|highest\s+price|more\s+expensive|dro[zż]sz|najdro[zż]sz", re.I)
_POOL = re.compile(r"\bpool\b|swimming|basen", re.I)
_BEACH = re.compile(r"beach|plaż|plaz", re.I)
_REF_RE = re.compile(_REF, re.I)


def parse_price_thb(price: Any) -> Optional[float]:
    """'from 19.98M THB' / 'from 178,750 USD' -> THB number; None when not a number ('Price on request')."""
    if price is None:
        return None
    m = re.search(r"([\d][\d,\.]*)\s*([MK])?\s*(THB|USD|EUR)?", str(price), re.I)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").upper()
    val *= 1_000_000 if unit == "M" else 1_000 if unit == "K" else 1
    cur = (m.group(3) or "THB").upper()
    if cur == "USD":
        val *= float(getattr(settings, "FX_USD_THB", 35.0))
    elif cur == "EUR":
        val *= float(getattr(settings, "FX_EUR_THB", 38.0))
    return val if val > 0 else None


def last_shown_cards(history: List[Dict[str, Any]], lookback: int = 8) -> List[Dict[str, Any]]:
    """Cards of the latest assistant turn that listed 2+ properties (within the last `lookback` assistant turns)."""
    seen = 0
    for turn in reversed(history or []):
        if turn.get("role") != "assistant":
            continue
        seen += 1
        if seen > lookback:
            break
        cards: List[Dict[str, Any]] = []
        sd = turn.get("structured_data") or {}
        for src in ((sd.get("properties") if isinstance(sd, dict) else None) or [], turn.get("properties") or []):
            for p in src:
                if isinstance(p, dict) and (p.get("property_id") or p.get("id")) and p.get("name"):
                    if p not in cards:
                        cards.append(p)
        if len(cards) >= 2:
            return cards
    return []


def _t(language: str, en: str, pl: str) -> str:
    return pl if language == "pl" else en


def answer_shown_set_question(message: str, cards: List[Dict[str, Any]], language: str = "en") -> Optional[str]:
    """Reply text, or None when the message is not a question about the shown set."""
    if language not in ("en", "pl") or len(cards) < 2:
        return None
    msg = (message or "").strip()
    if not msg or not _REF_RE.search(msg) or len(msg.split()) > 12 or _PLACE.search(msg):
        return None

    priced = [(c, parse_price_thb(c.get("price"))) for c in cards]
    priced = [(c, v) for c, v in priced if v]

    if (_CHEAP.search(msg) or _PRICEY.search(msg)) and len(priced) >= 2:
        cheap = bool(_CHEAP.search(msg)) and not _PRICEY.search(msg)
        priced.sort(key=lambda cv: cv[1], reverse=not cheap)
        (a, _), (b, _) = priced[0], priced[1]
        if cheap:
            txt = _t(language,
                     f"Of the listings above, the lowest starting price is **{a['name']}** ({a.get('price')}), "
                     f"followed by **{b['name']}** ({b.get('price')}).",
                     f"Spośród pokazanych ofert najniższa cena startowa jest w **{a['name']}** ({a.get('price')}), "
                     f"następnie **{b['name']}** ({b.get('price')}).")
        else:
            txt = _t(language,
                     f"Of the listings above, the highest starting price is **{a['name']}** ({a.get('price')}), "
                     f"followed by **{b['name']}** ({b.get('price')}).",
                     f"Spośród pokazanych ofert najwyższa cena startowa jest w **{a['name']}** ({a.get('price')}), "
                     f"następnie **{b['name']}** ({b.get('price')}).")
        skipped = len(cards) - len(priced)
        if skipped:
            txt += _t(language, " (Listings without a published price are not included in this comparison.)",
                      " (Oferty bez opublikowanej ceny nie są uwzględnione w porównaniu.)")
        return txt + _t(language, "\n\nPrices are starting prices per project; units differ in size.",
                        "\n\nCeny są cenami startowymi projektu; jednostki różnią się metrażem.")

    from app.services.amenity_service import property_has_amenity

    for rx, canon, en_what, pl_what in ((_POOL, "pool", "a pool", "basen"), (_BEACH, "beach_access", "beach access", "dostęp do plaży")):
        if rx.search(msg):
            yes = [c["name"] for c in cards if property_has_amenity(c, canon)]
            no = [c["name"] for c in cards if c["name"] not in yes]
            if len(yes) == len(cards):
                return _t(language, f"All of the listings above have {en_what}.", f"Wszystkie pokazane oferty mają {pl_what}.")
            if not yes:
                return _t(language, f"I can't confirm {en_what} for any of the listings above in our data.",
                          f"Nie mogę potwierdzić: {pl_what} - w naszych danych brak tej informacji dla pokazanych ofert.")
            return _t(language,
                      f"{en_what.capitalize()} is confirmed for: {', '.join('**'+n+'**' for n in yes)}. "
                      f"Not confirmed in our data for: {', '.join(no)}.",
                      f"{pl_what.capitalize()} potwierdzono dla: {', '.join('**'+n+'**' for n in yes)}. "
                      f"Brak potwierdzenia w danych dla: {', '.join(no)}.")
    return None
