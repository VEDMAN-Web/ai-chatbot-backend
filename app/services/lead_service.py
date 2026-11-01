"""
Public lead capture: validate contacts, upsert per session, interest levels.
Lead writes must never invent contact fields and must never break chat replies.
"""

from __future__ import annotations

import logging
import re
import threading
from copy import deepcopy
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import uuid4


from app.core.config import settings

logger = logging.getLogger(__name__)

# In-process upsert store keyed by agent:session_id (survives DummyLead / Firestore conv ids)
_IN_SESSION_LEADS: Dict[str, Dict[str, Any]] = {}
_SESSION_LOCK = threading.RLock()

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# E.164-ish / international: at least 8 digits after stripping non-digits
_PHONE_MIN_DIGITS = 8
_PHONE_MAX_DIGITS = 15

INTEREST_BROWSING = "browsing"
INTEREST_INTERESTED = "interested"
INTEREST_HOT = "hot"


def clear_session_leads() -> None:
    """Test helper."""
    with _SESSION_LOCK:
        _IN_SESSION_LEADS.clear()


def list_session_leads() -> List[Dict[str, Any]]:
    """Test helper."""
    with _SESSION_LOCK:
        return list(_IN_SESSION_LEADS.values())


def validate_email(value: Optional[str]) -> Optional[str]:
    """Return cleaned email or None if missing/invalid. Never invent."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or " " in raw:
        return None
    if not _EMAIL_RE.match(raw):
        return None
    return raw.lower()


def validate_phone(value: Optional[str]) -> Optional[str]:
    """Return cleaned phone or None if missing/invalid. Never invent."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < _PHONE_MIN_DIGITS or len(digits) > _PHONE_MAX_DIGITS:
        return None
    # Keep a readable form (original stripped), not a fabricated number
    return raw


def validate_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or len(raw) < 2:
        return None
    # Reject obvious placeholders
    if raw.lower() in {"n/a", "na", "none", "null", "unknown", "test"}:
        return None
    return raw


def infer_interest_level(
    *,
    buying_signal: Optional[str] = None,
    has_contact: bool = False,
    notes: Optional[str] = None,
) -> str:
    """
    Clear-signal interest only:
    - hot: contact given, viewing, callback
    - interested: brochure, price, ownership, payment, property details
    - browsing: search / weak engagement
    """
    blob = f"{buying_signal or ''} {notes or ''}".lower()
    if has_contact:
        return INTEREST_HOT
    if any(
        k in blob
        for k in (
            "viewing",
            "callback",
            "call me",
            "contact details",
            "direct customer lead",
            "email or calendar",
        )
    ):
        return INTEREST_HOT if has_contact else INTEREST_INTERESTED
    if any(
        k in blob
        for k in (
            "brochure",
            "price",
            "ownership",
            "payment",
            "requested details",
            "tenure",
            "leasehold",
            "freehold",
        )
    ):
        return INTEREST_INTERESTED
    return INTEREST_BROWSING


def _session_key(conversation: Any) -> str:
    sid = getattr(conversation, "session_id", None) or getattr(conversation, "id", None) or "anonymous"
    agent = getattr(conversation, "agent_type", None) or "public"
    return f"{agent}:{sid}"


def _interest_rank(level: Optional[str]) -> int:
    return {INTEREST_BROWSING: 1, INTEREST_INTERESTED: 2, INTEREST_HOT: 3}.get(level or "", 0)


class SessionLead:
    """Lightweight lead object used for in-session upsert ()."""

    def __init__(self, data: Dict[str, Any]):
        self.id = data.get("id") or str(uuid4())
        self.contact_name = data.get("contact_name")
        self.contact_email = data.get("contact_email")
        self.contact_phone = data.get("contact_phone")
        self.preferred_location = data.get("preferred_location")
        self.preferred_type = data.get("preferred_type")
        self.budget_min = data.get("budget_min")
        self.budget_max = data.get("budget_max")
        self.lead_score = float(data.get("lead_score") or 0.0)
        self.buying_signals = list(data.get("buying_signals") or [])
        self.interest_level = data.get("interest_level") or INTEREST_BROWSING
        self.status = data.get("status") or "new"
        self.session_id = data.get("session_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "contact_name": self.contact_name,
            "contact_email": self.contact_email,
            "contact_phone": self.contact_phone,
            "preferred_location": self.preferred_location,
            "preferred_type": self.preferred_type,
            "budget_min": self.budget_min,
            "budget_max": self.budget_max,
            "lead_score": self.lead_score,
            "buying_signals": list(self.buying_signals or []),
            "interest_level": self.interest_level,
            "status": self.status,
            "session_id": self.session_id,
        }


