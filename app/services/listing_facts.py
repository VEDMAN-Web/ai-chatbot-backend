"""
Direct answers to follow-up questions about ONE listing, taken only from its verified record.

No language model is involved: every sentence is a template filled with fields from the record, or an
honest "not in our verified data" pointing to an advisor. `answer_listing_fact` returns None when the
question is not one of the topics below, so the caller keeps its existing behaviour.
English and Polish only; any other language returns None.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]
_MONTHS_PL = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień",
              "wrzesień", "październik", "listopad", "grudzień"]
_STATUS_EN = {"under_construction": "under construction", "completed": "completed", "ready": "completed",
              "pre_sale": "in pre-sale", "off_plan": "off-plan"}
_STATUS_PL = {"under_construction": "w budowie", "completed": "ukończona", "ready": "ukończona",
              "pre_sale": "w przedsprzedaży", "off_plan": "w fazie projektu"}
_PLN_PER_THB = 8.5  # same indicative rate the search budget parser uses (1 PLN = 8.5 THB)

_RE_COMPLETION = re.compile(
    r"when\b.*\b(finish|ready|complete|done|built|handover|deliver)|\bcompletion\b|\bhand-?over\b|"
    r"\b(already|is it|been) (built|finished|completed|ready)\b|\bfinished\b|\bukończ|\boddan|"
    r"\bkiedy\b.*\b(gotow|skończ|oddan|budow)|\btermin\b", re.I)
_RE_UNITS = re.compile(
    r"how many\b.*\b(units?|apartments?|flats?)\b|\b(units?|apartments?)\b.*\b(left|remaining|available|total)\b|"
    r"how many\b.*\b(still )?(available|left|remain)|\bavailable units\b|\bile\b.*\b(apartament|mieszka|jednostek|lokali)|"
    r"\bile\b.*\bdostępn", re.I)
_RE_LAYOUT_WORD = re.compile(r"\bstudios?\b|\b(one|two|three|1|2|3)[ -]?bed(room)?s?\b|\b[123][ -]?br\b|\b(jedno|dwu|trzy)pokojow|\bgarsonier", re.I)
_RE_LAYOUT_ASK = re.compile(r"size|area|sqm|m2|square|big|large|price|cost|how much|how many|available|powierzchni|rozmiar|ile|cena|koszt", re.I)
_RE_RANGE = re.compile(r"\b(cheapest|lowest|minimum|starting)\b.*\b(unit|apartment|option|price)\b|"
                       r"\bmost expensive\b|\bpriciest\b|\bhighest (price|priced)\b|\bprice range\b|\bnajtańsz|\bnajdroższ", re.I)
_RE_BEDROOM_TYPES = re.compile(r"how many bedrooms|which bedrooms|what bedrooms|bedroom (types|options)|ile sypialni", re.I)
_RE_ADDRESS = re.compile(r"\baddress\b|where exactly|exact location|\badres\b|gdzie dokładnie", re.I)
_RE_PER_SQM = re.compile(r"per (square|sq\.?)\s?(m|metre|meter)|per sqm|za metr|za m2|cena za m", re.I)
_RE_CURRENCY = re.compile(r"\b(in|w)\s+(us\s+|u\.s\.\s+)?(euro|eur|dollars?|usd|zloty|zlotych|pln|złotych|złotówk\w*|dolar\w*)\b|"
                          r"\b(euro|eur|usd|pln)\b|\band in (zloty|dollars)\b", re.I)
_RE_OWNERSHIP = re.compile(r"free-?hold|lease-?hold|\bownership\b|who owns|\btitle\b|foreigner|foreign buyer|"
                           r"can i (own|buy)|własno|cudzoziem|dzierżaw", re.I)
_RE_YIELD = re.compile(r"\byield\b|\broi\b|\breturn|guarantee|rental income|zwrot|zysk|gwarant|stopa", re.I)
_RE_ADVICE = re.compile(r"good investment|worth (it|buying)|should i (buy|invest)|recommend|will the price (go|rise)|"
                        r"price (go|rise|increase)|appreciat|opłaca|polecasz|czy warto|wzrośn", re.I)
_RE_PAYMENT = re.compile(r"payment plan|installment|instalment|deposit|down ?payment|mortgage|financ|raty|rat[ay]\b|zaliczk|kredyt", re.I)
_RE_DEVELOPER = re.compile(r"developer|builder|who built|deweloper", re.I)
_RE_NOT_STATED = re.compile(
    r"airport|how far|distance|mall|supermarket|shop|restaurant|hospital|school|nearby|"
    r"\bpets?\b|\bdogs?\b|wi-?fi|internet|furnish|maintenance|sinking|\bfees?\b|\btax(es)?\b|discount|visa|"
    r"which floor|what floor|\bfloor\b(?! plan)|lotnisk|odległ|zwierz|podatk|opłat|rabat|wiz[aey]\b|piętr", re.I)
_RE_AMENITY = {
    "balcony": re.compile(r"balcon|balkon", re.I),
    "security": re.compile(r"security|guard|ochron|monitoring", re.I),
    "garden": re.compile(r"garden|ogr[oó]d", re.I),
    "elevator": re.compile(r"elevator|\blift\b|winda", re.I),
    "sauna": re.compile(r"sauna", re.I),
    "spa": re.compile(r"\bspa\b", re.I),
}


def _raw(card: Dict[str, Any]) -> Dict[str, Any]:
    r = card.get("raw_firestore_data")
    return r if isinstance(r, dict) else {}


def _num(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace(",", "").strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _money(value: float, currency: str) -> str:
    cur = (currency or "THB").upper()
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M {cur}".replace(".00M", "M")
    return f"{value:,.0f} {cur}"


def _fx_thb(currency: str) -> Optional[float]:
    cur = (currency or "THB").upper()
    if cur == "THB":
        return 1.0
    try:
        from app.core.config import settings
        return {"USD": float(settings.FX_USD_THB), "EUR": float(settings.FX_EUR_THB)}.get(cur)
    except Exception:
        return None


def _not_in_data(name: str, what_en: str, what_pl: str, advisor: str, lang: str) -> str:
    if lang == "pl":
        return (f"{what_pl} nie figuruje w zweryfikowanych danych dla **{name}**. "
                f"Nasz doradca ({advisor}) może to potwierdzić — możesz też użyć przycisku „Zapytaj o ofertę”.")
    return (f"{what_en} isn't in our verified data for **{name}**. "
            f"Our advisor ({advisor}) can confirm it for you — you can also use the Inquire button.")


def _layouts(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = raw.get("layouts")
    return [x for x in out if isinstance(x, dict)] if isinstance(out, list) else []


def _layout_label(lay: Dict[str, Any], lang: str) -> str:
    ut = str(lay.get("unitType") or "").lower()
    beds = str(lay.get("bedrooms") or "").strip()
    if ut == "studio":
        return "Studio" if lang != "pl" else "Studio"
    if beds:
        return f"{beds}-bedroom" if lang != "pl" else f"{beds}-sypialniowy"
    return ut.replace("_", " ").title() or "Unit"


def _match_layout(q: str, lays: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ql = q.lower()
    want_studio = bool(re.search(r"studio|garsonier", ql))
    m = re.search(r"\b(one|two|three|1|2|3)[ -]?(bed|br)|\b(jedno|dwu|trzy)pokojow|\b([123])[ -]?sypial", ql)
    want_beds = None
    if m:
        token = next(g for g in m.groups() if g)
        want_beds = {"one": "1", "two": "2", "three": "3", "jedno": "1", "dwu": "2", "trzy": "3"}.get(token, token)
    for lay in lays:
        if want_studio and str(lay.get("unitType") or "").lower() == "studio":
            return lay
        if want_beds and str(lay.get("bedrooms") or "").strip() == want_beds:
            return lay
    return None


def _fmt_date(raw_date: Any, lang: str) -> Optional[str]:
    m = re.match(r"^(\d{4})-(\d{2})", str(raw_date or "").strip())
    if not m:
        return None
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return None
    names = _MONTHS_PL if lang == "pl" else _MONTHS_EN
    return f"{names[month - 1]} {m.group(1)}"


def answer_listing_fact(
    question: str,
    card: Dict[str, Any],
    language: str = "en",
    advisor_email: str = "our advisory team",
    ownership: Optional[str] = None,
) -> Optional[str]:
    """Return a grounded one-topic answer, or None when the question is not one of the handled topics."""
    if language not in ("en", "pl") or not isinstance(card, dict) or not question:
        return None
    q = question.strip()
    raw = _raw(card)
    name = str(card.get("name") or "this listing").strip()
    pl = language == "pl"
    lays = _layouts(raw)
    currency = str(raw.get("currency") or card.get("currency") or "THB")

    # ---- currency conversion of the listing's starting price (indicative, arithmetic only)
    if _RE_CURRENCY.search(q):
        base = _num(raw.get("priceFrom")) or _num(card.get("price_numeric_min"))
        thb_rate = _fx_thb(currency)
        if base and thb_rate:
            thb = base * thb_rate
            ql = q.lower()
            if re.search(r"zlot|złot|pln", ql):
                tgt, val = "PLN", thb / _PLN_PER_THB
            elif re.search(r"euro|eur", ql):
                tgt, val = "EUR", thb / (_fx_thb("EUR") or 38.0)
            elif re.search(r"dollar|dolar|usd", ql):
                tgt, val = "USD", thb / (_fx_thb("USD") or 35.0)
            else:
                tgt, val = None, None
            if tgt:
                listed = _money(base, currency)
                if pl:
                    return (f"**{name}**: cena wyjściowa {listed}, czyli orientacyjnie ok. {_money(round(val, -2), tgt)} "
                            f"(przelicznik poglądowy, nie oferta wiążąca; ostateczną cenę potwierdza doradca).")
                return (f"**{name}**: starting price {listed}, which is roughly {_money(round(val, -2), tgt)} "
                        f"(indicative conversion, not a binding quote; our advisor confirms the final price).")
        return _not_in_data(name, "A converted price", "Przeliczona cena", advisor_email, language)

    # ---- price per square metre
    if _RE_PER_SQM.search(q):
        v = _num(raw.get("pricePerSqm"))
        if v:
            return (f"**{name}**: cena za m² wynosi ok. {_money(v, currency)}." if pl
                    else f"**{name}**: the listed price per square metre is about {_money(v, currency)}.")
        return _not_in_data(name, "The price per square metre", "Cena za m²", advisor_email, language)

    # ---- address
    if _RE_ADDRESS.search(q):
        a = raw.get("address")
        a = (a.get(language) or a.get("en") or "") if isinstance(a, dict) else (a or card.get("address") or "")
        a = str(a).strip()
        if a:
            return (f"Adres **{name}** w zweryfikowanych danych: {a}" if pl
                    else f"The address of **{name}** in our verified data: {a}")
        return _not_in_data(name, "The exact address", "Dokładny adres", advisor_email, language)

    # ---- completion / handover
    if _RE_COMPLETION.search(q):
        status_raw = str(card.get("status") or raw.get("completionStatus") or "").lower()
        status = (_STATUS_PL if pl else _STATUS_EN).get(status_raw, "")
        when = _fmt_date(raw.get("completionDate"), language)
        if when and status:
            return (f"**{name}** jest obecnie {status}. Planowane ukończenie według zweryfikowanych danych: {when}." if pl
                    else f"**{name}** is currently {status}. Expected completion in our verified data: {when}.")
        if when:
            return (f"Planowane ukończenie **{name}** według zweryfikowanych danych: {when}." if pl
                    else f"Expected completion for **{name}** in our verified data: {when}.")
        if status:
            return (f"**{name}** jest obecnie {status}, ale data ukończenia nie figuruje w zweryfikowanych danych. "
                    f"Nasz doradca ({advisor_email}) może ją potwierdzić." if pl
                    else f"**{name}** is currently {status}, but no completion date is in our verified data. "
                         f"Our advisor ({advisor_email}) can confirm it.")
        return _not_in_data(name, "The completion date", "Termin ukończenia", advisor_email, language)

    # ---- bedroom types on offer
    if _RE_BEDROOM_TYPES.search(q) and lays:
        labels = []
        for lay in lays:
            lab = _layout_label(lay, language)
            if lab not in labels:
                labels.append(lab)
        if labels:
            return (f"Typy lokali w zweryfikowanych danych **{name}**: " + ", ".join(labels) + "." if pl
                    else f"Unit types in our verified data for **{name}**: " + ", ".join(labels) + ".")

    # ---- units available / total
    if _RE_UNITS.search(q) and not _RE_LAYOUT_WORD.search(q):
        avail, total = _num(raw.get("availableUnits")), _num(raw.get("totalUnits"))
        if avail and total:
            return (f"W zweryfikowanych danych **{name}**: {int(avail)} dostępnych jednostek z {int(total)} w całym projekcie." if pl
                    else f"In our verified data for **{name}**: {int(avail)} units available out of {int(total)} in the whole project.")
        if total:
            return (f"Cały projekt **{name}** obejmuje {int(total)} jednostek; liczba dostępnych nie figuruje w danych. "
                    f"Doradca ({advisor_email}) potwierdzi aktualną dostępność." if pl
                    else f"The whole project **{name}** has {int(total)} units; the number still available isn't in our data. "
                         f"Our advisor ({advisor_email}) can confirm current availability.")
        if avail:
            return (f"Według zweryfikowanych danych **{name}** ma {int(avail)} dostępnych jednostek." if pl
                    else f"Our verified data shows {int(avail)} units available at **{name}**.")
        return _not_in_data(name, "The number of units", "Liczba jednostek", advisor_email, language)

    # ---- a specific layout (studio / 1-bed / 2-bed): size, price, count
    if _RE_LAYOUT_WORD.search(q) and _RE_LAYOUT_ASK.search(q):
        lay = _match_layout(q, lays)
        if lay:
            label = _layout_label(lay, language)
            parts = []
            area, p_from, p_to, n = _num(lay.get("area")), _num(lay.get("priceFrom")), _num(lay.get("priceTo")), _num(lay.get("availableUnits"))
            cur = str(lay.get("currency") or currency)
            if area:
                parts.append((f"powierzchnia ok. {area:g} m²" if pl else f"about {area:g} sqm"))
            if p_from and p_to and p_to != p_from:
                parts.append((f"cena od {_money(p_from, cur)} do {_money(p_to, cur)}" if pl else f"priced from {_money(p_from, cur)} to {_money(p_to, cur)}"))
            elif p_from:
                parts.append((f"cena od {_money(p_from, cur)}" if pl else f"priced from {_money(p_from, cur)}"))
            if n:
                parts.append((f"{int(n)} dostępnych" if pl else f"{int(n)} available"))
            if parts:
                return f"**{name}** — {label}: " + ", ".join(parts) + "."
        return _not_in_data(name, "That unit type", "Ten typ lokalu", advisor_email, language)

    # ---- price range / cheapest / most expensive unit
    if _RE_RANGE.search(q) and not _RE_OWNERSHIP.search(q):
        lo, hi = _num(raw.get("priceFrom")), _num(raw.get("priceTo"))
        if lo and hi and hi != lo:
            return (f"Ceny w **{name}** według zweryfikowanych danych: od {_money(lo, currency)} do {_money(hi, currency)}." if pl
                    else f"Prices at **{name}** in our verified data range from {_money(lo, currency)} to {_money(hi, currency)}.")
        if lo:
            return (f"Cena wyjściowa **{name}**: od {_money(lo, currency)}." if pl
                    else f"The starting price at **{name}** is {_money(lo, currency)}.")

    # ---- ownership / foreign buying
    if _RE_OWNERSHIP.search(q):
        if ownership:
            return (f"Forma własności w zweryfikowanych danych **{name}**: {ownership}. "
                    f"Zasady dla kupujących z zagranicy zależą od tej formy — potwierdzi je doradca ({advisor_email}) "
                    f"i prawnik." if pl
                    else f"The ownership structure in our verified data for **{name}** is: {ownership}. "
                         f"Rules for foreign buyers depend on that structure — our advisor ({advisor_email}) and a lawyer should confirm them.")
        return _not_in_data(name, "The ownership structure", "Forma własności", advisor_email, language)

    # ---- payment plan / developer
    if _RE_PAYMENT.search(q):
        plan = raw.get("paymentPlan")
        if isinstance(plan, str) and plan.strip():
            return (f"Plan płatności w zweryfikowanych danych **{name}**: {plan.strip()}" if pl
                    else f"Payment plan in our verified data for **{name}**: {plan.strip()}")
        return _not_in_data(name, "The payment plan", "Plan płatności", advisor_email, language)
    if _RE_DEVELOPER.search(q):
        return _not_in_data(name, "The developer's name", "Nazwa dewelopera", advisor_email, language)

    # ---- returns / investment advice: never a figure, never a guarantee
    if _RE_YIELD.search(q) or _RE_ADVICE.search(q):
        return (f"Nie podajemy w czacie stóp zwrotu ani prognoz cen i nie możemy gwarantować zysku. "
                f"Mogę przedstawić zweryfikowane dane o **{name}**, a doradca ({advisor_email}) omówi z Tobą inwestycję." if pl
                else f"We don't quote yield or return figures or price forecasts in chat, and returns can't be guaranteed. "
                     f"I can share the verified facts about **{name}**, and our advisor ({advisor_email}) can discuss the investment with you.")

    # ---- listed amenities that are not covered by the existing amenity answers
    for key, rx in _RE_AMENITY.items():
        if rx.search(q):
            have = {str(a).lower().replace("_", "-") for a in (raw.get("amenities") or card.get("amenities") or [])}
            ok = key in have or (key == "elevator" and ("lift" in have or "elevator" in have))
            label = {"balcony": "balcony", "security": "security", "garden": "garden",
                     "elevator": "elevator", "sauna": "sauna", "spa": "spa"}[key]
            if ok:
                return (f"**{name}**: tak — według zweryfikowanych danych: {label}." if pl
                        else f"**{name}**: yes — {label} is listed in our verified data.")
            return _not_in_data(name, label.capitalize(), label.capitalize(), advisor_email, language)

    # ---- everything else we simply do not hold
    if _RE_NOT_STATED.search(q):
        return _not_in_data(name, "That detail", "Ta informacja", advisor_email, language)

    return None
