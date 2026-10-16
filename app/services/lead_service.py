from decimal import Decimal
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from app.db.models import Lead, Conversation
import logging

logger = logging.getLogger(__name__)

def calculate_lead_score(
    has_contact: bool = False,
    budget_specified: bool = False,
    property_requested: bool = False,
    turns_count: int = 1
) -> float:
    """Computes a lead intent score between 0.0 and 100.0 based on signals."""
    score = 10.0  # Base engagement score
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
    db: Session,
    conversation: Conversation,
    contact_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_phone: Optional[str] = None,
    preferred_location: Optional[str] = None,
    preferred_type: Optional[str] = None,
    budget_min: Optional[Decimal] = None,
    budget_max: Optional[Decimal] = None,
    buying_signal: Optional[str] = None,
    notes: Optional[str] = None
) -> Lead:
    class DummyLead:
        def __init__(self, score_val=0.0):
            self.id = getattr(conversation, "id", "lead_firestore")
            self.lead_score = score_val
            self.contact_email = contact_email
            self.contact_phone = contact_phone
            self.contact_name = contact_name

    from app.core.config import settings
    if not getattr(settings, "ENABLE_LEAD_SAVING", False):
        return DummyLead(score_val=0.8)

    # If this is a Firestore conversation (string ID) or local DB is skipped
    conv_id = getattr(conversation, "id", None)
    lead = None
    if conv_id and not isinstance(conv_id, str):
        try:
            lead = db.query(Lead).filter(Lead.conversation_id == conv_id).first()
        except Exception as qe:
            logger.warning(f"Lead lookup in SQL failed: {qe}")
            lead = None
    
    if not lead and hasattr(conversation, "__table__"):
        try:
            lead = Lead(
                conversation_id=conversation.id,
                status="new",
                buying_signals=[]
            )
            db.add(lead)
        except Exception:
            lead = None

    has_contact = bool(contact_email or contact_phone)
    turns = len(conversation.message_history or [])
    score = calculate_lead_score(
        has_contact=has_contact,
        budget_specified=bool(budget_min or budget_max),
        property_requested=bool(preferred_location or preferred_type),
        turns_count=turns
    )
    conversation.lead_score = score

    if lead:
        if contact_name and contact_name.strip():
            lead.contact_name = contact_name.strip()
        if contact_email and contact_email.strip():
            lead.contact_email = contact_email.strip()
        if contact_phone and contact_phone.strip():
            lead.contact_phone = contact_phone.strip()
        if preferred_location and preferred_location.strip():
            lead.preferred_location = preferred_location.strip()
        if preferred_type and preferred_type.strip():
            lead.preferred_type = preferred_type.strip()
        if budget_min is not None:
            lead.budget_min = budget_min
        if budget_max is not None:
            lead.budget_max = budget_max

        signals = list(lead.buying_signals or [])
        if buying_signal and buying_signal not in signals:
            signals.append(buying_signal)
        if notes and notes not in signals:
            signals.append(f"Note: {notes}")
        lead.buying_signals = signals
        lead.lead_score = score

        try:
            db.commit()
            db.refresh(lead)
            return lead
        except Exception as e:
            logger.warning(f"Local DB commit skipped for lead (using Firestore): {e}")

    return DummyLead(score_val=score)