def calculate_lead_score(
    has_contact: bool = False,
    budget_specified: bool = False,
    property_requested: bool = False,
    turns_count: int = 1,
) -> float:
    """Computes a lead intent score between 0.0 and 100.0 based on signals."""
    score = 10.0
    if turns_count >= 3:
        score += 15.0
    if budget_specified:
        score += 25.0
    if property_requested:
        score += 20.0
    if has_contact:
        score += 30.0
    return min(score, 100.0)


def _first_present(doc: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = doc.get(k)
        if v is not None and v != "":
            return v
    return None


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return bool(str(value or "").strip())


def score_lead_document(doc: Dict[str, Any]) -> float:
    """
    Compute a lead score from a stored lead document's own signals.

    Tolerates every shape in the `leads` collection: snake_case session leads,
    camelCase docs from create_lead_in_firestore / the portal, and imported rows.
    Uses the same calculate_lead_score() as live chat capture.
    """
    d = dict(doc or {})
    email = _first_present(d, "contact_email", "email", "contactEmail")
    phone = _first_present(d, "contact_phone", "phone", "contactPhone")
    has_contact = bool(validate_email(email) or validate_phone(phone))

    budget_specified = any(
        _positive_number(d.get(k))
        for k in ("budget_min", "budget_max", "budget", "budgetMin", "budgetMax")
        if d.get(k) not in (None, "")
    )
    property_requested = _first_present(
        d,
        "preferred_location", "preferred_type", "preferredLocation", "preferredType",
        "location", "property_type", "propertyType",
        "property_id", "propertyId", "property_name", "propertyName",
    ) is not None

    turns = 1
    for k in ("turns_count", "message_count", "messageCount", "turns"):
        try:
            if d.get(k) is not None:
                turns = max(turns, int(d.get(k)))
                break
        except (TypeError, ValueError):
            continue
    else:
        history = d.get("message_history") or d.get("messages")
        if isinstance(history, list) and history:
            turns = len(history)

    return calculate_lead_score(
        has_contact=has_contact,
        budget_specified=budget_specified,
        property_requested=property_requested,
        turns_count=turns,
    )


def stored_lead_score(doc: Dict[str, Any]) -> Optional[float]:
    """
    Return the score actually stored on a lead doc, or None when it is unscored.

    calculate_lead_score() never returns below 10, so a stored value <= 0 (or a
    missing / unparseable one) is a default placeholder, not a computed score.
    """
    d = doc or {}
    for k in ("lead_score", "leadScore", "score"):
        raw = d.get(k)
        if raw is None or raw == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return None


def resolve_lead_score(doc: Dict[str, Any]) -> float:
    """Stored score when present, otherwise computed from the doc's signals."""
    stored = stored_lead_score(doc)
    return stored if stored is not None else score_lead_document(doc)


def record_or_update_lead(
    db: Optional[Any] = None,
    conversation: Optional[Any] = None,
    contact_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_phone: Optional[str] = None,
    preferred_location: Optional[str] = None,
    preferred_type: Optional[str] = None,
    budget_min: Optional[Decimal] = None,
    budget_max: Optional[Decimal] = None,
    buying_signal: Optional[str] = None,
    notes: Optional[str] = None,
) -> Any:
    """
    Upsert one lead per session. Saves only validated visitor-provided fields.
    Never invents name/email/phone. Always updates interest_level + buying_signals.
    """
    if conversation is None:
        raise ValueError("conversation is required")

    clean_email = validate_email(contact_email)
    clean_phone = validate_phone(contact_phone)
    clean_name = validate_name(contact_name)
    clean_loc = (preferred_location or "").strip() or None
    clean_type = (preferred_type or "").strip() or None

    key = _session_key(conversation)
    with _SESSION_LOCK:
        existing = dict(_IN_SESSION_LEADS.get(key) or {})
        if not existing:
            existing = {
                "id": f"lead_{uuid4().hex[:12]}",
                "session_id": getattr(conversation, "session_id", None),
                "buying_signals": [],
                "interest_level": INTEREST_BROWSING,
                "status": "new",
            }

        # Merge — only overwrite with newly provided validated values
        if clean_name:
            existing["contact_name"] = clean_name
        if clean_email:
            existing["contact_email"] = clean_email
        if clean_phone:
            existing["contact_phone"] = clean_phone
        if clean_loc:
            existing["preferred_location"] = clean_loc
        if clean_type:
            existing["preferred_type"] = clean_type
        if budget_min is not None:
            existing["budget_min"] = budget_min
        if budget_max is not None:
            existing["budget_max"] = budget_max

        signals = list(existing.get("buying_signals") or [])
        if buying_signal and buying_signal not in signals:
            signals.append(buying_signal)
        if notes and f"Note: {notes}" not in signals and notes not in signals:
            signals.append(f"Note: {notes}")
        existing["buying_signals"] = signals

        has_contact = bool(existing.get("contact_email") or existing.get("contact_phone"))
        new_level = infer_interest_level(
            buying_signal=buying_signal,
            has_contact=has_contact,
            notes=notes,
        )
        # Never downgrade interest
        if _interest_rank(new_level) >= _interest_rank(existing.get("interest_level")):
            existing["interest_level"] = new_level

        turns = len(getattr(conversation, "message_history", None) or [])
        score = calculate_lead_score(
            has_contact=has_contact,
            budget_specified=bool(existing.get("budget_min") or existing.get("budget_max")),
            property_requested=bool(existing.get("preferred_location") or existing.get("preferred_type")),
            turns_count=turns,
        )
        existing["lead_score"] = score
        try:
            conversation.lead_score = score
        except Exception:
            pass

        # Keep interest visible in signals for the sales team
        level_tag = f"interest_level:{existing['interest_level']}"
        signals = [s for s in existing["buying_signals"] if not str(s).startswith("interest_level:")]
        signals.append(level_tag)
        existing["buying_signals"] = signals

        _IN_SESSION_LEADS[key] = existing
        session_lead = SessionLead(deepcopy(existing))

    return session_lead


def get_session_lead(conversation: Any) -> Optional[SessionLead]:
    key = _session_key(conversation)
    with _SESSION_LOCK:
        data = _IN_SESSION_LEADS.get(key)
        return SessionLead(deepcopy(data)) if data else None


def _lead_document_id(conversation: Any) -> str:
    import hashlib

    return "lead_" + hashlib.sha1(_session_key(conversation).encode("utf-8")).hexdigest()[:24]


def _persist_firestore_lead(conversation: Any, session_lead: SessionLead) -> Optional[str]:
    """Upsert the session lead into the Firestore `leads` collection (one document per session)."""
    from datetime import datetime, timezone

    from app.repositories import _fs_utils as fs

    client = fs.user_fs_client() or fs.admin_fs_client()
    if client is None:
        logger.warning("Firestore client not available; lead kept in session only")
        return None

    def _num(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    doc_id = _lead_document_id(conversation)
    now_iso = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "id": doc_id,
        "conversation_id": str(getattr(conversation, "id", "") or "") or None,
        "session_id": getattr(conversation, "session_id", None),
        "site": getattr(conversation, "site_origin", None),
        "source": "chatbot",
        "buying_signals": list(session_lead.buying_signals or []),
        "lead_score": float(session_lead.lead_score or 0.0),
        "updated_at": now_iso,
    }
    for key in ("contact_name", "contact_email", "contact_phone", "preferred_location", "preferred_type"):
        value = getattr(session_lead, key, None)
        if value:
            payload[key] = value
    for key in ("budget_min", "budget_max"):
        value = _num(getattr(session_lead, key, None))
        if value is not None:
            payload[key] = value
    existing = fs.get_document(client, "leads", doc_id)
    if existing is None:
        payload["status"] = "new"
        payload["created_at"] = now_iso
    fs.set_document(client, "leads", doc_id, payload, merge=True)
    return doc_id


def enqueue_lead_save(
    db: Optional[Any] = None,
    conversation: Optional[Any] = None,
    **fields: Any,
) -> None:
    """
    Fire-and-forget lead save. Never delays the caller; failures are logged only.
    Uses a conversation snapshot so the reply path is not blocked.
    """
    if conversation is None:
        return

    # Snapshot identity + history length for scoring (avoid sharing live objects across threads)
    snap = type(
        "LeadConvSnap",
        (),
        {
            "id": getattr(conversation, "id", None),
            "session_id": getattr(conversation, "session_id", None),
            "agent_type": getattr(conversation, "agent_type", "public"),
            "message_history": list(getattr(conversation, "message_history", None) or []),
            "preferences": dict(getattr(conversation, "preferences", None) or {}),
            "site_origin": getattr(conversation, "site_origin", None),
            "lead_score": getattr(conversation, "lead_score", 0.0),
        },
    )()

    payload = dict(fields)

    def _run() -> None:
        try:
            lead = record_or_update_lead(db=None, conversation=snap, **payload)
            if getattr(settings, "ENABLE_LEAD_SAVING", False) and lead is not None:
                try:
                    _persist_firestore_lead(snap, lead)
                except Exception as pe:
                    logger.warning("Lead Firestore persist failed (session lead kept): %s", pe)
        except Exception as e:
            logger.warning("Background lead save failed (chat reply unaffected): %s", e)

    try:
        threading.Thread(target=_run, daemon=True, name="lead-save").start()
    except Exception as e:
        logger.warning("Could not start lead-save thread: %s", e)
        try:
            record_or_update_lead(db=None, conversation=conversation, **payload)
        except Exception as e2:
            logger.warning("Inline lead save fallback failed: %s", e2)
