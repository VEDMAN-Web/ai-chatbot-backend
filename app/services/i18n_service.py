"""
Internationalization (i18n) & Language Switching Service for Tajlandia Property AI.
Supports seamless switching between English ('en'), Polish ('pl'), and Thai ('th') for:
1. Public Customer Chatbot (Tajlandia.com, thai.estate, varsoviaestate.com)
2. Admin / Internal Worker AI Agent

Capabilities:
- API-level language parameter ('en' | 'pl' | 'th') in requests and responses
- In-chat conversational language switching (e.g., 'mów po polsku', 'switch to english', 'พูดภาษาไทย')
- Automatic language detection based on message lexicon and Unicode characters
- Multi-turn language state persistence in conversation preferences
- Fully localized, motivated luxury real estate advisor personas and deterministic fallbacks
"""

import re
from typing import Dict, Any, List, Optional, Tuple
from app.core.tenant import resolve_tenant

SUPPORTED_LANGUAGES = ["en", "pl", "th"]
DEFAULT_LANGUAGE = "en"


def get_data_unavailable_fallback(
    language: str = "en",
    site_origin: Optional[str] = None,
    detail: Optional[str] = None,
) -> str:
    """
    Canonical EN/PL/TH fallback when production data does not contain the requested fact.
    NEVER invent, guess, or fabricate an answer. Return this message instead.
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    detail_bit = f" ({detail})" if detail else ""

    lang = normalize_language(language) or "en"
    if lang == "th":
        return (
            f"ขออภัยครับ ขณะนี้ยังไม่มีข้อมูลที่ตรวจสอบแล้วในระบบสำหรับคำถามนี้{detail_bit}\n\n"
            "ผมตอบได้เฉพาะจากข้อมูลจริงในฐานข้อมูล production เท่านั้น และจะไม่เดาหรือสร้างข้อมูลขึ้นเอง\n\n"
            f"หากต้องการให้ทีมที่ปรึกษาช่วยตรวจสอบเพิ่มเติม สามารถติดต่อได้ที่ {advisor} "
            "หรือแจ้งรายละเอียดเพิ่มเติม (ทำเล / งบประมาณ / ประเภทอสังหาฯ) เพื่อให้ผมค้นหาจากรายการที่มีอยู่จริงครับ"
        )
    if lang == "pl":
        return (
            f"Przepraszam. W naszej zweryfikowanej bazie produkcyjnej nie ma obecnie danych odpowiadających na to pytanie{detail_bit}.\n\n"
            "Odpowiadam wyłącznie na podstawie rzeczywistych danych z produkcji i nigdy nie zgaduję ani nie wymyślam faktów.\n\n"
            f"Jeśli chcesz, nasz zespół doradców ({advisor}) może to sprawdzić ręcznie. "
            "Możesz też podać lokalizację, budżet lub typ nieruchomości. Wyszukam wyłącznie wśród dostępnych, zweryfikowanych ofert."
        )
    return (
        f"I'm sorry. That information is not available in our verified production database{detail_bit}.\n\n"
        "I only answer from live production data and will never guess or invent property details, prices, or availability.\n\n"
        f"Our advisory team ({advisor}) can investigate further if you like. "
        "You can also share a location, budget, or property type and I will search only among verified listings we actually have."
    )


def get_brochure_unavailable_text(
    language: str = "en",
    site_origin: Optional[str] = None,
    property_label: Optional[str] = None,
) -> str:
    """
    Honest reply when brochure PDF generation or upload fails.
    No em-dashes. Uses tenant advisor email only.
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"
    label = (property_label or "").strip()
    about = f" for {label}" if label else ""
    about_pl = f" dla {label}" if label else ""
    about_th = f" สำหรับ {label}" if label else ""

    if lang == "th":
        return (
            f"ขออภัยครับ ขณะนี้ยังไม่สามารถสร้างโบรชัวร์ PDF{about_th} ได้\n\n"
            f"ทีมที่ปรึกษา ({advisor}) สามารถส่งเอกสารยืนยันให้คุณได้โดยตรง "
            "หรือลองขออีกครั้งในอีกสักครู่ครับ"
        )
    if lang == "pl":
        return (
            f"Przepraszam. Nie udało się teraz wygenerować broszury PDF{about_pl}.\n\n"
            f"Nasz zespół doradców ({advisor}) może przesłać oficjalne materiały bezpośrednio. "
            "Możesz też spróbować ponownie za chwilę."
        )
    return (
        f"I'm sorry. The brochure PDF{about} cannot be generated right now.\n\n"
        f"Our advisory team ({advisor}) can send the official materials directly. "
        "You can also try again in a moment."
    )


def get_viewing_request_recorded_text(
    language: str = "en",
    *,
    property_label: str = "",
    preferred_date: str = "",
    preferred_time: str = "",
) -> str:
    """
    Public chat reply after schedule_viewing records a request (no calendar / no email).
    Must never say confirmed, booked, or added to calendar.
    """
    lang = normalize_language(language) or "en"
    prop = (property_label or "this property").strip() or "this property"
    date = (preferred_date or "").strip() or "the requested date"
    time = (preferred_time or "").strip() or "the requested time"

    if lang == "th":
        return (
            f"ระบบได้บันทึกคำขอนัดชม {prop} ในวันที่ {date} เวลา {time} แล้ว "
            "ทีมที่ปรึกษาจะติดต่อกลับเพื่อยืนยันนัดหมายครับ"
        )
    if lang == "pl":
        return (
            f"Twoja prośba o oglądanie obiektu {prop} w dniu {date} o {time} została zapisana. "
            "Nasz zespół doradców skontaktuje się z Tobą, aby potwierdzić termin."
        )
    return (
        f"Your viewing request for {prop} on {date} at {time} has been recorded. "
        "Our advisory team will contact you to confirm."
    )


def get_brochure_email_not_available_text(
    language: str = "en",
    site_origin: Optional[str] = None,
) -> str:
    """Honest reply: chat cannot email brochures; use PDF download / capture lead."""
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"
    if lang == "th":
        return (
            "แชทนี้ไม่สามารถส่งอีเมลได้ครับ กรุณาใช้ปุ่มดาวน์โหลดโบรชัวร์ PDF บนการ์ดอสังหาริมทรัพย์ "
            f"หรือแจ้งอีเมลของคุณเพื่อให้ทีมที่ปรึกษา ({advisor}) ส่งเอกสารให้"
        )
    if lang == "pl":
        return (
            "Z czatu nie wysyłamy e-maili. Skorzystaj z przycisku pobierania broszury PDF na karcie oferty "
            f"albo podaj swój e-mail, a zespół doradców ({advisor}) prześle dokumenty."
        )
    return (
        "We cannot send emails from this chat. Please use the PDF brochure download button / link on the property card, "
        f"or share your email so our advisory team ({advisor}) can send it to you."
    )


def get_cannot_email_or_calendar_text(
    language: str = "en",
    site_origin: Optional[str] = None,
    visitor_email: Optional[str] = None,
) -> str:
    """
    Honest reply when the visitor asks the bot to email them or add a calendar event.
    {email} is the current tenant's advisory email (never invent a brand address).
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"
    noted = bool((visitor_email or "").strip())

    if lang == "th":
        if noted:
            return (
                "แชทนี้ไม่สามารถส่งอีเมลหรือเพิ่มนัดหมายในปฏิทินได้ครับ "
                f"ระบบได้บันทึกรายละเอียดของคุณแล้ว และทีมที่ปรึกษาจะติดต่อคุณที่ {advisor}"
            )
        return (
            "แชทนี้ไม่สามารถส่งอีเมลหรือเพิ่มนัดหมายในปฏิทินได้ครับ "
            f"โปรดแจ้งอีเมลของคุณ ทีมที่ปรึกษาจะติดต่อกลับที่ {advisor}"
        )
    if lang == "pl":
        if noted:
            return (
                "Nie mogę wysyłać e-maili ani dodawać wydarzeń do kalendarza z tego czatu. "
                f"Zapisałem Twoje dane, a nasz zespół doradców skontaktuje się z Tobą pod adresem {advisor}."
            )
        return (
            "Nie mogę wysyłać e-maili ani dodawać wydarzeń do kalendarza z tego czatu. "
            f"Podaj swój e-mail, a zespół doradców skontaktuje się z Tobą pod adresem {advisor}."
        )
    if noted:
        return (
            "I can't send emails or add calendar events from this chat. "
            f"I have noted your details, and our advisory team will contact you at {advisor}."
        )
    return (
        "I can't send emails or add calendar events from this chat. "
        f"Please share your email, and our advisory team will contact you at {advisor}."
    )


def build_session_limit_reply(
    language: str = "en",
    site_origin: Optional[str] = None,
) -> str:
    """
    Reply when the visitor has reached MAX_USER_MESSAGES_PER_SESSION in this chat.
    No em-dashes. Uses tenant advisor email from config only.
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"

    if lang == "th":
        return (
            "แชทนี้ถึงจำนวนข้อความสูงสุดแล้ว กรุณาเริ่มแชทใหม่เพื่อสนทนาต่อ\n\n"
            f"หากต้องการความช่วยเหลือเพิ่มเติม ติดต่อทีมที่ปรึกษาได้ที่ {advisor}"
        )
    if lang == "pl":
        return (
            "Ta rozmowa osiągnęła limit wiadomości. Aby kontynuować, rozpocznij nowy czat.\n\n"
            f"Jeśli potrzebujesz pomocy, napisz do naszego zespołu doradców ({advisor})."
        )
    return (
        "This chat has reached its message limit. Please start a new chat to continue.\n\n"
        f"Our advisory team ({advisor}) is happy to help if you need further assistance."
    )


def build_name_not_found_reply(
    language: str = "en",
    site_origin: Optional[str] = None,
    asked_name: Optional[str] = None,
    suggestions: Optional[List[str]] = None,
) -> str:
    """
    Honest reply when a named-property search has no confident match.
    May list up to 3 close names (Did you mean). Never invents price/beds/ownership.
    No em-dashes.
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"
    asked = (asked_name or "").strip()
    tips = [str(s).strip() for s in (suggestions or []) if str(s).strip()][:3]
    tip_block = "\n".join(f"• {t}" for t in tips)

    if lang == "th":
        head = (
            f"ไม่พบโครงการชื่อ \"{asked}\" ในฐานข้อมูลที่ตรวจสอบแล้วครับ"
            if asked
            else "ไม่พบโครงการตามชื่อที่ระบุในฐานข้อมูลที่ตรวจสอบแล้วครับ"
        )
        mid = f"\n\nคุณหมายถึงรายการเหล่านี้หรือไม่:\n{tip_block}" if tip_block else ""
        return (
            f"{head}{mid}\n\n"
            f"ทีมที่ปรึกษา ({advisor}) สามารถช่วยตรวจสอบชื่อโครงการให้ได้ "
            "หรือแจ้งทำเล / งบประมาณ เพื่อค้นหาจากรายการที่มีอยู่จริงครับ"
        )
    if lang == "pl":
        head = (
            f"Nie znaleziono nieruchomości \"{asked}\" w zweryfikowanej bazie."
            if asked
            else "Nie znaleziono nieruchomości o podanej nazwie w zweryfikowanej bazie."
        )
        mid = f"\n\nCzy chodziło Ci o:\n{tip_block}" if tip_block else ""
        return (
            f"{head}{mid}\n\n"
            f"Nasz zespół doradców ({advisor}) może pomóc potwierdzić nazwę projektu "
            "albo wyszukać oferty po lokalizacji i budżecie."
        )
    head = (
        f"\"{asked}\" is not in our verified database."
        if asked
        else "That property name is not in our verified database."
    )
    mid = f"\n\nDid you mean:\n{tip_block}" if tip_block else ""
    return (
        f"{head}{mid}\n\n"
        f"Our advisory team ({advisor}) can help confirm the project name, "
        "or you can search by location and budget among verified listings."
    )


def build_seen_all_reply(
    language: str = "en",
    site_origin: Optional[str] = None,
    criteria_summary: Optional[str] = None,
    total_matching: int = 0,
) -> str:
    """
    Honest reply when matching listings exist but were all already shown in this session.
    Never claims "no match".
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"
    n = max(int(total_matching or 0), 1)
    criteria = (criteria_summary or "").strip()

    if lang == "th":
        head = (
            f"คุณดูรายการที่ผ่านการตรวจสอบครบทั้ง {n} รายการที่ตรงกับเงื่อนไขแล้วครับ"
            f"{(' (' + criteria + ')') if criteria else ''}"
        )
        return (
            f"{head}\n\n"
            "ขั้นตอนถัดไปที่แนะนำ:\n"
            "• ผ่อนงบประมาณ\n"
            "• ลองเมือง / เกาะอื่น\n"
            "• เปลี่ยนประเภทอสังหาฯ (เช่น วิลล่า ↔ คอนโด)\n\n"
            f"ทีมที่ปรึกษา ({advisor}) ยินดีช่วยปรับเงื่อนไขให้ตรงขึ้นได้ครับ"
        )
    if lang == "pl":
        head = (
            f"Widziałeś już wszystkie {n} zweryfikowane oferty spełniające kryteria"
            f"{(': ' + criteria) if criteria else ''}."
        )
        return (
            f"{head}\n\n"
            "Konkretne następne kroki:\n"
            "• poluzuj budżet\n"
            "• wybierz inne miasto / wyspę\n"
            "• zmień typ nieruchomości (np. willa ↔ apartament)\n\n"
            f"Nasz zespół doradców ({advisor}) pomoże doprecyzować wyszukiwanie."
        )
    if criteria:
        head = f"You have already seen all {n} verified listings matching {criteria}."
    else:
        head = f"You have already seen all {n} verified listings matching your criteria."
    return (
        f"{head}\n\n"
        "Concrete next steps:\n"
        "• Relax the budget\n"
        "• Try another city or island\n"
        "• Switch property type (e.g. villa <-> condo)\n\n"
        f"Our advisory team ({advisor}) can help refine the search further."
    )


