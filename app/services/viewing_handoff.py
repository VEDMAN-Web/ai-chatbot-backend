"""Viewing / visit requests without contact details: hand over to the human advisor (never invent slots or dates)."""
import re
from typing import Dict, List, Optional

_VIEWING_RE = re.compile(
    r"\bviewings?\b|\bsite\s+visit|\b(?:can|could|may)\s+i\s+(?:visit|see|view)\b|\b(?:want|like|wish|plan)\s+to\s+(?:visit|see\s+it|view)\b"
    r"|\bvisit\s+(?:it|this|that|the|next|tomorrow|on)\b|\bsee\s+it\s+in\s+person\b|\bshow\s+me\s+around\b"
    r"|\b(?:book|arrange|schedule|set\s+up)\s+(?:a\s+)?(?:visit|tour|viewing|showing|appointment)\b"
    r"|obejrz|oglądn|ogladn|oglądani|ogladani|wizyt|zwiedz|umówi\w*\s+(?:prezentac|oglądan|wizyt|spotkan)|prezentacj",
    re.IGNORECASE,
)


def is_viewing_request(message: str) -> bool:
    return bool(message and _VIEWING_RE.search(message))


def _contact_line(contacts: List[Dict[str, str]]) -> str:
    return " · ".join(f"{c.get('label', '').strip()} {c.get('phone', '').strip()}".strip() for c in contacts if c.get("phone"))


def build_viewing_reply(language: str, name: Optional[str], contacts: List[Dict[str, str]], advisor_email: str) -> str:
    phones = _contact_line(contacts)
    if language == "pl":
        of = f" oferty **{name}**" if name else ""
        tail = f" Możesz też zadzwonić lub napisać na WhatsApp: {phones}." if phones else ""
        return (
            f"Chętnie umówię oglądanie{of}. Terminy ustala bezpośrednio nasz doradca ({advisor_email}) - "
            "kliknij **Porozmawiaj z doradcą** (lub **Zapytaj** przy ofercie) i zostaw imię oraz e-mail lub telefon, "
            f"a doradca potwierdzi z Tobą dogodną datę.{tail}"
        )
    if language == "th":
        of = f" **{name}**" if name else ""
        tail = f" หรือโทร/WhatsApp ได้ที่ {phones}" if phones else ""
        return (
            f"ยินดีนัดชม{of}ครับ ที่ปรึกษาของเรา ({advisor_email}) จะยืนยันวันเวลากับคุณโดยตรง "
            f"กดปุ่ม **คุยกับที่ปรึกษา** แล้วฝากชื่อและอีเมลหรือเบอร์โทรไว้ได้เลย{tail}"
        )
    of = f" of **{name}**" if name else ""
    tail = f" You can also call or WhatsApp us right now: {phones}." if phones else ""
    return (
        f"I'd be glad to arrange a viewing{of}. Viewings are coordinated directly by our advisor ({advisor_email}), "
        "who confirms a suitable date with you. Tap **Talk to an advisor** (or **Inquire** on the property card) "
        f"and leave your name plus an email or phone number.{tail}"
    )


def name_for_id(history: List[Dict], pid: Optional[str]) -> Optional[str]:
    """Display name of a listing shown earlier in this conversation (latest card wins)."""
    if not pid:
        return None
    for turn in reversed(history or []):
        if turn.get("role") != "assistant":
            continue
        sd = turn.get("structured_data") or {}
        for src in ((sd.get("properties") if isinstance(sd, dict) else None) or [], turn.get("properties") or []):
            for c in src:
                if isinstance(c, dict) and str(c.get("property_id") or c.get("id")) == str(pid) and c.get("name"):
                    return str(c["name"])
    return None
