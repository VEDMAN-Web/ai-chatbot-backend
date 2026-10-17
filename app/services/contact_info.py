"""Contact details + localized copy for the "Inquire" pop-up form (shared by every chat front end)."""
import re
from typing import Any, Dict, List

from app.core.tenant import TenantProfile

FORM_COPY: Dict[str, Dict[str, str]] = {
    "en": {
        "title": "We're glad you're interested!",
        "intro": "Just give us your name, email or phone number so our agent can reach out to you in minutes.",
        "name_label": "Your name",
        "email_label": "Email",
        "phone_label": "Phone / WhatsApp",
        "message_label": "Anything we should know? (optional)",
        "submit_label": "Request a call back",
        "need_contact": "Please enter an email address or a phone number so we can reach you.",
        "bad_email": "Please enter a valid email address.",
        "contact_line": "If you would rather contact us directly, you can call or WhatsApp us anytime on:",
        "success": "Thank you! Our agent will reach out to you shortly.",
        "not_stored": "Thank you! Online requests are not switched on yet, so please call or WhatsApp us directly.",
        "call": "Call",
        "whatsapp": "WhatsApp",
    },
    "pl": {
        "title": "Cieszymy się z Twojego zainteresowania!",
        "intro": "Podaj nam swoje imię, adres e-mail lub numer telefonu, a nasz doradca skontaktuje się z Tobą w ciągu kilku minut.",
        "name_label": "Imię",
        "email_label": "E-mail",
        "phone_label": "Telefon / WhatsApp",
        "message_label": "Coś, o czym powinniśmy wiedzieć? (opcjonalnie)",
        "submit_label": "Poproś o kontakt",
        "need_contact": "Podaj adres e-mail lub numer telefonu, abyśmy mogli się z Tobą skontaktować.",
        "bad_email": "Podaj poprawny adres e-mail.",
        "contact_line": "Jeśli wolisz skontaktować się z nami bezpośrednio, zadzwoń lub napisz na WhatsApp o dowolnej porze:",
        "success": "Dziękujemy! Nasz doradca skontaktuje się z Tobą wkrótce.",
        "not_stored": "Dziękujemy! Zgłoszenia online nie są jeszcze włączone, prosimy o bezpośredni kontakt telefoniczny lub przez WhatsApp.",
        "call": "Zadzwoń",
        "whatsapp": "WhatsApp",
    },
    "th": {
        "title": "ยินดีที่คุณสนใจ!",
        "intro": "เพียงให้ชื่อ อีเมล หรือเบอร์โทรศัพท์ของคุณ เจ้าหน้าที่ของเราจะติดต่อกลับภายในไม่กี่นาที",
        "name_label": "ชื่อของคุณ",
        "email_label": "อีเมล",
        "phone_label": "โทรศัพท์ / WhatsApp",
        "message_label": "มีอะไรที่อยากให้เราทราบไหม (ไม่บังคับ)",
        "submit_label": "ขอให้ติดต่อกลับ",
        "need_contact": "กรุณากรอกอีเมลหรือเบอร์โทรศัพท์เพื่อให้เราติดต่อกลับได้",
        "bad_email": "กรุณากรอกอีเมลให้ถูกต้อง",
        "contact_line": "หากต้องการติดต่อเราโดยตรง โทรหรือส่งข้อความ WhatsApp ได้ตลอดเวลาที่เบอร์:",
        "success": "ขอบคุณครับ! เจ้าหน้าที่ของเราจะติดต่อกลับเร็วๆ นี้",
        "not_stored": "ขอบคุณครับ! ขณะนี้ยังไม่เปิดรับคำขอออนไลน์ กรุณาโทรหรือส่งข้อความ WhatsApp หาเราโดยตรง",
        "call": "โทร",
        "whatsapp": "WhatsApp",
    },
}

# Copy for the chat widget shell itself (button, bubble, greeting, chips) so every front end uses one source.
WIDGET_COPY: Dict[str, Dict[str, Any]] = {
    "en": {
        "cta_label": "Talk to an advisor",
        "cta_action": "open_inquiry_form",
        "bubble_label": "Ask Tina",
        "placeholder": "Ask about properties, locations or prices...",
        "send_label": "Send",
        "greeting": "Hi, I'm Tina, your property assistant. Tell me what you're looking for, or ask about any listing.",
        "suggestions": ["Villas in Phuket", "Condos under 5M THB", "Can foreigners buy freehold?", "Talk to an advisor"],
    },
    "pl": {
        "cta_label": "Porozmawiaj z doradcą",
        "cta_action": "open_inquiry_form",
        "bubble_label": "Zapytaj Tinę",
        "placeholder": "Zapytaj o nieruchomości, lokalizacje lub ceny...",
        "send_label": "Wyślij",
        "greeting": "Cześć, jestem Tina, Twoja asystentka ds. nieruchomości. Napisz, czego szukasz, lub zapytaj o dowolną ofertę.",
        "suggestions": ["Wille na Phuket", "Apartamenty do 5 mln THB", "Czy cudzoziemiec może kupić na własność?", "Porozmawiaj z doradcą"],
    },
    "th": {
        "cta_label": "คุยกับที่ปรึกษา",
        "cta_action": "open_inquiry_form",
        "bubble_label": "ถามทีน่า",
        "placeholder": "สอบถามเรื่องอสังหาริมทรัพย์ ทำเล หรือราคา...",
        "send_label": "ส่ง",
        "greeting": "สวัสดีค่ะ ฉันทีน่า ผู้ช่วยด้านอสังหาริมทรัพย์ของคุณ บอกได้เลยว่ากำลังมองหาอะไร หรือสอบถามโครงการที่สนใจ",
        "suggestions": ["วิลล่าในภูเก็ต", "คอนโดไม่เกิน 5 ล้านบาท", "ต่างชาติซื้อฟรีโฮลด์ได้ไหม", "คุยกับที่ปรึกษา"],
    },
}


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def build_contact_info(tenant: TenantProfile, language: str = "en") -> Dict[str, Any]:
    """Everything a front end needs to render the Inquire form (no hard-coded numbers or texts on its side)."""
    lang = language if language in FORM_COPY else "en"
    contacts: List[Dict[str, str]] = []
    for c in tenant.contacts or []:
        d = _digits(c.get("phone", ""))
        if not d:
            continue
        contacts.append({
            "label": c.get("label", ""),
            "phone": c["phone"],
            "tel_url": f"tel:+{d}",
            "whatsapp_url": f"https://wa.me/{d}",
        })
    return {
        "site_origin": tenant.site_origin,
        "brand_name": tenant.brand_name,
        "advisor_email": tenant.advisor_email,
        "language": lang,
        "contacts": contacts,
        "inquiry_form": FORM_COPY[lang],
        "widget": WIDGET_COPY[lang],
    }
