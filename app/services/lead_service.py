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

    # Durable Firestore write (never blocks on failure)
    if getattr(settings, "ENABLE_LEAD_SAVING", True):
        try:
            persist_lead_to_firestore(session_lead, conversation, notes=notes)
        except Exception as fe:
            logger.warning("Firestore lead write skipped: %s", fe)

    return session_lead


def get_session_lead(conversation: Any) -> Optional[SessionLead]:
    key = _session_key(conversation)
    with _SESSION_LOCK:
        data = _IN_SESSION_LEADS.get(key)
        return SessionLead(deepcopy(data)) if data else None


def make_lead_dedupe_key(
    *,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    site: Optional[str] = None,
) -> Optional[str]:
    """Normalised phone/email + site. Prefer phone when both present."""
    from app.core.tenant import normalize_tenant_domain_key
    site_key = normalize_tenant_domain_key(site) or str(site or "").strip().lower() or "unknown"
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) >= _PHONE_MIN_DIGITS:
        return f"p:{digits}|{site_key}"
    em = validate_email(email)
    if em:
        return f"e:{em}|{site_key}"
    return None


def _parse_notes_meta(notes: Optional[str]) -> Dict[str, str]:
    raw = str(notes or "")
    out: Dict[str, str] = {}
    for key in ("property_id", "property_name", "language", "summary", "site"):
        m = re.search(rf"{key}=([^;|]*)", raw)
        if m:
            out[key] = m.group(1).strip()
    return out


def persist_lead_to_firestore(
    session_lead: SessionLead,
    conversation: Any,
    *,
    notes: Optional[str] = None,
    property_id: Optional[str] = None,
    property_name: Optional[str] = None,
    language: Optional[str] = None,
    summary: Optional[str] = None,
    client_override: Any = None,
) -> Optional[str]:
    """
    Upsert into Firestore `leads` on dedupe_key (phone/email + site).
    Updates summary/property when the same visitor contacts again. Returns doc id.
    """
    from datetime import datetime, timezone

    site = getattr(conversation, "site_origin", None) or ""
    meta = _parse_notes_meta(notes)
    pid = property_id or meta.get("property_id") or ""
    pname = property_name or meta.get("property_name") or ""
    lang = language or meta.get("language") or ""
    summ = summary or meta.get("summary") or (notes or "")
    if meta.get("site"):
        site = meta.get("site") or site

    dedupe = make_lead_dedupe_key(
        email=session_lead.contact_email,
        phone=session_lead.contact_phone,
        site=site,
    )
    if not dedupe:
        # No durable contact key — skip Firestore write (still kept in-session)
        return None

    client = client_override
    if client is None:
        try:
            from app.services.firestore_service import get_firestore_client
            client = get_firestore_client("user")
        except Exception:
            client = None
    if not client:
        logger.warning("Firestore client unavailable; lead not written to Firestore")
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": None,  # filled below
        "name": session_lead.contact_name or "",
        "email": session_lead.contact_email or "",
        "phone": session_lead.contact_phone or "",
        "status": session_lead.status or "new",
        "site": site,
        "source": "chatbot",
        "property_id": pid,
        "property_name": pname,
        "session_id": getattr(conversation, "session_id", None) or session_lead.session_id or "",
        "language": lang,
        "summary": (summ or "")[:2000],
        "dedupe_key": dedupe,
        "updated_at": now_iso,
    }

    try:
        col = client.collection("leads")
        existing_id = None
        existing_data = None
        # Prefer query by dedupe_key (requires composite index in production)
        try:
            query = col.where("dedupe_key", "==", dedupe).limit(1)
            for snap in query.stream():
                existing_id = getattr(snap, "id", None)
                existing_data = snap.to_dict() if hasattr(snap, "to_dict") else None
                break
        except Exception as qe:
            logger.debug("leads dedupe query failed (falling back to scan): %s", qe)
            try:
                for snap in col.stream():
                    data = snap.to_dict() if hasattr(snap, "to_dict") else None
                    if isinstance(data, dict) and data.get("dedupe_key") == dedupe:
                        existing_id = getattr(snap, "id", None)
                        existing_data = data
                        break
            except Exception:
                pass

        # Transactional when the client exposes .transaction(); otherwise atomic-enough upsert.
        def _upsert():
            nonlocal existing_id, existing_data
            if existing_id:
                doc_ref = col.document(existing_id)
                update = {
                    "updated_at": now_iso,
                    "summary": payload["summary"] or (existing_data or {}).get("summary") or "",
                    "property_id": pid or (existing_data or {}).get("property_id") or "",
                    "property_name": pname or (existing_data or {}).get("property_name") or "",
                    "session_id": payload["session_id"],
                    "language": lang or (existing_data or {}).get("language") or "",
                    "status": payload["status"],
                    "dedupe_key": dedupe,
                }
                if payload["name"]:
                    update["name"] = payload["name"]
                if payload["email"]:
                    update["email"] = payload["email"]
                if payload["phone"]:
                    update["phone"] = payload["phone"]
                doc_ref.update(update)
                return existing_id
            doc_ref = col.document()
            new_id = getattr(doc_ref, "id", None) or f"lead_{uuid4().hex[:12]}"
            body = dict(payload)
            body["id"] = new_id
            body["created_at"] = now_iso
            doc_ref.set(body)
            return new_id

        if hasattr(client, "transaction"):
            try:
                from google.cloud import firestore as gcf  # type: ignore
                transaction = client.transaction()

                @gcf.transactional
                def _txn_body(transaction):
                    # Re-read inside the transaction when possible
                    return _upsert()

                return _txn_body(transaction)
            except Exception as te:
                logger.debug("Lead Firestore transaction fallback to direct upsert: %s", te)
                return _upsert()
        return _upsert()
    except Exception as e:
        logger.warning("Firestore lead upsert failed: %s", e)
        return None


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
            # The Firestore write happens inside record_or_update_lead.
            record_or_update_lead(db=None, conversation=snap, **payload)
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