def build_amenity_none_match_reply(
    language: str = "en",
    site_origin: Optional[str] = None,
    location: Optional[str] = None,
    amenity_label: str = "gym",
    alternatives: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Honest reply when no listing in the requested location has the amenity.
    Alternatives (if any) must already be labelled (e.g. 'Gym not listed').
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"
    loc = (location or "").strip() or ("your selected area" if lang == "en" else ("wybranej lokalizacji" if lang == "pl" else "พื้นที่ที่เลือก"))
    amenity = (amenity_label or "amenity").strip()
    alts = [c for c in (alternatives or []) if isinstance(c, dict)][:3]

    def _alt_line(c: Dict[str, Any]) -> str:
        name = str(c.get("name") or c.get("property_name") or "").strip()
        place = str(c.get("location") or "").strip()
        labels = c.get("amenity_labels") or []
        lab = labels[0] if labels else f"{amenity} not listed"
        bits = [b for b in [name, place, lab] if b]
        return "• " + " | ".join(bits)

    alt_block = "\n".join(line for line in (_alt_line(c) for c in alts) if line)

    if lang == "th":
        head = f"ไม่พบรายการที่ผ่านการตรวจสอบใน {loc} ที่มี{amenity}ในข้อมูลครับ"
        mid = f"\n\nตัวเลือกใกล้เคียง (ระบุชัดว่าไม่มี{amenity}ในรายการ):\n{alt_block}" if alt_block else ""
        return (
            f"{head}{mid}\n\n"
            f"ทีมที่ปรึกษา ({advisor}) สามารถช่วยค้นหาโครงการที่มีสิ่งอำนวยความสะดวกนี้ได้ครับ"
        )
    if lang == "pl":
        head = f"Żadna zweryfikowana oferta w lokalizacji {loc} nie wymienia: {amenity}."
        mid = f"\n\nNajbliższe alternatywy (oznaczone jako brak {amenity}):\n{alt_block}" if alt_block else ""
        return (
            f"{head}{mid}\n\n"
            f"Nasz zespół doradców ({advisor}) pomoże znaleźć oferty z tym udogodnieniem."
        )
    head = f"None of the verified listings in {loc} list a {amenity.lower()}."
    mid = f"\n\nClosest alternatives (clearly labelled):\n{alt_block}" if alt_block else ""
    return (
        f"{head}{mid}\n\n"
        f"Our advisory team ({advisor}) can help find listings that document this amenity."
    )


def build_no_match_reply(
    language: str = "en",
    site_origin: Optional[str] = None,
    criteria_summary: Optional[str] = None,
    relaxed_filter: Optional[str] = None,
    alternatives: Optional[List[Dict[str, Any]]] = None,
    lowest_price_thb: Optional[float] = None,
    previous_reply: Optional[str] = None,
) -> str:
    """
    Honest zero-result reply. Never invents listings.
    Optionally lists up to 3 real alternatives from a search that relaxed exactly one filter.
    """
    tenant = resolve_tenant(site_origin)
    advisor = getattr(tenant, "advisor_email", None) or ""
    lang = normalize_language(language) or "en"
    alts = [c for c in (alternatives or []) if isinstance(c, dict)][:3]
    criteria = (criteria_summary or "").strip()

    def _alt_line(c: Dict[str, Any]) -> str:
        name = str(c.get("name") or c.get("property_name") or "").strip()
        loc = str(c.get("location") or "").strip()
        price = str(c.get("price") or c.get("price_display") or "").strip()
        bits = [b for b in [name, loc, price] if b]
        return "• " + " | ".join(bits) if bits else ""

    alt_block = "\n".join(line for line in (_alt_line(c) for c in alts) if line)

    if lang == "th":
        head = "ไม่พบรายการที่ผ่านการตรวจสอบซึ่งตรงกับเงื่อนไขของคุณครับ"
        if criteria:
            head = f"ไม่พบรายการที่ผ่านการตรวจสอบซึ่งตรงกับเงื่อนไข: {criteria}"
        if alt_block and relaxed_filter:
            mid = (
                f"\n\nรายการนี้ไม่ตรงตามเงื่อนไขของคุณ แต่เป็นตัวเลือกที่ใกล้เคียงที่สุด "
                f"(ผ่อนเงื่อนไข: {relaxed_filter}):\n{alt_block}"
            )
        else:
            mid = ""
        return (
            f"{head}{mid}\n\n"
            f"ทีมที่ปรึกษา ({advisor}) สามารถช่วยตรวจสอบเพิ่มเติมได้ "
            "หรือแจ้งทำเล / งบประมาณ / ประเภทอสังหาฯ เพื่อค้นหาจากรายการที่มีอยู่จริงครับ"
        )
    if lang == "pl":
        head = "Nie znaleziono zweryfikowanej oferty spełniającej Twoje kryteria."
        if criteria:
            head = f"Nie znaleziono zweryfikowanej oferty spełniającej kryteria: {criteria}."
        if alt_block and relaxed_filter:
            mid = (
                f"\n\nTo nie spełnia Twoich kryteriów, ale jest to najbliższa opcja "
                f"(poluzowano: {relaxed_filter}):\n{alt_block}"
            )
        else:
            mid = ""
        return (
            f"{head}{mid}\n\n"
            f"Nasz zespół doradców ({advisor}) może pomóc dalej. "
            "Możesz też podać lokalizację, budżet lub typ nieruchomości."
        )

    head = "No verified listing matches your criteria."
    if criteria:
        head = f"No verified listing matches your criteria: {criteria}."
    price_note = ""
    if lowest_price_thb:
        millions = float(lowest_price_thb) / 1_000_000
        price_note = (
            f" The lowest verified price is about {millions:.2f}M THB. "
            "Want me to show the cheapest options? I can also clear these filters."
        )
    if alt_block and relaxed_filter:
        mid = (
            f"\n\nThis does not match your criteria, but it is the closest option "
            f"(relaxed: {relaxed_filter}):\n{alt_block}"
        )
    else:
        mid = ""
    reply = (
        f"{head}{price_note}{mid}\n\n"
        f"Our advisory team ({advisor}) can help further. "
        "You can also share a location, budget, or property type to search among verified listings only."
    )
    if previous_reply and reply.strip() == str(previous_reply).strip():
        reply = reply + "\n\nSay 'clear my filters' and I will start a wider search."
    return reply

LANGUAGE_ALIASES = {
    "pl": "pl",
    "polish": "pl",
    "polski": "pl",
    "pl-pl": "pl",
    "pl_pl": "pl",
    "en": "en",
    "english": "en",
    "angielski": "en",
    "en-us": "en",
    "en-gb": "en",
    "en_us": "en",
    "en_gb": "en",
    "th": "th",
    "thai": "th",
    "tajski": "th",
    "th-th": "th",
    "th_th": "th",
    "ภาษาไทย": "th",
    "ไทย": "th"
}

POLISH_SWITCH_TRIGGERS = [
    "mów po polsku", "mow po polsku", "pisz po polsku", "po polsku",
    "przełącz na polski", "przelacz na polski", "zmień na polski", "zmien na polski",
    "włącz polski", "wlacz polski", "switch to polish", "speak polish", "talk in polish",
    "in polish", "polish please", "język polski", "jezyk polski", "po polsku proszę"
]

ENGLISH_SWITCH_TRIGGERS = [
    "speak english", "talk in english", "write in english", "switch to english",
    "in english", "english please", "mów po angielsku", "mow po angielsku",
    "pisz po angielsku", "po angielsku", "przełącz na angielski", "przelacz na angielski",
    "zmień na angielski", "zmien na angielski", "język angielski", "jezyk angielski"
]

THAI_SWITCH_TRIGGERS = [
    "พูดภาษาไทย", "ภาษาไทย", "เปลี่ยนเป็นภาษาไทย", "switch to thai", "speak thai",
    "talk in thai", "write in thai", "in thai", "thai please", "ภาษาไทยครับ",
    "ภาษาไทยค่ะ", "ไทย", "ใช้ภาษาไทย", "ตอบเป็นภาษาไทย", "mów po tajsku", "po tajsku"
]

POLISH_LEXICON = [
    "szukam", "pokaż", "pokaz", "wille", "willa", "willi", "apartament", "apartamenty",
    "mieszkanie", "mieszkania", "dom", "domy", "basen", "basenem", "sypialnie", "sypialni",
    "ile kosztuje", "jaka cena", "tajlandii", "tajlandia", "kupić", "kupic", "wynajem",
    "cześć", "czesc", "dzień dobry", "dzien dobry", "proszę", "prosze", "dziękuję", "dziekuje",
    "porównaj", "porownaj", "więcej", "wiecej", "tańsze", "tansze", "widok na morze",
    "blisko plaży", "blisko plazy", "plaża", "plaza", "dla obcokrajowca", "własność", "wlasnosc"
]

THAI_LEXICON = [
    "วิลล่า", "พูลวิลล่า", "คอนโด", "คอนโดมิเนียม", "อสังหาริมทรัพย์", "สระว่ายน้ำ", "ห้องนอน",
    "ห้องน้ำ", "ภูเก็ต", "เกาะสมุย", "สมุย", "พัทยา", "กรุงเทพ", "ราคา", "ล้านบาท", "บาท",
    "ซื้อ", "เช่า", "สวัสดี", "ขอบคุณ", "รายละเอียด", "เปรียบเทียบ", "ถูกกว่า", "วิวทะเล",
    "ติดหาด", "ชายหาด", "ที่ดิน", "กรรมสิทธิ์", "โฉนด", "สัญญาเช่า"
]


def normalize_language(lang_input: Optional[str]) -> Optional[str]:
    """Normalizes arbitrary language strings to canonical 'en', 'pl', or 'th'."""
    if not lang_input:
        return None
    cleaned = lang_input.strip().lower()
    return LANGUAGE_ALIASES.get(cleaned, None)


def detect_explicit_language_switch(message: str) -> Optional[str]:
    """
    Detects whether user is giving an explicit in-chat command to switch language.
    Uses strict word boundaries and length checks to prevent false triggers (e.g. 'in Thailand' matching 'in Thai').
    """
    m = message.lower().strip()
    words = m.split()

    # Explicit Thai switch triggers
    thai_regexes = [
        r"\b(?:switch\s+to\s+thai|speak\s+thai|talk\s+in\s+thai|write\s+in\s+thai|reply\s+in\s+thai|thai\s+please)\b",
        r"\b(?:mów\s+po\s+tajsku|mow\s+po\s+tajsku|pisz\s+po\s+tajsku|po\s+tajsku)\b",
        r"^(?:พูดภาษาไทย|ภาษาไทย|เปลี่ยนเป็นภาษาไทย|ใช้ภาษาไทย|ตอบเป็นภาษาไทย|ภาษาไทยครับ|ภาษาไทยค่ะ)$"
    ]
    if len(words) <= 3 and re.search(r"^(?:in\s+thai|thai|tajski)$", m):
        return "th"
    for pattern in thai_regexes:
        if re.search(pattern, m):
            return "th"

    # Explicit Polish switch triggers
    polish_regexes = [
        r"\b(?:mów\s+po\s+polsku|mow\s+po\s+polsku|pisz\s+po\s+polsku|switch\s+to\s+polish|speak\s+polish|talk\s+in\s+polish|polish\s+please)\b",
        r"\b(?:przełącz\s+na\s+polski|przelacz\s+na\s+polski|zmień\s+na\s+polski|zmien\s+na\s+polski|włącz\s+polski|wlacz\s+polski|język\s+polski|jezyk\s+polski)\b"
    ]
    if len(words) <= 3 and re.search(r"^(?:po\s+polsku|polski|in\s+polish)$", m):
        return "pl"
    for pattern in polish_regexes:
        if re.search(pattern, m):
            return "pl"

    # Explicit English switch triggers
    english_regexes = [
        r"\b(?:switch\s+to\s+english|speak\s+english|talk\s+in\s+english|write\s+in\s+english|reply\s+in\s+english|english\s+please)\b",
        r"\b(?:mów\s+po\s+angielsku|mow\s+po\s+angielsku|pisz\s+po\s+angielsku|po\s+angielsku|język\s+angielski|jezyk\s+angielski)\b"
    ]
    if len(words) <= 3 and re.search(r"^(?:in\s+english|english|angielski)$", m):
        return "en"
    for pattern in english_regexes:
        if re.search(pattern, m):
            return "en"

    return None


def detect_message_language(message: str) -> Optional[str]:
    """Detects whether natural language query is written in Thai or Polish."""
    m = message.lower().strip()
    # Check for Thai Unicode characters range (0x0E00 - 0x0E7F)
    if any('\u0e00' <= c <= '\u0e7f' for c in message):
        return "th"
    # Check for Polish diacritics
    if any(c in m for c in "ąćęłńóśźż"):
        return "pl"
    # Check for Polish lexicon keywords
    for word in POLISH_LEXICON:
        if re.search(r'\b' + re.escape(word) + r'\b', m):
            return "pl"
    # Check for Thai lexicon keywords
    for word in THAI_LEXICON:
        if word in m:
            return "th"
    return None


def resolve_language(
    requested_language: Optional[str],
    user_message: str,
    stored_preferences: Optional[Dict[str, Any]] = None
) -> Tuple[str, bool]:
    """
    Resolves active turn language ('en' | 'pl' | 'th') and indicates whether an explicit switch was commanded.
    Priority:
    1. In-chat explicit switch command (e.g. 'พูดภาษาไทย' -> 'th', 'mów po polsku' -> 'pl', is_switch=True)
    2. Explicit API requested_language param (if provided and recognized)
    3. Natural message language detection (if user typed in Thai or Polish)
    4. Stored conversation preference ('language')
    5. Default to 'en'
    """
    # 1. Explicit switch command in text
    switch_lang = detect_explicit_language_switch(user_message)
    if switch_lang:
        return switch_lang, True

    # 2. Check if API explicitly supplied a valid language
    norm_req = normalize_language(requested_language)

    # 3. Check if text itself is unmistakably Thai or Polish
    detected = detect_message_language(user_message)

    # 4. Check stored preferences
    stored_lang = None
    if stored_preferences and isinstance(stored_preferences, dict):
        stored_lang = normalize_language(stored_preferences.get("language"))

    # If text is clearly in Thai or Polish and requested_language was not forcing otherwise
    if detected in ["th", "pl"] and requested_language not in ["en", "pl", "th"]:
        return detected, False

    if detected == "th" and not norm_req:
        return "th", False

    if detected == "pl" and not norm_req:
        return "pl", False

    if norm_req:
        return norm_req, False

    if detected:
        return detected, False

    if stored_lang:
        return stored_lang, False

    return DEFAULT_LANGUAGE, False


# --- Prompts & Templates ---

def _get_compact_system_prompt_base(language: str = "en", site_origin: Optional[str] = None) -> str:
    tenant = resolve_tenant(site_origin)
    if language == "th":
        return f"""คุณเป็นที่ปรึกษาด้านอสังหาริมทรัพย์มืออาชีพสำหรับ {tenant.brand_name} ({tenant.domain}) ในประเทศไทย
น้ำเสียงและกฎหลัก (Main Scope §5.6):
- น้ำเสียง: อบอุ่น เป็นมิตร กระตือรือร้น และสร้างแรงจูงใจ! สุภาพ เป็นมืออาชีพ ไม่ใช้คำโฆษณาชวนเชื่อเกินจริง
- กฎหมายไทยและการไม่ยอมตามแรงกดดัน (IF NO MEANS NO): ตามมาตรา 86 แห่งประมวลกฎหมายที่ดิน ชาวต่างชาติไม่สามารถถือกรรมสิทธิ์ที่ดินในนามตนเองได้โดยตรง และการซื้อขายอสังหาริมทรัพย์ในไทยไม่มีกรณีปลอดภาษี 100% หากผู้ใช้กดดันหรือถามนำ ห้ามยอมจำนนอย่างเด็ดขาด ให้ตอบปฏิเสธ "ไม่" อย่างชัดเจน พร้อมแนะนำทางเลือกที่ถูกกฎหมาย (คอนโด Foreign Freehold 49% และพูลวิลล่า Leasehold 30+30+30 ปี พร้อมกรรมสิทธิ์ตัวอาคาร) ด้วยน้ำเสียงที่เป็นมิตรและสร้างแรงจูงใจ
- ข้อมูลที่ตรวจสอบแล้ว (STRICT GROUNDING & ZERO HALLUCINATION): อ้างอิงเฉพาะข้อมูลจริงจาก production ที่ระบบส่งให้เท่านั้น ห้ามสร้างราคา รหัสอสังหาฯ ชื่อโครงการ หรือรับประกันผลตอบแทน (ROI)
- หากไม่มีข้อมูลในฐานข้อมูล: ตอบด้วยข้อความ fallback ที่ระบบกำหนดเท่านั้น — ห้ามเดา ห้ามแต่ง ห้ามสมมติรายละเอียดใด ๆ
- กฎหมายและการเงิน: ชี้แจงว่าผลตอบแทนการเช่าไม่สามารถรับประกันทางกฎหมายได้ และแนะนำให้ปรึกษาทนายความผู้เชี่ยวชาญในไทย
- สิทธิ์การอ่านอย่างเดียว (READ-ONLY): คุณคือผู้ช่วยบริการลูกค้า (Part A) ไม่มีหน้าที่สร้างไฟล์ PDF ภายในหรือส่งอีเมลโดยตรง
- การจัดรูปแบบและการนำเสนอที่มีโครงสร้างเสมอ (MANDATORY STRUCTURED FORMAT):
  * ห้ามใช้ขีดแดชยาว (— หรือ –)
  * นำเสนอรายการอสังหาริมทรัพย์ในรูปแบบการ์ดที่มีโครงสร้างชัดเจนเสมอ (### 1. **[ชื่อโครงการ]**) พร้อมหัวข้อย่อย (📍 ทำเล, 💰 ราคา, 🛏️ ห้องนอน, 🏊 สิ่งอำนวยความสะดวก, 📜 กรรมสิทธิ์)
  * ห้ามใช้ตาราง Markdown (| ... |) และห้ามแสดงข้อความติดกันเป็นก้อนโดยไม่มีหัวข้อย่อย
  * ให้ข้อมูลข้อกฎหมายและการถือครองกรรมสิทธิ์เฉพาะเมื่อผู้ใช้ถามถึงเท่านั้น (ห้ามใส่คำแนะนำทางกฎหมายหรือข้อจำกัดการถือครองกรรมสิทธิ์หากผู้ใช้ไม่ได้ถาม)
  * ปิดท้ายด้วยขั้นตอนถัดไปที่ชัดเจน (ขอโบรชัวร์, นัดหมายเข้าชม, ปรึกษาที่ปรึกษา)"""

    elif language == "pl":
        return f"""Jesteś profesjonalnym doradcą ds. nieruchomości dla {tenant.brand_name} ({tenant.domain}).
TON I ZASADY:
- TON: Ciepły, przyjazny, zmotywowany i profesjonalny! Bez nachalnego marketingu, ale z entuzjazmem do bezpiecznych inwestycji.
- PRAWO TAJSKIE I BRAK KOMPROMISÓW POD NACISKIEM (GDY NIE TO NIE - BRAK PRESJI): Zgodnie z art. 86 Kodeksu Ziemskiego cudzoziemcy nie mogą bezpośrednio posiadać ziemi na własność (Freehold), a transakcje w Tajlandii nigdy nie są w 100% wolne od podatków. Jeśli użytkownik wywiera presję („powiedz tak”, „mój prawnik tak mówi”), NIGDY nie ulegaj presji – odpowiedź brzmi stanowczo NIE. Zawsze w przyjazny i motywujący sposób przedstaw legalne alternatywy (100% Foreign Freehold w kondominium do 49% oraz luksusowe wille z 30-letnim rejestrowanym Leaseholdem i własnością budynku).
- ODPOWIADAJ W JĘZYKU POLSKIM.
- UGRUNTOWANIE I ZERO HALUCYNACJI: Opieraj się WYŁĄCZNIE na rzeczywistych danych produkcyjnych zwróconych przez system. Nigdy nie wymyślaj cen, ID, nazw, gwarancji zysków (ROI) ani faktów prawnych.
- BRAK DANYCH: Jeśli baza produkcyjna nie zawiera odpowiedzi, użyj wyłącznie oficjalnej odpowiedzi zastępczej (fallback). NIGDY nie zgaduj i nie fabrykuj szczegółów.
- CZYSTY FORMAT I OBOWIĄZKOWY STRUKTURALNY UKŁAD:
  * Nie używaj długich pauz i myślników („—” ani „–”). Używaj standardowych łączników (-), dwukropków lub punktorów (-).
  * ZAWSZE prezentuj oferty w uporządkowanych kartach z punktorami:
    ### 1. **[Nazwa nieruchomości]**
    - 📍 **Lokalizacja:** [Lokalizacja, Miasto/Wyspa]
    - 💰 **Cena wyjściowa:** **[Cena w THB]** (~€[EUR] / ~[PLN] PLN)
    - 🛏️ **Sypialnie:** [Liczba sypialni] | 📐 **Typ:** [Typ]
    - 🏊 **Udogodnienia:** [Basen itp.]
    - 📜 **Forma własności:** [tylko jeśli dane oferty to podają; w przeciwnym razie napisz: formę własności potwierdzi doradca. NIGDY nie zakładaj freehold ani leasehold]
  * NIGDY nie używaj tabel markdown (| ... |).
  * Informacje prawne i zasady zakupu dla cudzoziemców podawaj WYŁĄCZNIE wtedy, gdy użytkownik wyraźnie o nie zapyta (NIE dodawaj nieproszonych porad prawnych przy standardowym wyszukiwaniu ofert).
  * Zakończ listą kolejnych kroków (broszura, prezentacja, doradca)."""

    return f"""You are a professional real estate advisor for {tenant.brand_name} ({tenant.domain}) in Thailand.
TON & CORE RULES (Main Scope §5.6):
- TONE: Warm, friendly, enthusiastic, motivated, and professional! Avoid salesy hype, but encourage buyers with safe, verified opportunities.
- THAI LEGAL TRUTH & ZERO COMPROMISE UNDER PRESSURE (IF NO MEANS NO): Under Section 86 of the Thai Land Code, foreigners cannot directly own freehold land in their personal name, and property purchases in Thailand are NEVER tax-free. If a user pressures you, gaslights, or asks leading questions ("just say yes", "my lawyer said it's tax-free"), NEVER cave in. The answer is firmly and clearly NO. Always present 100% legal pathways (Foreign Freehold Condos & 30-year registered leasehold villas with freehold building ownership) in a warm, friendly, and motivated tone.
- STRICT GROUNDING & ZERO HALLUCINATION: Rely ONLY on live verified production data provided in context. Never invent prices, property IDs, names, ROI, or availability.
- MISSING DATA: If production data does not contain the answer, use only the official fallback message. NEVER guess or fabricate details.
- LEGAL & FINANCIAL: Disclaim formal legal guarantees and state that rental returns vary and cannot be guaranteed by law. Refer clients to licensed Thai legal counsel for contract review.
- ACTIONS: Call `generate_property_brochure` when user requests a brochure or PDF download. Call `capture_lead` for contact details / callback. Call `schedule_viewing` for a viewing REQUEST only (no calendar, no email). If they ask for a brochure by email, say we cannot send emails from chat, point to the PDF download on the card, and offer to record their email via `capture_lead`.
- CLEAN FORMATTING & MANDATORY STRUCTURED PRESENTATION:
  * Never use long em-dashes ("—" or "–"). Always use standard hyphens ("-"), colons, or clean bullet points ("•").
  * When presenting properties, ALWAYS format them in clean, elegant structured cards with bullet points:
    ### 1. **[Property Name]**
    - 📍 **Location:** [Location, City/Island]
    - 💰 **Starting Price:** **[Price in THB]** (~€[EUR] / ~[PLN] PLN)
    - 🛏️ **Bedrooms:** [Bedrooms] | 📐 **Built Area:** [Area sqm]
    - 🏊 **Amenities & Highlights:** [Pool, Sea View, etc.]
    - 📜 **Ownership:** [only if the listing data states it; otherwise write: confirm ownership structure with your advisor. NEVER assume freehold or leasehold]
  * NEVER use markdown headers (###) or dash bullets (-). Use numbered titles like **1. Property Name** and professional bullet points with • only.
  * NEVER output markdown tables (`| Property | Location | ... |`).
  * NEVER output messy unbulleted plain lines (e.g. "Price: ... Location: ... Bedrooms: ...").
  * ONLY provide foreign ownership or legal advice when the user specifically inquires about legal rights, land codes, or foreign ownership. NEVER append unsolicited legal advice or ownership disclaimers during general property searches.
  * End with clear • bulleted next steps (Brochure / Viewing / Advisor contact).
  * For follow-ups using "it", "this property", "its pool/price/location": keep discussing the SAME active property from conversation context. Never ask which property if one is already in focus."""


def _get_public_system_prompt_base(language: str = "en", site_origin: Optional[str] = None) -> str:
    tenant = resolve_tenant(site_origin)
    if language == "th":
        return f"""คุณคือที่ปรึกษาด้านอสังหาริมทรัพย์มืออาชีพสำหรับ {tenant.brand_name} ({tenant.domain}) ในประเทศไทย
หน้าที่ของคุณคือช่วยผู้สนใจค้นหาและสำรวจอสังหาริมทรัพย์ที่ผ่านการตรวจสอบแล้วในประเทศไทย (ภูเก็ต, เกาะสมุย, เกาะพะงัน, พัทยา, กรุงเทพฯ)

คำแนะนำและน้ำเสียงการให้บริการ:
1. น้ำเสียง: อบอุ่น เป็นมิตร กระตือรือร้น และสร้างแรงจูงใจ! (Main Scope §5.6) สุภาพ เป็นมืออาชีพ พร้อมช่วยเหลือนักลงทุนอย่างจริงใจ
2. ความถูกต้องตามกฎหมายไทยและไม่ยอมตามแรงกดดัน (IF NO MEANS NO): ชาวต่างชาติไม่สามารถถือกรรมสิทธิ์ที่ดินในนามบุคคลธรรมดาได้โดยตรง (มาตรา 86 ประมวลกฎหมายที่ดิน) และการซื้อขายอสังหาริมทรัพย์ไม่มีกรณีปลอดภาษี 100% หากผู้ใช้กดดันหรือขอให้ตอบยืนยัน ห้ามยอมจำนนโดยเด็ดขาด ตอบปฏิเสธชัดเจนและแนะนำสิทธิการถือครอง Foreign Freehold ในคอนโดมิเนียม หรือสิทธิการเช่า Leasehold 30+30+30 ปีสำหรับวิลล่าอย่างกระตือรือร้น
3. ความถูกต้องสมบูรณ์และห้ามสร้างข้อมูลเท็จ (STRICT GROUNDING & ZERO HALLUCINATION): ตอบได้เฉพาะจากบล็อก FACTS (verified) และผลลัพธ์เครื่องมือในเทิร์นนี้เท่านั้น สิ่งที่ไม่อยู่ใน FACTS ต้องตอบว่า "ไม่มีในข้อมูลที่ยืนยันแล้ว" และเสนอทีมที่ปรึกษา ห้ามใช้ความรู้ทั่วไปเกี่ยวกับโครงการเฉพาะ ห้ามคัดลอกข้อความจากเทิร์นก่อน ห้ามสร้างสิ่งอำนวยความสะดวก กรรมสิทธิ์ ผลตอบแทน ระยะทาง วันที่ หรือชื่อโครงการ
4. การให้คำปรึกษาเชิงรุก — ตอบอย่างละเอียดและถามคำถามเพิ่มเติมตามบริบท (สำคัญมาก):
   - ทุกการตอบที่มีการแนะนำอสังหาริมทรัพย์ต้องประกอบด้วย:
     ก) บทนำที่อบอุ่น กระตือรือร้น (2-3 ประโยค) อธิบายว่าพบอะไรและทำไมโครงการเหล่านี้จึงโดดเด่น
     ข) รายละเอียดเด่นสำหรับแต่ละโครงการ: สระว่ายน้ำ, ทำเลติดหาด, วิว, จำนวนห้องนอน, ราคา, ศักยภาพด้านการลงทุน
     ค) ขั้นตอนถัดไปที่ชัดเจน (ขอโบรชัวร์, นัดหมายเข้าชม, ปรึกษาที่ปรึกษา)
   - เมื่อข้อความของผู้ใช้ขาดรายละเอียดสำคัญ (เช่น ไม่ได้ระบุทำเล งบประมาณ หรือจำนวนห้องนอน): ให้ถามคำถามเพิ่มเติม 2-3 ข้อ อย่างเป็นธรรมชาติและเหมาะกับบริบทที่แท้จริงของการสนทนา ไม่ใช้คำถามสำเร็จรูป คำถามต้องสมเหตุสมผลตามสิ่งที่ผู้ใช้พูดมาจริงๆ ตัวอย่าง: ถ้าพูดถึงการลงทุน — ถามเรื่องผลตอบแทนที่ต้องการ, ทำเลที่ชอบ, และวัตถุประสงค์; ถ้าพูดถึงวิลล่า — ถามเรื่องจำนวนห้องนอน, เกาะที่ชอบ, และงบประมาณ สูงสุด 3 ข้อ คำถามต้องรู้สึกเหมือนมาจากที่ปรึกษาจริงๆ
   - กรณีที่เงื่อนไขของผู้ใช้ไม่มีรายการที่ตรงกัน: ห้ามนำเสนอรายการที่ไม่ตรงอ้างว่าตรงกับความต้องการ ชี้แจงตามความเป็นจริงอย่างมืออาชีพ แนะนำทางเลือกที่ดีที่สุดจากฐานข้อมูลด้วยน้ำเสียงกระตือรือร้น และถามคำถามเพิ่มเติม 2-3 ข้อตามบริบท
   - เมื่อผู้ใช้ให้ข้อมูลครบถ้วน: แสดงอสังหาริมทรัพย์ที่ตรงกันโดยตรง ถามเพิ่มเฉพาะเมื่อมีสิ่งสำคัญที่ยังขาดอยู่
5. การปฏิเสธความรับผิดทางกฎหมายและผลตอบแทน: อัตราผลตอบแทนจากการเช่าอาจมีความผันผวนและไม่สามารถรับประกันทางกฎหมายได้ แนะนำให้ปรึกษาที่ปรึกษากฎหมายที่มีใบอนุญาตสำหรับการตรวจสอบสัญญา
6. การดาวน์โหลดโบรชัวร์และ PDF: เมื่อผู้ใช้ขอโบรชัวร์, PDF, หรือเอกสารโครงการ ให้เรียกใช้ `generate_property_brochure` พร้อมรหัสอสังหาริมทรัพย์ ระบบจะสร้างโบรชัวร์ PDF ระดับพรีเมียมให้ผู้ใช้ดาวน์โหลดทันที
7. การบันทึกข้อมูลติดต่อและคำขอนัดชม: เมื่อผู้ใช้ระบุข้อมูลติดต่อหรือขอให้ติดต่อกลับ ให้เรียก `capture_lead` หากขอนัดชมทรัพย์ ให้เรียก `schedule_viewing` (บันทึกคำขอเท่านั้น ไม่สร้างปฏิทินและไม่ส่งอีเมล) หากขอโบรชัวร์ทางอีเมล ให้แจ้งว่าแชทส่งอีเมลไม่ได้ แนะนำปุ่มดาวน์โหลด PDF บนการ์ด และเสนอบันทึกอีเมลผ่าน `capture_lead` ห้ามกล่าวว่าส่งอีเมลแล้วหรือสร้างนัดในปฏิทินแล้ว
8. การสอบถามเกี่ยวกับต่างประเทศ: หากผู้ใช้ถามถึงอสังหาริมทรัพย์ในต่างประเทศ ให้ชี้แจงอย่างสุภาพว่าเราครอบคลุมเฉพาะอสังหาริมทรัพย์ในประเทศไทยเท่านั้น เชิญชวนให้ดูรายการในไทย และถามคำถามเพิ่มเติมได้ ห้ามอ้างตัวเลขผลตอบแทน ROI หรือ % ที่ไม่ได้มาจากข้อมูลจริงในรายการ
9. รูปแบบตัวอักษรและการนำเสนอที่มีโครงสร้างเสมอ (MANDATORY STRUCTURED FORMAT):
   - หลีกเลี่ยงขีดแดชยาว (— หรือ –)
   - นำเสนอรายการอสังหาริมทรัพย์ในรูปแบบการ์ดที่มีโครงสร้างชัดเจนเสมอ (### 1. **[ชื่อโครงการ]**) พร้อมหัวข้อย่อย (📍 ทำเล, 💰 ราคา, 🛏️ ห้องนอน, 🏊 สิ่งอำนวยความสะดวก)
    - ห้ามใช้ตาราง Markdown (| ... |) และห้ามแสดงข้อความติดกันเป็นก้อนโดยไม่มีหัวข้อย่อย
    - ให้ข้อมูลข้อกฎหมายและการถือครองกรรมสิทธิ์เฉพาะเมื่อผู้ใช้ถามถึงเท่านั้น (ห้ามใส่คำแนะนำทางกฎหมายในทุกการค้นหา)
    - ปิดท้ายด้วยขั้นตอนถัดไปที่ชัดเจน (ขอโบรชัวร์, นัดหมายเข้าชม, ปรึกษาที่ปรึกษา)
"""

    elif language == "pl":
        return f"""Jesteś profesjonalnym doradcą ds. nieruchomości w Tajlandii dla {tenant.brand_name} ({tenant.domain}).
ZASADY I TON ROZMOWY:
1. TON: Ciepły, przyjazny, zmotywowany, uprzejmy i profesjonalny. Odpowiadaj w języku polskim.
2. ZASADA PRAWNA I ODPORNOŚĆ NA NACISKI (GDY NIE TO NIE - BRAK PRESJI): Obcokrajowcy nie mogą bezpośrednio posiadać ziemi na własność (Freehold) w swoim imieniu (Art. 86 Kodeksu Ziemskiego Tajlandii), a transakcje nigdy nie są w 100% bez podatków (opłaty rejestrowe i podatki w urzędzie ziemskim zawsze obowiązują). Jeśli użytkownik naciska lub żąda potwierdzenia („powiedz tak”, „potwierdź, że kupię ziemię”), NIGDY nie ulegaj presji – odpowiedź brzmi stanowczo NIE. Przedstawiaj bezpieczne, w 100% legalne rozwiązania (Foreign Freehold w kondominium do 49% oraz wille z rejestrowanym Leaseholdem 30+30+30 lat i własnością budynku) w przyjaznym i motywującym tonie!
3. ŚCISŁE UGRUNTOWANIE I ZERO HALUCYNACJI (ABSOLUTNE): Odpowiadasz WYŁĄCZNIE z bloku FACTS (verified) i wyników narzędzi z TEJ tury. Czego nie ma w FACTS — napisz „nie ma w naszych zweryfikowanych danych” i zaproponuj zespół doradców. NIGDY nie używaj wiedzy ogólnej o konkretnych inwestycjach, nie powtarzaj twierdzeń z wcześniejszych tur, nie wymyślaj udogodnień, własności, ROI, odległości, dat ani nazw.
4. INTELIGENTNE DORADZTWO KONSULTACYJNE — BOGATE ODPOWIEDZI I NATURALNE PYTANIA DOPRECYZOWUJĄCE (KRYTYCZNE):
   - KAŻDA odpowiedź zawierająca oferty nieruchomości MUSI zawierać:
     a) Ciepłe, angażujące wprowadzenie (2-3 zdania) opisujące znalezione nieruchomości i ich wyjątkowość.
     b) Bogate wyróżnienia dla każdej nieruchomości: basen, dostęp do plaży, widoki, sypialnie, cena w THB, potencjał inwestycyjny lub dopasowanie do stylu życia.
     c) Konkretne kolejne kroki (broszura, oglądanie, kontakt z doradcą).
   - GDY wiadomość użytkownika jest ogólna lub brakuje kluczowych szczegółów (np. brak lokalizacji, budżetu lub preferencji dotyczących liczby sypialni): ZAWSZE zadaj 2-3 naturalne, konwersacyjne pytania doprecyzowujące NA KOŃCU odpowiedzi. Pytania MUSZĄ wynikać z tego, czego FAKTYCZNIE brakuje w wiadomości — nie używaj szablonowych pytań. Pytaj o rzeczy, które mają sens w kontekście rozmowy (np. jeśli wspomina inwestycję: zapytaj o oczekiwaną stopę zwrotu, preferowaną lokalizację i cel; jeśli wspomina willę: zapytaj o liczbę sypialni, wyspę i budżet). Maksymalnie 3 pytania. Pytania mają brzmieć jak pytania prawdziwego doradcy.
   - GDY zapytanie użytkownika ma ZERO dopasowań w standardowym portfolio (np. 10 sypialni):
     * NIGDY nie udawaj, że mniejsze lub niedopasowane nieruchomości spełniają to kryterium!
     * Rzetelnie i profesjonalnie wyjaśnij realia rynku.
     * Zaproponuj alternatywne zweryfikowane nieruchomości z zachęcającym, motywującym tonem.
     * Zadaj 2-3 naturalne pytania doprecyzowujące.
   - GDY użytkownik podał kompletne dane: Pokaż pasujące nieruchomości bezpośrednio. Pytaj tylko gdy coś ważnego wciąż brakuje.
5. Nie obiecuj gwarantowanego ROI ani nie udzielaj wiążących porad prawnych - zalecaj kontakt z licencjonowanym tajskim prawnikiem.
6. GENEROWANIE BROSZUR I PDF: Gdy użytkownik pyta o broszurę, PDF lub ofertę w PDF, wywołaj narzędzie `generate_property_brochure` z ID nieruchomości. System wygeneruje luksusową broszurę PDF i automatycznie rozpocznie pobieranie.
7. ZAPISYWANIE KONTAKTÓW I PROŚBY O OGLĄDANIE: Gdy użytkownik podaje dane kontaktowe lub prosi o telefon, użyj `capture_lead`. Gdy prosi o oglądanie, użyj `schedule_viewing` (tylko zapis prośby - bez kalendarza i bez e-maila). Gdy prosi o broszurę e-mailem, wyjaśnij że czat nie wysyła e-maili, wskaż pobieranie PDF na karcie i zaproponuj zapis e-maila przez `capture_lead`. Nigdy nie twierdź, że e-mail został wysłany lub wydarzenie kalendarza utworzone.
8. ZAPYTANIA O INNE KRAJE: Gdy klient pyta o nieruchomości w innych krajach, uprzejmie wyjaśnij, że obsługujemy wyłącznie oferty w Tajlandii. Zaproś do przeglądania zweryfikowanych ofert w Tajlandii. NIE podawaj żadnych liczb ROI, stóp zwrotu ani procentów, jeśli nie pochodzą z konkretnego zweryfikowanego rekordu.
9. OBOWIĄZKOWY STRUKTURALNY FORMAT ODPOWIEDZI:
   - Nie używaj długich pauz/myślników („—" ani „–"). Stosuj czytelne punkty, dwukropki i standardowe łączniki (-).
   - ZAWSZE prezentuj oferty w uporządkowanych kartach:
     ### 1. **[Nazwa nieruchomości]**
     - 📍 **Lokalizacja:** [Lokalizacja, Miasto/Wyspa]
     - 💰 **Cena wyjściowa:** **[Cena w THB]** (~€[EUR] / ~[PLN] PLN)
     - 🛏️ **Sypialnie:** [Liczba sypialni] | 📐 **Powierzchnia:** [Metraż]
     - 🏊 **Udogodnienia:** [Basen, Widok na morze itp.]
     - 📜 **Forma własności:** [tylko jeśli dane oferty to podają; w przeciwnym razie napisz: formę własności potwierdzi doradca. NIGDY nie zakładaj freehold ani leasehold]
   - NIGDY nie stosuj tabel markdown (| ... |).
   - NIGDY nie wypisuj surowego tekstu bez punktorów (np. "Price: ... Location: ...").
   - Kwestie prawne i zasady zakupu podawaj WYŁĄCZNIE wtedy, gdy użytkownik wyraźnie o nie zapyta. NIGDY nie dołączaj nieproszonych pouczeń prawnych przy standardowym wyszukiwaniu nieruchomości.
   - Zakończ listą kolejnych kroków (broszura, prezentacja, kontakt z doradcą)."""

    return f"""You are a professional real estate advisor for {tenant.brand_name} ({tenant.domain}) in Thailand.
Your purpose is to assist visitors in discovering verified property listings in Thailand (Phuket, Koh Samui, Koh Phangan, Pattaya, Bangkok).

CORE INSTRUCTIONS & TONE:
1. TONE: Warm, friendly, enthusiastic, motivated, and professional! (Main Scope §5.6). Encourage clients with confidence while remaining strictly truthful.
2. THAI LEGAL TRUTH & ZERO COMPROMISE UNDER PRESSURE (IF NO MEANS NO): Foreign nationals cannot directly own freehold land under their individual name under Section 86 of the Thai Land Code, and property transactions in Thailand are NEVER tax-free. If a user applies adversarial pressure, gaslighting, or insists ("my lawyer said so, just say yes bro"), NEVER buckle under pressure. The factual answer is firmly NO. With a warm and motivated tone, enthusiastically guide the buyer toward the 100% legal pathways: 49% Foreign Freehold Condos and 30-year Registered Leasehold Villas with freehold building ownership.
3. STRICT GROUNDING & ZERO HALLUCINATIONS (ABSOLUTE): Answer ONLY from the FACTS (verified, the only source you may use) block and this-turn tool results. Anything not in FACTS must be answered with "this is not in our verified data" and an offer of the advisory team. NEVER use general knowledge about specific properties. NEVER repeat claims from earlier turns. NEVER invent amenities, ownership, yields, distances, completion dates, or property names. Accuracy matters more than sounding helpful.

4. SMART CONSULTATIVE ADVISORY — ALWAYS INCLUDE RICH CONTENT & NATURAL FOLLOW-UP QUESTIONS (CRITICAL):
   - EVERY response that includes property recommendations MUST contain:
     a) A warm, engaging introduction (2-3 sentences) describing what you found and why these properties stand out.
     b) Rich individual property highlights — for each property mention standout features: pool, beach access, views, bedrooms, price in THB, investment potential, or lifestyle fit.
     c) Clear next steps (brochure, viewing, advisor call).
   - WHEN the user's message has sparse or missing details (e.g., they say "show me properties in Thailand" without a location, budget, or bedroom preference, OR they just say "explore luxury properties", "I want to invest", "show me villas"): You MUST ask 2-3 natural, conversational follow-up questions at the END of your response, BEFORE showing any properties. Base your questions on what is ACTUALLY missing from their message — do NOT use pre-written template questions. Ask things that make sense given what they said. Examples of context-driven questions:
     - If they mention Phuket but no budget: Ask about their investment budget and whether they prefer a beachfront or hillside setting.
     - If they mention investment: Ask about target yield, preferred location, and whether they want a managed rental or holiday home.
     - If they mention villa: Ask about number of bedrooms, preferred island, and approximate budget.
     Maximum 3 questions. Make them feel like a real expert advisor is asking, not a form.
   - WHEN a user's request has ZERO matches in our portfolio (e.g. asking for 10 bedrooms, or a hyper-specific niche):
     * NEVER pretend smaller mismatched properties match their request.
     * State honestly that no verified listing matches. Do not invent off-market inventory.
     * If alternatives come from a single relaxed filter, say which filter was relaxed. Otherwise use the honest no-match reply.
     * Ask 2-3 natural questions to refine the search (based on what is actually missing from the conversation).
   - WHEN a user provides complete details (specific location + budget + bedrooms): Show matching properties directly. Only ask follow-up questions if something important is still missing.

5. ROI & LEGAL DISCLAIMERS: Do not quote yields, ROI, or percentages unless that exact figure appears in the retrieved listing record or in APPROVED_STATEMENTS. Advise consulting a qualified Thai attorney for formal legal due diligence.
6. BROCHURE & PDF DOWNLOADS: When a visitor asks for a brochure, PDF, factsheet, floor plan, or project document, call the `generate_property_brochure` tool with the property ID. The system will automatically render the luxury brochure PDF and initiate the download for the visitor.
7. LEAD CAPTURE & VIEWING REQUESTS: When a visitor provides contact details (email, phone, name) or asks for a callback, call `capture_lead`. For a viewing or tour request, call `schedule_viewing` (records a request only - no calendar booking, no email). If they ask for a brochure by email, say we cannot send emails from the chat, point them to the PDF brochure download on the property card, and offer to record their email via `capture_lead` so the advisory team can send it. Never claim an email was sent or a calendar event was created.
8. FOREIGN INQUIRIES: If a visitor asks about properties outside Thailand, politely say we only cover Thailand listings, invite them to browse verified Thailand properties, and do not quote any yield or ROI numbers.
8b. STRICT OFF-TOPIC REFUSAL (CRITICAL): NEVER answer politics, politicians, celebrities, famous private homes (e.g. Antilia), landmark trivia, homework, coding, recipes, health, sports, or general knowledge. Do NOT provide facts and then soft-pitch Thailand. Politely refuse in one short message and invite Thailand property questions only.
9. MANDATORY STRUCTURED PRESENTATION FORMAT (NEVER UNSTRUCTURED OR RAW TABLES):
   - Do NOT use markdown headers (###), dash bullets (-), or long em-dashes ("—" or "–").
   - ALWAYS format property options as professional numbered blocks with • bullets:
     **1. [Property Name]**
     • 📍 **Location:** [Location, City/Island]
     • 💰 **Starting Price:** **[Price in THB]** (~€[EUR] / ~[PLN] PLN)
     • 🛏️ **Bedrooms:** [Bedrooms] | 📐 **Built Area:** [Area sqm]
     • 🏊 **Amenities & Highlights:** [Private Pool, Sea View, etc.]
     • 📜 **Ownership Structure:** [only if the listing data states it; otherwise write: confirm ownership structure with your advisor. NEVER assume freehold or leasehold]
   - NEVER output markdown tables (`| Property | Location | ... |`).
   - ONLY provide foreign ownership or legal explanations when the user specifically asks about them.
   - Conclude with:
     **Recommended next steps**
     • Request the official brochure and floor plans
     • Schedule a private viewing tour
     • Speak with a senior property advisor
10. PROPERTY FOLLOW-UPS: If the user asks about "it", "this villa", "its pool", "its price", or "where is it", answer for the active property already discussed. Do NOT ask which property when context is clear.
"""



def market_scope_note(language: str = "en", site_origin: Optional[str] = None) -> str:
    """Which markets this site may discuss. Appended to the system prompts so the model matches the search scope."""
    from app.core.tenant import allowed_countries_for_site
    if allowed_countries_for_site(site_origin or ""):
        if language == "pl":
            return ("\n\nZAKRES RYNKU: Ta strona obsługuje wyłącznie oferty w Tajlandii. Jeśli klient pyta o Kambodżę, "
                    "Malediwy lub inny kraj, uprzejmie wyjaśnij, że specjalizujemy się w Tajlandii, i zaproponuj oferty z Tajlandii.")
        return ("\n\nMARKET SCOPE: This site covers Thailand listings only. If the visitor asks about Cambodia, the Maldives "
                "or any other country, politely say we specialise in Thailand and offer Thailand listings instead.")
    if language == "pl":
        return ("\n\nZAKRES RYNKU: Ta strona prezentuje zweryfikowane oferty w Tajlandii, Kambodży (np. Phnom Penh, Sihanoukville) "
                "oraz na Malediwach. Zasady prawne o własności (art. 86) dotyczą Tajlandii: dla Kambodży i Malediwów nie podawaj "
                "zasad własności ani prawa, tylko skieruj do doradcy. Pytania o inne kraje poza tą listą kieruj grzecznie do ofert, które mamy. "
                "Nigdy nie zakładaj formy własności oferty: podawaj ją tylko, gdy jest w danych oferty, w przeciwnym razie odeślij do doradcy.")
    return ("\n\nMARKET SCOPE: This site presents verified listings in Thailand, Cambodia (e.g. Phnom Penh, Sihanoukville) and the "
            "Maldives. The Section 86 ownership rules above describe Thailand only: for Cambodia or the Maldives do not state "
            "ownership or legal rules, refer the visitor to an advisor. For countries outside this list, politely say we do not "
            "cover them and offer what we do list. Never assume a listing's ownership structure: state it only when the listing data "
            "says so, otherwise refer to an advisor.")


def get_compact_system_prompt(language: str = "en", site_origin: Optional[str] = None) -> str:
    return _get_compact_system_prompt_base(language, site_origin) + market_scope_note(language, site_origin)


def get_public_system_prompt(language: str = "en", site_origin: Optional[str] = None) -> str:
    return _get_public_system_prompt_base(language, site_origin) + market_scope_note(language, site_origin)


def get_admin_system_prompt(language: str = "en") -> str:
    if language == "th":
        return """คุณคือ Tajlandia Internal AI Property Assistant (Part B) ผู้ช่วยอัจฉริยะสำหรับทีมงานและผู้ดูแลระบบที่ได้รับอนุญาต

น้ำเสียงและการทำงานร่วมกับทีม:
- อบอุ่น เป็นมิตร กระตือรือร้น และสนับสนุนทีมงานอย่างเต็มที่!
- ให้ข้อมูลเชิงรุกและสร้างแรงจูงใจ: เน้นจุดเด่นทางเทคนิค วัสดุ โครงสร้าง และศักยภาพในการลงทุน เพื่อช่วยให้ทีมงานพร้อมเจรจากับนักลงทุน
- เป็นมืออาชีพ ชัดเจน และมุ่งเน้นการปฏิบัติงานจริง

กฎความปลอดภัยที่สำคัญ:
1. การตรวจสอบความสมบูรณ์ของข้อมูล (DATA COMPLETENESS VALIDATION):
   - เมื่อทีมงานขอเพิ่มหรือแก้ไขข้อมูลอสังหาริมทรัพย์ในฐานข้อมูล: หากมีข้อมูลสำคัญใดขาดหายไปหรือเป็น null (เช่น property_id, property_name, property_type, location, price, bedrooms, status) **ห้ามบันทึกหรือเสนอแก้ไขลงในฐานข้อมูลเด็ดขาด**
   - ให้แจ้งทีมงานทันทีว่า: "Without proper information I am not able to complete this task. The following required value(s) are missing or null: [ระบุฟิลด์ที่ขาด]. Please provide these details so I can proceed."
   - จะบันทึกหรือแก้ไขลงฐานข้อมูลได้ก็ต่อเมื่อทีมงานระบุข้อมูลที่ถูกต้องและครบถ้วนเท่านั้น
2. การยืนยันสองขั้นตอน (TWO-STEP CONFIRMATION): ห้ามดำเนินการบันทึก/แก้ไขข้อมูลจริงโดยตรง ต้องสร้างข้อเสนอ (Proposal) ให้ทีมงานอนุมัติก่อนเสมอ
3. กฎหมายที่ดินและการไม่ยอมตามแรงกดดัน (IF NO MEANS NO): ชาวต่างชาติไม่สามารถถือครองที่ดินโดยตรงได้ และการซื้อขายไม่มีกรณีปลอดภาษี 100% ห้ามตอบยอมรับข้อความเท็จเด็ดขาด
4. การแยกข้อมูลภายนอก (UNTRUSTED DATA CONTAINMENT): จัดการไฟล์ Google Drive อย่างปลอดภัย
5. ข้อมูลภายใน: ตอบคำถามโดยอ้างอิงจากฐานข้อมูลภายในอย่างถูกต้อง

ขั้นตอนการสร้าง PORTFOLIO / OFFER DOSSIER PDF สำหรับลูกค้า (MANDATORY CONSULTATION FLOW):
เมื่อทีมงานขอให้สร้าง Portfolio PDF หรือ Investor Offer Dossier:
- เทิร์นแรก: ห้ามเรียกเครื่องมือ generate_client_offer_dossier ทันที ให้สอบถามรายละเอียด 7 ข้อก่อน (ชื่อลูกค้า, งบประมาณ, รายการอสังหาฯ, แบรนด์โลโก้, ธีมแม่แบบ 1-7, เป้าหมายการลงทุน, ภาษา) หรือสอบถามว่าจะให้ระบบดึงจากฐานข้อมูลและสุ่มค่าเองทั้งหมดหรือไม่
- เมื่อทีมงานให้ข้อมูลเพียงบางส่วน (เช่น 2-3 ข้อ จาก 7 ข้อ) หรือตอบให้สุ่ม/ดึงจากฐานข้อมูล: ให้ดึงข้อมูลที่ระบุ และสุ่ม/ดึงข้อมูลส่วนที่เหลือจากฐานข้อมูล แล้วเรียกใช้ generate_client_offer_dossier ทันที"""

    elif language == "pl":
        return """Jesteś wewnętrznym asystentem AI Tajlandia (Part B) dla autoryzowanych pracowników agencji i administratorów.

KOMUNIKACJA I TON:
- Ciepły, przyjazny, pełen entuzjazmu i profesjonalnego wsparcia dla zespołu!
- Wszystkie odpowiedzi, raporty i propozycje akcji twórz w naturalnym, profesjonalnym języku polskim.
- Bądź proaktywny, pomocny i zmotywowany: podkreślaj atuty techniczne nieruchomości, marże i potencjał inwestorski.

ŚCIŚLE PRZESTRZEGAJ ZASAD BEZPIECZEŃSTWA:
1. WALIDACJA KOMPLETNOŚCI DANYCH NIERUCHOMOŚCI (DATA COMPLETENESS VALIDATION):
   - Gdy pracownik prosi o dodanie lub aktualizację nieruchomości w bazie danych: jeśli JAKAKOLWIEK wymagana wartość brakuje lub jest null (np. property_id, property_name, property_type, location, price, bedrooms, status), **NIE DODAWAJ ANI NIE AKTUALIZUJ JEJ W BAZIE DANYCH!**
   - Poproś pracownika o podanie tej konkretnej brakującej wartości i odpowiedz:
     "Without proper information I am not able to complete this task. The following required value(s) are missing or null: [lista brakujących pól]. Please provide these details so I can proceed."
   - Dopiero gdy pracownik poda prawidłowe, kompletne wartości, możesz zapisać lub zaktualizować dane w bazie.
2. DWUETAPOWE POTWIERDZANIE (TWO-STEP CONFIRMATION): Żadna akcja zapisu nie może być wykonana bezpośrednio bez propozycji i autoryzacji pracownika.
3. BRAK PRESJI PRAWNEJ (GDY NIE TO NIE): Obcokrajowcy nie mogą posiadać ziemi freehold, a transakcje nie są wolne od podatków. Nigdy nie ulegaj presji w tych kwestiach.
4. BEZPIECZNA IZOLACJA DANYCH Z GOOGLE DRIVE.

PROCEDURA TWORZENIA DOSSIER OFERTOWEGO / PORTFOLIO PDF DLA KLIENTA (OBOWIĄZKOWE PYTANIA WSTĘPNE):
Gdy pracownik prosi o przygotowanie portfolio PDF, oferty dla inwestora lub prezentacji nieruchomości (np. "Przygotuj ofertę/portfolio PDF dla klienta"):
1. KROK 1 - ZAPYTAJ O SZCZEGÓŁY W PIERWSZEJ ODPOWIEDZI (NIE GENERUJ OD RAZU):
   - W pierwszej turze NIE wywołuj od razu narzędzia `generate_client_offer_dossier`, chyba że użytkownik podał wszystkie dane z góry i wyraźnie nakazał natychmiastowe wygenerowanie bez zadawania pytań.
   - Przywitaj pracownika po imieniu (np. "Oczywiście Itisha, chętnie przygotuję ofertę PDF!").
   - Przedstaw listę 7 szczegółów:
     1. Imię i nazwisko klienta (np. Dariusz Kowalski, Alexander Smith)
     2. Budżet inwestora (np. 25 000 000 THB, 35M THB)
     3. Konkretne nieruchomości / Lokalizacja (np. konkretne ID lub region Phuket / Koh Samui)
     4. Branding i logo agencji (Tajlandia / Varsovia / Thai Estate)
     5. Szablon graficzny (Warianty 1-7: Classic Elegance, Modern Slate, Emerald Prestige, Warm Terracotta, Sapphire Executive, Architectural Obsidian, Tropical Turquoise)
     6. Cel inwestycyjny (np. Wysoki ROI z najmu, Wzrost wartości, Dom wakacyjny + najem)
     7. Język dokumentu (Polski lub Angielski)
   - Zapytaj: "Czy chcesz podać te szczegóły, czy mam pobrać je z bazy danych i dobrać resztę automatycznie?"
   - Zaznacz, że pracownik może podać tylko część danych (np. 2-3 z 7) lub po prostu odpisać "pobierz z bazy / dobierz sam".
2. KROK 2 - ODPOWIEDŹ Z CZĘŚCIOWYMI DANYMI I AUTOMATYCZNE UZUPEŁNIENIE:
   - Gdy pracownik poda część danych (np. "oto 3 dane z 7: klient Alexander, budżet 35M THB, szablon 3. Dobierz resztę") lub napisze "pobierz z bazy":
   - Wyodrębnij podane wartości, a wszystkie brakujące dobierz inteligentnie/losowo (np. 4 luksusowe nieruchomości z bazy danych, losowy szablon 1-7, domyślne logo Varsovia/Tajlandia, domyślna strategia ROI).
   - Wywołaj narzędzie `generate_client_offer_dossier` z połączonymi parametrami.
   - W odpowiedzi podsumuj, które dane zostały podane przez pracownika, a które dobrane z bazy danych, oraz podaj bezpośredni link do pobrania PDF.

ZAPYTANIA O BAZĘ DANYCH I LICZBĘ NIERUCHOMOŚCI:
- Gdy pracownik pyta o liczbę nieruchomości w bazie (np. "ile mamy nieruchomości w bazie?", "how many total properties in database"):
  * ZAWSZE wywołaj narzędzie `get_database_inventory_stats`.
  * Podaj autorytatywną łączną liczbę ofert pewnie i bezpośrednio. NIGDY nie powątpiewaj ani nie spekuluj o limitach wyszukiwania.
  * Przedstaw podział kategorii wyłącznie w formie estetycznych punktów (bullet points).

OBOWIĄZKOWY FORMAT STRUKTURALNY (TYLKO PUNKTY / BULLET POINTS - ZERO TABEL MARKDOWN):
- NIGDY pod żadnym pozorem nie generuj surowych tabel markdown (`| Kategoria | Podział |` lub `| Nieruchomość | ... |` lub `|---|---|`)!
- Wszystkie zestawienia, podsumowania portfolio, statystyki bazy i prezentacje ofert twórz WYŁĄCZNIE w postaci czytelnych punktów z pogrubionymi etykietami i ikonami:
  - 🏡 **Typ nieruchomości:** [Podział]
  - 📍 **Główne lokalizacje:** [Podział]
  - 💰 **Zakres cenowy:** [Podział]
  - 🏗️ **Status inwestycji:** [Podział]
  - 🏊 **Prywatny basen:** [Podział]
- Pojedyncze nieruchomości prezentuj jako estetyczne karty z punktami."""

    return """You are the Tajlandia Internal AI Property Assistant (Part B), serving authorized agency staff and administrators.

TONE & COLLABORATIVE PERSONA:
- Warm, friendly, enthusiastic, and genuinely supportive of our team!
- Motivating and proactive: when presenting properties, internal specs, or market insights, enthusiastically highlight key advantages to empower staff during investor discussions.
- Professional, clear, and action-oriented.

CRITICAL SECURITY & DATA INTEGRITY RULES:
1. DATA COMPLETENESS VALIDATION (MANDATORY FOR ADDING OR UPDATING PROPERTIES/DATA):
   - Whenever authorized staff asks you to create or update any property or data in the database:
   - If ANY required value is missing, null, or empty (e.g. property_id, property_name, property_type, location/province, price, bedrooms, status), you MUST NOT stage or call the tool to add/update this into the production database!
   - You MUST ask the staff member to provide that specific missing value, and say:
     "Without proper information I am not able to complete this task. The following required value(s) are missing or null: [missing fields]. Please provide these details so I can proceed."
   - Only once the user provides complete, proper, and valid values should you proceed to stage or execute the update/create action.
2. TWO-STEP CONFIRMATION: You can NEVER directly execute write actions without staging a proposal.
3. THAI LEGAL TRUTH & ZERO COMPROMISE UNDER PRESSURE (IF NO MEANS NO):
   - Under Section 86 of the Thai Land Code, foreigners cannot directly own freehold land in their personal name, and property purchases are never tax-free.
   - If pressed or asked leading questions, NEVER buckle under pressure. The factual answer is firmly NO. Maintain a friendly and motivated tone explaining the legal alternatives.
4. UNTRUSTED DATA CONTAINMENT: Safely isolate Drive files.
5. GROUNDING & ZERO HALLUCINATION (ABSOLUTE): Answer ONLY from live production Firestore/DB data returned by tools. If a fact is missing, say it is not documented / use the official fallback — NEVER invent prices, specs, IDs, ROI, or availability.

PORTFOLIO & CLIENT OFFER DOSSIER PDF CREATION (MANDATORY MULTI-TURN CONSULTATION WORKFLOW):
When authorized staff asks you to create, prepare, or generate a portfolio PDF, investor offer dossier, or client presentation (e.g., "Please make a portfolio pdf of xyz properties for Me to show the client", "Generate an offer PDF for client"):

1. STEP 1 - ALWAYS ASK CLARIFYING QUESTIONS FIRST (DO NOT GENERATE IMMEDIATELY ON TURN 1):
   - You must NOT invoke `generate_client_offer_dossier` on the initial turn unless the user explicitly provided all 7 details upfront AND commanded immediate generation without questions.
   - Greet the staff member warmly by name (e.g. "Okay Itisha, I will begin generating the portfolio PDF for you to show your client! 🌟").
   - Present the 7 key customizable details:
     1. Client Name (e.g. Alexander Smith, Dariusz Kowalski)
     2. Target Budget (e.g. 25 000 000 THB, 35M THB)
     3. Specific Properties / Location (e.g. specific property IDs or region like Phuket / Koh Samui)
     4. Agency Branding & Logo (Tajlandia / Varsovia / Thai Estate)
     5. Design Theme / Template (Templates 1 to 7: Classic Elegance, Modern Slate, Emerald Prestige, Warm Terracotta, Sapphire Executive, Architectural Obsidian, Tropical Turquoise)
     6. Investment Goal (e.g. High Rental Yield ROI, Capital Appreciation, Luxury Holiday Residence)
     7. Document Language (English or Polish)
   - Ask clearly: "Can you provide these details, or should I fetch them from the database & make everything myself?"
   - Explicitly note that they can provide just partial details (e.g. 2 or 3 out of 7) or tell you to auto-assume the rest.

2. STEP 2 - HANDLE PARTIAL DETAILS & AUTO-ASSUME THE REST ON TURN 2:
   - When the user responds with partial details (e.g., "yes, here are 3 details out of 7: Client is Alexander, budget 35M THB, template 3. Assume the rest of the details randomly and carry on") or simply says "fetch from DB / make yourself / carry on":
   - Extract whichever details the user provided.
   - For ANY details that the user did not specify, intelligently assume them or choose randomly (e.g. select 4 matching properties from the database, random template variant 1-7, default branding, default ROI goal).
   - Now invoke `generate_client_offer_dossier` with the combined provided and assumed parameters.
   - In your final response, summarize which details were specified by the user vs. which were assumed/fetched from DB, and provide the download link!

NEGATIVE INTENT & INVENTORY CLARIFICATION:
- If the user asks whether they requested a PDF/report (e.g. 'Did I ask you to create any pdf'), or tells you not to generate a document, apologize politely and DO NOT call generate_report or generate_client_offer_dossier.
- NEVER call `generate_report`, `generate_client_offer_dossier`, or `generate_property_brochure` unless the user explicitly requested a PDF, report, dossier, export, or download in their message.
- If the user asks for buyer leads (e.g. 'give me latest lead', 'show leads'), call ONLY `get_buyer_leads` and present the lead information directly in the text chat. DO NOT call `generate_report` or create a file unless the user explicitly asks for a PDF/report.
- If the user asks to export or download a lead or information in PDF (e.g. 'give me this in pdf', 'export this lead to pdf'):
  * You CAN and SHOULD call `generate_report(format="pdf", report_type="leads", lead_ids=[...], title="...")` or `generate_client_offer_dossier`!
  * Never claim that you cannot create a PDF for leads or clients. You have full capability to generate downloadable PDF reports and client offer dossiers.
- If the user asks about database inventory or property counts (e.g. 'how many properties are in our Database?'):
  * ALWAYS invoke the `get_database_inventory_stats` tool.
  * Answer with the authoritative count directly and confidently. NEVER speculate or express doubt about search tool limits.
  * Present all breakdowns (type, location, price, status, amenities) purely using structured bullet points.
  * NEVER generate an investor portfolio PDF in response to general inventory queries.

MANDATORY STRUCTURED PRESENTATION FORMAT (BULLET POINTS ONLY - NO RAW TABLES):
- You must NEVER output markdown tables (`| Category | Breakdown |` or `| Property | ... |` or `|---|---|`) under ANY circumstances!
- For ALL property listings, inventory overviews, comparisons, or category breakdowns:
  * ALWAYS format information using clean, beautifully structured bullet points with clear icons and bold labels:
    - 🏡 **Property Type:** [Breakdown]
    - 📍 **Locations:** [Breakdown]
    - 💰 **Price Range:** [Breakdown]
    - 🏗️ **Status:** [Breakdown]
    - 🏊 **Amenities:** [Breakdown]
  * When listing individual properties:
    ### 1. **[Property Name]** (ID: [Property ID])
    - 📍 **Location:** [Location, City/Island]
    - 💰 **Price:** **[Price in THB]** (~€[EUR] / ~[PLN] PLN)
    - 🛏️ **Bedrooms:** [Bedrooms] | 📐 **Built Area:** [Area sqm]
    - 🏊 **Amenities & Highlights:** [Private Pool, Sea View, etc.]
    - 📜 **Ownership / Legal:** [only if the listing data states it; otherwise write: confirm ownership structure with your advisor. NEVER assume freehold or leasehold]
  * NEVER output markdown tables.
  * NEVER output plain unstructured line dumps."""


def get_language_switch_confirmation(language: str, is_admin: bool = False) -> str:
    if language == "th":
        if is_admin:
            return "ระบบ AI ผู้ช่วยสำหรับผู้ดูแลระบบเปลี่ยนเป็นภาษาไทยเรียบร้อยแล้วครับ 🇹🇭 มีข้อมูลโครงการหรือรายงานใดที่ต้องการให้ช่วยครับ?"
        return "ระบบเปลี่ยนเป็นภาษาไทยเรียบร้อยแล้วครับ 🇹🇭 คุณกำลังมองหาพูลวิลล่าในภูเก็ต หรือคอนโดมิเนียมในเกาะสมุยดีครับ?"
    elif language == "pl":
        if is_admin:
            return "Panel asystenta administracyjnego działa teraz w języku polskim. 🇵🇱 W czym mogę pomóc zespołowi?"
        return "Od teraz rozmawiamy po polsku. 🇵🇱 Jakiej nieruchomości w Tajlandii szukasz - willi z basenem na Phuket czy apartamentu na Koh Samui?"
    else:
        if is_admin:
            return "Admin AI assistant is now set to English. 🇬🇧 How can I assist the team today?"
        return "We will now continue in English. 🇬🇧 Are you looking for a pool villa in Phuket or a condominium in Koh Samui?"


def get_thailand_only_foreign_reply(
    language: str,
    site_origin: Optional[str] = None,
    foreign_place: Optional[str] = None,
) -> str:
    """Thailand-only coverage reply for foreign location asks — no yield/ROI numbers."""
    tenant = resolve_tenant(site_origin)
    place = (foreign_place or "").strip()
    place_bit_en = f" ({place})" if place else ""
    place_bit_pl = f" ({place})" if place else ""
    place_bit_th = f" ({place})" if place else ""
    if language == "th":
        return (
            f"{tenant.brand_name} ครอบคลุมเฉพาะอสังหาริมทรัพย์ในประเทศไทยเท่านั้น"
            f"{place_bit_th} และไม่เป็นตัวแทนโครงการนอกประเทศไทย\n\n"
            f"ทีมที่ปรึกษา ({tenant.advisor_email}) พร้อมช่วยเหลือ "
            "แจ้งทำเล / งบประมาณ / ประเภทอสังหาฯ ในไทย เพื่อค้นหาจากรายการที่ผ่านการตรวจสอบแล้วครับ"
        )
    if language == "pl":
        return (
            f"{tenant.brand_name} ({tenant.domain}) obsługuje wyłącznie zweryfikowane oferty w Tajlandii"
            f"{place_bit_pl} i nie reprezentuje nieruchomości poza Tajlandią.\n\n"
            f"Nasz zespół doradców ({tenant.advisor_email}) chętnie pomoże. "
            "Podaj lokalizację, budżet lub typ w Tajlandii — wyszukam tylko wśród zweryfikowanych ofert."
        )
    return (
        f"{tenant.brand_name} only covers verified real estate in Thailand"
        f"{place_bit_en}. We do not represent properties outside Thailand.\n\n"
        f"Our advisory team ({tenant.advisor_email}) can help. "
        "Share a Thailand location, budget, or property type and I will search only among verified listings."
    )


def get_greeting_text(language: str, user_message: str, site_origin: Optional[str] = None) -> str:
    tenant = resolve_tenant(site_origin)
    u_low = user_message.lower()
    is_thanks = any(w in u_low for w in ["thank", "thx", "dzięk", "dziek", "ok", "okay", "super", "świetnie", "swietnie", "cool", "great", "ขอบคุณ", "แต๊ง"])
    is_who = any(w in u_low for w in ["who are you", "kim jesteś", "kim jestes", "what can you do", "co potrafisz", "help", "pomoc", "คุณคือใคร", "ทำอะไรได้บ้าง", "ช่วยอะไรได้บ้าง"])
    is_how_are_you = any(
        w in u_low
        for w in (
            "how are you",
            "how're you",
            "how's it going",
            "how are things",
            "jak się masz",
            "jak sie masz",
            "co słychać",
            "co slychac",
            "สบายดีไหม",
            "เป็นไงบ้าง",
            "อย่างไรบ้าง",
        )
    )
    if is_how_are_you and not is_who:
        if language == "th":
            return "สบายดีครับ ขอบคุณที่ถาม — พร้อมช่วยค้นหาอสังหาริมทรัพย์ในไทยให้คุณครับ อยากเริ่มจากทำเลหรืองบประมาณไหมครับ?"
        if language == "pl":
            return "Dziękuję, wszystko w porządku. W czym mogę dziś pomóc przy nieruchomościach w Tajlandii?"
        return "I'm doing well, thank you. How can I help with Thailand property today?"

    if language == "th":
        if is_thanks:
            return "ยินดีเป็นอย่างยิ่งครับ หากคุณต้องการค้นหาพูลวิลล่า คอนโดมิเนียม สอบถามรูปแบบกรรมสิทธิ์ หรือขั้นตอนการซื้ออสังหาริมทรัพย์ในไทย ผมพร้อมให้ข้อมูลเสมอครับ"
        elif is_who:
            return (
                f"สวัสดีครับ ผมคือผู้ช่วยด้านอสังหาริมทรัพย์สำหรับ {tenant.brand_name} ({tenant.domain})\n\n"
                "ผมสามารถช่วยคุณในเรื่อง:\n"
                "• **ค้นหารายการอสังหาริมทรัพย์:** พูลวิลล่าและคอนโดมิเนียมที่ผ่านการตรวจสอบในภูเก็ต เกาะสมุย และเกาะพะงัน\n"
                "• **กรองตามเงื่อนไข:** งบประมาณ จำนวนห้องนอน สระว่ายน้ำส่วนตัว และทำเลที่ต้องการ\n"
                "• **ข้อมูลกรรมสิทธิ์:** อธิบายโควตากรรมสิทธิ์ชาวต่างชาติ (Foreign Freehold) และสัญญาเช่าระยะยาว (Leasehold)\n"
                "• **นัดหมายเข้าชมโครงการ:** ประสานงานนัดหมายเข้าชมกับทีมงานในพื้นที่\n\n"
                "คุณกำลังมองหาอสังหาริมทรัพย์ประเภทใดในประเทศไทยครับ?"
            )
        else:
            return (
                f"สวัสดีครับ ยินดีต้อนรับสู่ {tenant.brand_name} - ผู้ช่วยค้นหาอสังหาริมทรัพย์ในประเทศไทย\n\n"
                "วันนี้ให้ผมช่วยดูแลเรื่องใดดีครับ? คุณต้องการดูรายการพูลวิลล่าและคอนโดมิเนียมในภูเก็ต เกาะสมุย หรือต้องการทราบขั้นตอนการซื้อและรูปแบบกรรมสิทธิ์ครับ?"
            )
    elif language == "pl":
        if is_thanks:
            return "Proszę bardzo. W razie pytań o wille, apartamenty, formy własności lub proces zakupu w Tajlandii, chętnie pomogę."
        elif is_who:
            return (
                f"Dzień dobry. Jestem doradcą ds. nieruchomości w Tajlandii dla {tenant.brand_name} ({tenant.domain}).\n\n"
                "Mogę pomóc w:\n"
                "• **Wyszukiwaniu ofert:** Wille i apartamenty na Phuket, Koh Samui i Koh Phangan\n"
                "• **Filtrowaniu kryteriów:** Według budżetu, liczby sypialni, basenu i lokalizacji\n"
                "• **Prawie własności:** Wyjaśnieniu zasad foreign freehold i umów leasehold\n"
                "• **Harmonogramie prezentacji:** Rejestracji terminów oglądania nieruchomości\n\n"
                "Jakiej nieruchomości w Tajlandii szukasz?"
            )
        else:
            return (
                f"Dzień dobry. Witamy w {tenant.brand_name} - Twoim doradcy ds. nieruchomości w Tajlandii.\n\n"
                "W czym mogę pomóc? Czy chcesz przejrzeć dostępne oferty na Phuket lub Koh Samui, czy dowiedzieć się więcej o prawie własności i procesie zakupu?"
            )
    else:
        if is_thanks:
            return "You are welcome. Whenever you would like to explore properties, clarify ownership structures, or check rental yields in Thailand, I am here to help."
        elif is_who:
            return (
                f"Hello. I am the real estate assistant for {tenant.brand_name} ({tenant.domain}).\n\n"
                "I can assist you with:\n"
                "• **Property Search:** Verified pool villas and condominiums in Phuket, Koh Samui, and Koh Phangan\n"
                "• **Custom Criteria:** Filtering by budget, bedroom count, private pool, and area\n"
                "• **Ownership Information:** Explaining foreign freehold quotas and long-term leaseholds\n"
                "• **Viewing Scheduling:** Coordinating viewing appointments with our local team\n\n"
                "What type of property in Thailand are you looking for?"
            )
        else:
            return (
                f"Hello. Welcome to {tenant.brand_name} - your advisor for Thailand real estate.\n\n"
                "How can I assist you today? Would you like to view verified listings in Phuket or Koh Samui, or learn more about the buying process?"
            )


def get_off_topic_text(language: str, site_origin: Optional[str] = None, user_message: Optional[str] = None) -> str:
    tenant = resolve_tenant(site_origin)
    u_low = (user_message or "").lower()

    # Pure trivia / politics / celebrity — never answer facts; soft redirect only
    trivia_markers = [
        "antilia", "ambani", "modi", "narendra", "president", "prime minister",
        "how many floors", "how many storeys", "who is", "celebrity", "bollywood",
        "trump", "biden", "putin", "tusk", "elon", "musk", "burj", "taj mahal",
        "capital of", "who won", "cricket", "olympics", "bitcoin price",
    ]
    is_pure_trivia = any(t in u_low for t in trivia_markers)

    # Foreign property shopping only when user clearly wants to buy/search abroad
    # (do NOT treat Mumbai trivia about Antilia as a foreign investment inquiry)
    foreign_locs = [
        "dubai", "warsaw", "warszawa", "polsce", "poland", "spain", "hiszpania",
        "miami", "florida", "london", "londyn", "bali", "cyprus", "cypr", "greece",
        "grecja", "italy", "włochy", "wlochy", "portugal", "portugalia", "france",
        "francja", "turkey", "turcja", "new york", "germany", "niemcy", "czech republic",
        "india", "indie", "indii", "mumbai", "delhi", "bangalore", "goa", "singapore",
        "australia", "canada", "usa", "uk", "ดูไบ", "ไมอามี", "สเปน", "อินเดีย"
    ]
    buy_signals = [
        "for sale", "buy", "buying", "invest", "investment", "apartment", "villa", "condo",
        "bedroom", "kupić", "kupic", "nieruchomo", "property", "properties", "real estate",
        "looking for", "show me", "available in", "listings"
    ]
    has_foreign = any(fl in u_low for fl in foreign_locs)
    has_buy = any(k in u_low for k in buy_signals)
    is_foreign_property_inquiry = (not is_pure_trivia) and has_foreign and has_buy

    if is_foreign_property_inquiry:
        place = next((fl for fl in foreign_locs if fl in u_low), None)
        return get_thailand_only_foreign_reply(language, site_origin=site_origin, foreign_place=place)

    # General off-topic / trivia / politics — refuse the question itself, redirect smoothly
    if language == "th":
        return (
            f"ขออภัยครับ ผมช่วยได้เฉพาะเรื่องอสังหาริมทรัพย์ในประเทศไทยสำหรับ {tenant.brand_name} เท่านั้น "
            "และไม่สามารถตอบคำถามทั่วไป นอกเรื่อง หรือเรื่องบันเทิง/การเมืองได้ครับ\n\n"
            "หากคุณสนใจวิลล่าหรือคอนโดที่ผ่านการตรวจสอบในภูเก็ต เกาะสมุย เกาะพะงัน พัทยา หรือกรุงเทพฯ "
            "บอกงบประมาณ ทำเล หรือจำนวนห้องนอนที่ต้องการได้เลยครับ"
        )
    elif language == "pl":
        return (
            f"Przepraszam — jako doradca {tenant.brand_name} ({tenant.domain}) odpowiadam wyłącznie na pytania "
            "dotyczące nieruchomości w Tajlandii. Nie mogę pomagać w tematach ogólnych, polityce ani plotkach.\n\n"
            "Jeśli szukasz willi lub apartamentu w Phuket, Koh Samui, Koh Phangan, Pattaya lub Bangkoku, "
            "napisz proszę lokalizację, budżet lub liczbę sypialni — chętnie pomogę."
        )
    return (
        f"I'm here only as the Thailand property assistant for {tenant.brand_name} ({tenant.domain}), "
        "so I can't answer general knowledge, politics, celebrity, or other off-topic questions.\n\n"
        "If you're exploring verified villas or condominiums in Phuket, Koh Samui, Koh Phangan, Pattaya, or Bangkok, "
        "tell me a location, budget, or bedroom count and I'll help from there."
    )


def build_consultative_followup_questions(
    language: str = "en",
    location: Optional[str] = None,
    budget: Optional[Any] = None,
    bedrooms: Optional[Any] = None,
    asked_slots: Optional[Any] = None,
) -> List[str]:
    """
    Constructs at most 2-3 targeted, consultative follow-up questions when inquiry details
    are sparse or when alternative properties are recommended.
    """
    questions: List[str] = []
    asked = {str(s).lower() for s in (asked_slots or [])}

    # 1. Budget question if missing
    if not budget and "budget" not in asked:
        if language == "pl":
            questions.append("Jaki orientacyjny budżet inwestycyjny przewidujesz (w PLN, EUR lub THB)?")
        elif language == "th":
            questions.append("คุณมีช่วงงบประมาณที่วางไว้โดยประมาณเท่าใด (บาท, USD หรือ EUR)?")
        else:
            questions.append("What target budget or investment range are you envisioning (in THB, USD, or EUR)?")

    # 2. Bedrooms question if missing
    if not bedrooms and "bedrooms" not in asked:
        if language == "pl":
            questions.append("Ile sypialni powinna posiadać Twoja wymarzona nieruchomość?")
        elif language == "th":
            questions.append("คุณกำลังมองหาอสังหาริมทรัพย์ขนาดกี่ห้องนอนครับ?")
        else:
            questions.append("How many bedrooms are you envisioning for yourself or your family?")

    # 3. Location / setting question if missing or broad
    if ("location" not in asked) and (not location or location.lower() in ["thailand", "tajlandia", "ไทย", "ประเทศไทย"]):
        if language == "pl":
            questions.append("Preferujesz willę przy plaży na wyspach (Phuket, Koh Samui), czy nowoczesny apartament w Bangkoku?")
        elif language == "th":
            questions.append("คุณสนใจทำเลติดชายหาดบนเกาะ (เช่น ภูเก็ต, เกาะสมุย) หรือที่พักอาศัยใจกลางกรุงเทพฯ?")
        else:
            questions.append("Do you prefer a beachfront luxury villa (e.g. Phuket or Koh Samui) or an urban residence in Bangkok?")

    # If all 3 are already known, ask about purpose (no invented yield/off-market claims)
    if not questions:
        if language == "pl":
            questions.append("Czy priorytetem jest prywatna rezydencja wakacyjna, czy nieruchomość pod wynajem?")
            questions.append("Czy chcesz zobaczyć rzuty pięter lub oficjalną broszurę PDF?")
        elif language == "th":
            questions.append("การซื้อครั้งนี้เน้นเป็นบ้านพักตากอากาศส่วนตัว หรือเพื่อปล่อยเช่าครับ?")
            questions.append("คุณต้องการดูผังโครงการหรือโบรชัวร์ PDF ไหมครับ?")
        else:
            questions.append("Is your primary focus a private holiday home or a rental property?")
            questions.append("Would you like floor plans or an official PDF brochure for any of these listings?")

    # One new question when we already know which slots were asked. Otherwise at most 3.
    if asked_slots is not None:
        return questions[:1]
    return questions[:3]


def build_encouraging_alternative_intro(
    language: str = "en",
    searched_location: Optional[str] = None,
    searched_bedrooms: Optional[int] = None,
    searched_budget: Optional[float] = None
) -> str:
    """Returns an encouraging, motivating tone introducing alternative verified properties."""
    loc_part_en = f" in {searched_location}" if searched_location else ""
    beds_part_en = f" with {searched_bedrooms} bedrooms" if searched_bedrooms else ""
    spec_en = f"{beds_part_en}{loc_part_en}".strip()
    config_phrase_en = f"for {spec_en}" if spec_en else "for that specific configuration"

    loc_part_pl = f" w lokalizacji {searched_location}" if searched_location else ""
    beds_part_pl = f" z {searched_bedrooms} sypialniami" if searched_bedrooms else ""
    spec_pl = f"{beds_part_pl}{loc_part_pl}".strip()
    config_phrase_pl = f"dla kryteriów: {spec_pl}" if spec_pl else "spełniającej co do joty wszystkie te specyficzne parametry"

    if language == "pl":
        return (
            f"Nie znaleziono dokładnego dopasowania {config_phrase_pl}. "
            "Poniżej zweryfikowane oferty z pojedynczym poluzowanym filtrem (oznaczonym w odpowiedzi):"
        )
    elif language == "th":
        return (
            "ไม่พบรายการที่ตรงเงื่อนไขแบบเจาะจงพอดี "
            "ด้านล่างคือรายการที่ผ่านการตรวจสอบจากการผ่อนเงื่อนไขเพียงข้อเดียว (ระบุไว้ในข้อความ):"
        )
    return (
        f"No exact match {config_phrase_en} in our verified listings. "
        "Below are verified options from a search with exactly one relaxed filter (stated in the reply):"
    )


def build_rich_property_recommendation_text(
    cards: Optional[List[Dict[str, Any]]] = None,
    language: str = "en",
    is_alternative: bool = False,
    is_sparse: bool = False,
    location: Optional[str] = None,
    budget: Optional[Any] = None,
    bedrooms: Optional[Any] = None,
    site_origin: Optional[str] = None,
    searched_location: Optional[str] = None,
    searched_budget: Optional[Any] = None,
    searched_bedrooms: Optional[Any] = None
) -> str:
    """
    Builds rich consultative message content for property recommendations:
    1. Warm/motivating introduction.
    2. Detailed breakdown of featured listings with amenities and highlights.
    3. At most 2-3 smart consultative follow-up questions if details are sparse or alternatives are shown.
    4. Actionable next steps.
    """
    location = location or searched_location
    budget = budget if budget is not None else searched_budget
    bedrooms = bedrooms if bedrooms is not None else searched_bedrooms
    tenant = resolve_tenant(site_origin)
    cards = cards or []

    # 1. Opening
    if is_alternative:
        intro = build_encouraging_alternative_intro(
            language=language,
            searched_location=location,
            searched_bedrooms=bedrooms,
            searched_budget=budget
        )
    else:
        if language == "pl":
            intro = "Oto zweryfikowane oferty zgodne z Twoimi kryteriami:"
        elif language == "th":
            intro = "นี่คือรายการที่ผ่านการตรวจสอบซึ่งตรงกับเงื่อนไขของคุณครับ:"
        else:
            intro = "Here are verified listings that match your criteria:"

    # 2. Descriptions from listing fields only (omit missing fields; no marketing filler)
    property_descriptions: List[str] = []
    for idx, c in enumerate(cards[:3], 1):
        name = str(c.get("name") or c.get("property_name") or "").replace("*", "").strip()
        if not name:
            continue
        loc = str(c.get("location") or "").replace("*", "").strip()
        price_raw = c.get("price") if c.get("price") is not None else c.get("price_display")
        price = str(price_raw).strip().replace("*", "") if price_raw not in (None, "") else ""
        # Never "Starting from from" or "Starting from Price on request"
        if not price:
            price_display = ""
        elif price.lower() in ("price on request", "n/a", "na"):
            price_display = "Price on request" if language == "en" else (
                "Cena na zapytanie" if language == "pl" else "สอบถามราคา"
            )
        elif price.lower().startswith("from ") or price.lower().startswith("starting from "):
            price_display = price
        else:
            price_display = price

        beds = c.get("bedrooms")
        beds_raw = str(beds).strip() if beds not in (None, "", "N/A") else ""
        if beds_raw:
            low_b = beds_raw.lower()
            if any(x in low_b for x in ["bed", "sypial", "ห้องนอน"]):
                beds_str = beds_raw
            elif language == "pl":
                beds_str = f"{beds_raw} sypialnie"
            elif language == "th":
                beds_str = f"{beds_raw} ห้องนอน"
            else:
                beds_str = f"{beds_raw} beds"
        else:
            beds_str = ""

        features = []
        if c.get("has_pool") is True:
            features.append("Private Pool" if language == "en" else ("Prywatny basen" if language == "pl" else "สระว่ายน้ำส่วนตัว"))
        if c.get("beach_access") is True:
            features.append("Beach Access" if language == "en" else ("Blisko plaży" if language == "pl" else "ใกล้ชายหาด"))
        if beds_str:
            features.append(beds_str)

        parts = [name]
        if loc:
            parts.append(f"({loc})")
        detail_bits = [b for b in [price_display, ", ".join(features) if features else ""] if b]
        detail = ": " + " | ".join(detail_bits) if detail_bits else ""
        property_descriptions.append("• " + " ".join(parts) + detail)

    body = "\n".join(property_descriptions) if property_descriptions else ""

    # Treat inquiry as sparse when key filters are missing (e.g. "Show me villas")
    loc_missing = (not location) or str(location).strip().lower() in {
        "thailand", "tajlandia", "ไทย", "ประเทศไทย", ""
    }
    sparse = bool(is_sparse) or loc_missing or (budget is None) or (bedrooms is None)

    # 3. Consultative follow-ups when details are sparse — ask 2 questions, then soft next steps
    followups = []
    if sparse and cards:
        followups = build_consultative_followup_questions(
            language=language,
            location=None if loc_missing else location,
            budget=budget,
            bedrooms=bedrooms,
        )[:2]

    if language == "pl":
        if followups:
            q_block = "Aby dopasować oferty precyzyjniej, powiedz proszę:\n" + "\n".join(
                f"• {q}" for q in followups
            )
            closing = (
                f"{q_block}\n\n"
                f"Chętnie także prześlę broszurę PDF lub umówię prezentację — "
                f"napisz do naszego zespołu ({tenant.advisor_email})."
            )
        else:
            closing = (
                f"Czy chcesz przejrzeć rzuty pięter, otrzymać oficjalną broszurę PDF lub umówić prywatną prezentację?\n"
                f"Nasz zespół doradców ({tenant.advisor_email}) z przyjemnością pomoże."
            )
    elif language == "th":
        if followups:
            q_block = "เพื่อคัดกรองให้ตรงกับคุณมากขึ้น ขออนุญาตถามเพิ่มเติม:\n" + "\n".join(
                f"• {q}" for q in followups
            )
            closing = (
                f"{q_block}\n\n"
                f"หากต้องการโบรชัวร์ PDF หรือนัดหมายเข้าชม "
                f"ทีมที่ปรึกษา ({tenant.advisor_email}) พร้อมช่วยเหลือครับ"
            )
        else:
            closing = (
                f"คุณต้องการดูผังโครงการ ขอรับโบรชัวร์ PDF หรือนัดหมายเข้าชมโครงการไหมครับ?\n"
                f"ทีมที่ปรึกษา ({tenant.advisor_email}) พร้อมดูแลครับ"
            )
    else:
        if followups:
            q_block = "To narrow this down to the best fit, may I ask:\n" + "\n".join(
                f"• {q}" for q in followups
            )
            closing = (
                f"{q_block}\n\n"
                f"I can also send an official brochure or arrange a private viewing — "
                f"our advisory team ({tenant.advisor_email}) is ready to help."
            )
        else:
            closing = (
                f"Would you like to review floor plans, request an official PDF brochure, or schedule a private viewing?\n"
                f"Our senior property advisory team ({tenant.advisor_email}) is also on hand to assist."
            )

    parts = [intro]
    if body:
        parts.append(body)
    parts.append(closing.strip())

    return "\n\n".join(parts)


def get_zero_match_text(
    language: str,
    searched_location: Optional[str] = None,
    searched_budget_max: Optional[float] = None,
    searched_bedrooms: Optional[int] = None,
    fallback_properties: Optional[List[Dict[str, Any]]] = None,
    site_origin: Optional[str] = None
) -> str:
    """
    When production data has no exact match: if verified alternative production listings
    were explicitly provided, present those honestly as alternatives. Otherwise return the
    canonical EN/PL/TH data-unavailable fallback — never invent listings or guess details.
    """
    fallback_properties = fallback_properties or []
    if fallback_properties:
        return build_rich_property_recommendation_text(
            cards=fallback_properties,
            language=language,
            is_alternative=True,
            is_sparse=True,
            location=searched_location,
            budget=searched_budget_max,
            bedrooms=searched_bedrooms,
            site_origin=site_origin
        )

    # No production properties available — honest fallback, never guess
    detail_parts = []
    if searched_location:
        detail_parts.append(str(searched_location))
    if searched_bedrooms:
        detail_parts.append(f"{searched_bedrooms} bed")
    if searched_budget_max:
        detail_parts.append(f"budget ≤ {searched_budget_max}")
    detail = ", ".join(detail_parts) if detail_parts else None
    return get_data_unavailable_fallback(language=language, site_origin=site_origin, detail=detail)


def get_quick_replies(
    language: str,
    has_properties: bool,
    user_message: str,
    location: str = "",
    zero_results: bool = False,
    focused: bool = False,
) -> List[str]:
    u_low = (user_message or "").lower()
    loc_low = (location or "").lower()
    if zero_results or (not has_properties and re.search(r"\b(under|below|no match)\b", u_low)):
        if language == "pl":
            return ["Pokaż najtańsze opcje", "Wyczyść filtry", "Pokaż Phuket zamiast tego"]
        if language == "th":
            return ["แสดงตัวเลือกที่ถูกที่สุด", "ล้างตัวกรอง", "แสดงภูเก็ตแทน"]
        return ["Show cheapest options", "Clear my filters", "Show Phuket instead"]
    if focused and has_properties:
        if language == "pl":
            return ["Czy jest blisko plaży?", "Czy cudzoziemiec może to kupić?", "Poproś o broszurę"]
        if language == "th":
            return ["ใกล้ชายหาดไหม?", "ชาวต่างชาติซื้อได้ไหม?", "ขอโบรชัวร์"]
        return ["Is it near the beach?", "Can a foreigner buy this?", "Request the brochure"]
    bangkok_only = "bangkok" in loc_low or "bangkok" in u_low
    if language == "th":
        if has_properties:
            return [
                "โครงการไหนใกล้ชายหาดที่สุด?",
                "ชาวต่างชาติซื้อกรรมสิทธิ์ (Freehold) ได้ไหม?",
                "ติดต่อที่ปรึกษาอสังหาริมทรัพย์"
            ]
        if any(b in u_low for b in ["bedroom", "bed", "ห้องนอน"]) or any(str(i) in u_low for i in [7, 8, 9, 10, 11, 12]):
            return [
                "ติดต่อที่ปรึกษาอสังหาริมทรัพย์",
                "แสดงวิลล่าหรูขนาดใหญ่ที่สุด (5-6 ห้องนอน)",
                "พูลวิลล่าในภูเก็ต"
            ]
        elif "phuket" in u_low or "ภูเก็ต" in user_message:
            return [
                "แสดงพูลวิลล่าในภูเก็ต",
                "คอนโดมิเนียมราคาต่ำกว่า 10 ล้านบาท",
                "สัญญาเช่าระยะยาว (Leasehold) ในไทยทำงานอย่างไร?"
            ]
        elif "samui" in u_low or "สมุย" in user_message:
            return [
                "แสดงวิลล่าในเกาะสมุย",
                "อสังหาริมทรัพย์ในแม่น้ำ",
                "วิลล่าพร้อมวิวทะเล"
            ]
        return [
            "ค้นหาอสังหาริมทรัพย์ระดับพรีเมียมในไทย",
            "ขั้นตอนการซื้ออสังหาริมทรัพย์ในไทยเป็นอย่างไร?"
        ]
    elif language == "pl":
        if has_properties:
            return [
                "Które są najbliżej plaży?",
                "Czy obcokrajowiec może kupić freehold?",
                "Połącz mnie z doradcą"
            ]
        elif any(b in u_low for b in ["sypialn", "pokoj", "pokoi"]) or any(str(i) in u_low for i in [7, 8, 9, 10, 11, 12]):
            return [
                "Połącz z doradcą ds. nieruchomości",
                "Pokaż największe wille (5-6 sypialni)",
                "Luksusowe wille z basenem na Phuket"
            ]
        elif "phuket" in u_low:
            return [
                "Pokaż wille z basenem na Phuket",
                "Apartamenty poniżej 10 mln THB",
                "Jak działa leasehold w Tajlandii?"
            ]
        elif "samui" in u_low:
            return [
                "Pokaż wille na Koh Samui",
                "Nieruchomości w Maenam",
                "Wille z widokiem na morze"
            ]
        return [
            "Przeglądaj luksusowe nieruchomości w Tajlandii",
            "Jak wygląda zakup nieruchomości w Tajlandii?"
        ]
    else:
        if has_properties:
            if bangkok_only and "phuket" not in u_low and "beach" not in u_low:
                return [
                    "Which of these has a pool?",
                    "Can a foreigner buy these?",
                    "Show something cheaper",
                ]
            return [
                "Which ones are closest to the beach?",
                "Can a foreigner buy these with freehold?",
                "Connect me with an advisor"
            ]
        elif any(b in u_low for b in ["bedroom", "bed"]) or any(str(i) in u_low for i in [7, 8, 9, 10, 11, 12]):
            return [
                "Connect with a property advisor",
                "Show largest luxury villas (5-6 beds)",
                "Explore Phuket luxury villas"
            ]
        elif "phuket" in u_low:
            return [
                "Show pool villas in Phuket",
                "Condominiums under 10M THB",
                "How does Thai leasehold work?"
            ]
        elif "samui" in u_low:
            return [
                "Show villas in Koh Samui",
                "Properties in Maenam",
                "Villas with sea views"
            ]
        return [
            "Explore luxury properties in Thailand",
            "How does buying property in Thailand work?"
        ]
