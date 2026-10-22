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

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Lead, Conversation

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
    """Lightweight lead object used for in-session upsert (and when SQL is off)."""

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


def record_or_update_lead(
    db: Optional[Session] = None,
    conversation: Optional[Conversation] = None,
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


def _persist_sql_lead(db: Session, conversation: Any, session_lead: SessionLead) -> None:
    conv_id = getattr(conversation, "id", None)
    lead = None
    if conv_id and not isinstance(conv_id, str):
        try:
            lead = db.query(Lead).filter(Lead.conversation_id == conv_id).first()
        except Exception as qe:
            logger.warning("Lead lookup in SQL failed: %s", qe)
            return

    if not lead and hasattr(conversation, "__table__") and conv_id and not isinstance(conv_id, str):
        try:
            lead = Lead(
                conversation_id=conversation.id,
                status="new",
                buying_signals=[],
            )
            db.add(lead)
        except Exception as e:
            logger.warning("Lead create skipped: %s", e)
            return

    if not lead:
        return

    if session_lead.contact_name:
        lead.contact_name = session_lead.contact_name
    if session_lead.contact_email:
        lead.contact_email = session_lead.contact_email
    if session_lead.contact_phone:
        lead.contact_phone = session_lead.contact_phone
    if session_lead.preferred_location:
        lead.preferred_location = session_lead.preferred_location
    if session_lead.preferred_type:
        lead.preferred_type = session_lead.preferred_type
    if session_lead.budget_min is not None:
        lead.budget_min = session_lead.budget_min
    if session_lead.budget_max is not None:
        lead.budget_max = session_lead.budget_max
    lead.buying_signals = list(session_lead.buying_signals or [])
    lead.lead_score = float(session_lead.lead_score or 0.0)
    db.commit()
    try:
        db.refresh(lead)
    except Exception:
        pass


def enqueue_lead_save(
    db: Optional[Session] = None,
    conversation: Optional[Conversation] = None,
    **fields: Any,
) -> None:
    """
    Fire-and-forget lead save. Never delays the caller; failures are logged only.
    Uses a conversation snapshot so the reply path is not blocked.
    """
    if conversation is None:
        return

    # Snapshot identity + history length for scoring (avoid sharing live ORM across threads)
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
            "__table__": getattr(conversation, "__table__", None),
        },
    )()

    payload = dict(fields)

    def _run() -> None:
        local_db = None
        try:
            lead = record_or_update_lead(db=None, conversation=snap, **payload)
            if getattr(settings, "ENABLE_LEAD_SAVING", False):
                try:
                    from app.db.session import UserSessionLocal
                    local_db = UserSessionLocal()
                except Exception:
                    local_db = None
                if local_db is not None and lead is not None:
                    try:
                        _persist_sql_lead(local_db, snap, lead)
                    except Exception as pe:
                        logger.warning("Lead SQL persist failed (session lead kept): %s", pe)
        except Exception as e:
            logger.warning("Background lead save failed (chat reply unaffected): %s", e)
        finally:
            if local_db is not None:
                try:
                    local_db.close()
                except Exception:
                    pass

    try:
        threading.Thread(target=_run, daemon=True, name="lead-save").start()
    except Exception as e:
        logger.warning("Could not start lead-save thread: %s", e)
        try:
            record_or_update_lead(db=None, conversation=conversation, **payload)
        except Exception as e2:
            logger.warning("Inline lead save fallback failed: %s", e2)
