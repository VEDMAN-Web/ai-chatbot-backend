import os
import logging
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, EmailStr, field_validator
from typing import Optional, List, Dict, Any
import io
from app.core.config import settings
from app.db.session import get_db
from app.services.conversation_service import get_or_create_conversation, append_conversation_turn
from app.services.public_agent import public_agent, format_property_card
from app.services.lead_service import record_or_update_lead
from app.db.models import Property

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Public Customer Chatbot"])

# --- Request & Response Schemas ---

class PublicChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128, description="Unique session identifier for web visitor", example="sess_12345")
    message: Optional[str] = Field(None, max_length=2000, description="Visitor's conversational query or request", example="Show me villas in Phuket under 15 million THB")
    site_origin: Optional[str] = Field("varsoviaestate.com", max_length=100, description="Origin site domain (e.g. varsoviaestate.com, thai.estate, tajlandia.com)")
    language: Optional[str] = Field("en", description="Language code for conversation ('en', 'pl', or 'th'). Default is 'en'", example="en")

    # Optional lead / contact submission fields (allows frontend to submit contact forms directly through this single API!)
    contact_name: Optional[str] = Field(None, max_length=200, description="Visitor's full name", example="Alexander Smith")
    contact_email: Optional[str] = Field(None, max_length=200, description="Visitor's email address", example="alexander.smith@example.com")
    contact_phone: Optional[str] = Field(None, max_length=100, description="Visitor's phone or WhatsApp number", example="+48 501 234 567")
    preferred_location: Optional[str] = Field(None, max_length=200, description="Preferred area or island", example="Bang Tao, Phuket")
    budget: Optional[float] = Field(None, ge=0, description="Stated budget in THB", example=25000000)
    notes: Optional[str] = Field(None, max_length=2000, description="Inquiry comments or viewing preferences", example="Interested in viewing next week")

    @field_validator("site_origin", mode="before")
    def validate_origin(cls, v):
        default_origin = getattr(settings, "SITE_ORIGIN", "varsoviaestate.com") or "varsoviaestate.com"
        if not v or not isinstance(v, str):
            return default_origin
        v_clean = v.strip().lower().replace("https://", "").replace("http://", "").split("/")[0]
        if "tajlandia" in v_clean or default_origin in v_clean:
            return default_origin
        allowed = [default_origin, "thai.estate", "localhost", "127.0.0.1"]
        for domain in allowed:
            if domain in v_clean:
                return v_clean
        return default_origin

class PropertyCard(BaseModel):
    property_id: str = Field(..., example="PROP-PHK-001")
    name: str = Field(..., example="Aileen Residence Bangtao")
    property_type: str = Field(..., example="Villa")
    price: str = Field(..., example="from 8.xx million THB")
    bedrooms: str = Field(..., example="2–3")
    area: str = Field(..., example="0–167 sqm")
    location: str = Field(..., example="Bang Tao, Phuket")
    status: str = Field(..., example="Under construction")
    has_pool: Optional[bool] = Field(False, example=True)
    beach_access: Optional[bool] = Field(False, example=False)
    images: List[str] = Field(default_factory=list, example=["https://tajlandia.com/api/img?..."])
    detail_page_url: str = Field(..., example="https://tajlandia.com/oferty/phuket/aileen-residence-villas")
    highlights: List[str] = Field(default_factory=list, example=["54 exclusive pool villas", "Completion Q4 2027"])

class PublicChatResponse(BaseModel):
    type: str = Field(..., example="property_cards", description="Response type: 'property_cards', 'text', or 'brochure'")
    text: str = Field(..., description="Grounded natural language response")
    properties: List[PropertyCard] = Field(default_factory=list, description="Array of matching property cards")
    quick_replies: List[str] = Field(default_factory=list, description="Suggested follow-up actions for user")
    lead_captured: bool = Field(False, description="True if customer contact details were recorded")
    preferences: Dict[str, Any] = Field(default_factory=dict, description="Extracted user preferences and active search focus")
    session_id: str = Field(..., example="sess_12345")
    conversation_id: str = Field(..., example="a9f234-bc23-...")
    language: str = Field("en", description="Active response language ('en', 'pl', or 'th')", example="en")
    brochure_property_id: Optional[str] = Field(None, description="Property ID to auto-download brochure PDF. Set when type='brochure'.", example="PROP-PHK-001")
    pdf_url: Optional[str] = Field(None, description="Direct URL to view or download the generated PDF brochure", example="https://s3.hf.co/...")
    download_url: Optional[str] = Field(None, description="Direct URL to download the generated PDF brochure")
    lead_id: Optional[str] = Field(None, description="UUID of recorded lead if contact details were submitted", example="b182cb82-...")

class ChatHistoryResponse(BaseModel):
    session_id: str
    conversation_id: str
    site_origin: str
    lead_score: float
    preferences: Dict[str, Any]
    messages: List[Dict[str, Any]]

class LeadSubmitRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    contact_name: Optional[str] = Field(None, max_length=100)
    contact_email: Optional[str] = Field(None, max_length=100)
    contact_phone: Optional[str] = Field(None, max_length=50)
    preferred_location: Optional[str] = Field(None, max_length=100)
    budget: Optional[float] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=1000)

class LeadSubmitResponse(BaseModel):
    status: str
    lead_id: str
    lead_score: float
    message: str


# --- Endpoints ---

@router.post(
    "/public",
    response_model=PublicChatResponse,
    summary="Public Chat & Lead Capture",
    description="Unified conversational & lead capture endpoint for visitors on tajlandia.com, thai.estate, and varsoviaestate.com."
)
def handle_public_chat(
    req: PublicChatRequest,
    db: Session = Depends(get_db)
):
    has_contact = bool(
        (req.contact_name and req.contact_name.strip()) or
        (req.contact_email and req.contact_email.strip()) or
        (req.contact_phone and req.contact_phone.strip())
    )
    user_msg = (req.message or "").strip()

    if not user_msg and not has_contact:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide a message or contact details."
        )

    try:
        conversation = get_or_create_conversation(
            db=db,
            session_id=req.session_id,
            site_origin=req.site_origin or getattr(settings, "SITE_ORIGIN", "varsoviaestate.com"),
            agent_type="public"
        )

        recorded_lead_id = None
        if has_contact:
            budget_dec = Decimal(str(req.budget)) if req.budget is not None else None
            lead = record_or_update_lead(
                db=db,
                conversation=conversation,
                contact_name=req.contact_name,
                contact_email=req.contact_email,
                contact_phone=req.contact_phone,
                preferred_location=req.preferred_location,
                budget_max=budget_dec,
                buying_signal=f"Direct contact submission: {req.contact_email or req.contact_phone or req.contact_name}",
                notes=req.notes
            )
            recorded_lead_id = str(lead.id)

            # Sync lead to Firestore
            if getattr(settings, "ENABLE_FIRESTORE_SYNC", False):
                try:
                    from app.services.firestore_service import create_lead_in_firestore
                    create_lead_in_firestore({
                        "name": req.contact_name,
                        "email": req.contact_email,
                        "phone": req.contact_phone,
                        "brand": req.site_origin or getattr(settings, "BRAND_NAME", "Varsovia Estate"),
                        "source_url": f"https://{req.site_origin or getattr(settings, 'SITE_ORIGIN', 'varsoviaestate.com')}",
                        "message": req.notes or user_msg,
                        "property_id": req.preferred_location or ""
                    })
                except Exception as e:
                    logger.warning(f"Firestore lead sync error: {e}")

        # If user submitted contact form with no extra message, return pleasant localized confirmation immediately
        if not user_msg:
            from app.core.tenant import resolve_tenant
            tenant = resolve_tenant(req.site_origin)
            confirmations = {
                "en": f"Thank you{', ' + req.contact_name if req.contact_name else ''}! Your inquiry has been registered. An advisor from {tenant.brand_name} ({tenant.advisor_email}) will contact you shortly.",
                "pl": f"Dziękujemy{', ' + req.contact_name if req.contact_name else ''}! Twoje zgłoszenie zostało zarejestrowane. Doradca {tenant.brand_name} ({tenant.advisor_email}) skontaktuje się z Tobą wkrótce.",
                "th": f"ขอบคุณครับ{', ' + req.contact_name if req.contact_name else ''}! ระบบได้บันทึกข้อมูลการติดต่อของคุณเรียบร้อยแล้ว ที่ปรึกษาจาก {tenant.brand_name} ({tenant.advisor_email}) จะติดต่อกลับโดยเร็วที่สุดครับ"
            }
            lang = req.language if req.language in ["en", "pl", "th"] else "en"
            reply_text = confirmations.get(lang, confirmations["en"])
            append_conversation_turn(
                db=db,
                conversation=conversation,
                user_message=f"[Contact Form] {req.contact_name or ''} {req.contact_email or ''} {req.contact_phone or ''}".strip(),
                assistant_message=reply_text
            )

            return {
                "type": "text",
                "text": reply_text,
                "properties": [],
                "quick_replies": ["Show me properties in Phuket", "Search villas under 15M THB"],
                "lead_captured": True,
                "preferences": conversation.preferences or {},
                "session_id": conversation.session_id,
                "conversation_id": str(conversation.id),
                "language": lang,
                "brochure_property_id": None,
                "lead_id": recorded_lead_id
            }

        # Otherwise, process message through chatbot agent
        response = public_agent.run_conversation(
            db=db,
            conversation=conversation,
            user_message=user_msg,
            requested_language=req.language
        )
        if has_contact:
            response["lead_captured"] = True
            response["lead_id"] = recorded_lead_id

        # Log query turn to Firestore
        if getattr(settings, "ENABLE_FIRESTORE_SYNC", False) and user_msg:
            try:
                from app.services.firestore_service import log_query_to_firestore
                matched_pids = [
                    p.get("property_id") for p in response.get("properties", [])
                    if isinstance(p, dict) and p.get("property_id")
                ]
                log_query_to_firestore(
                    session_id=req.session_id,
                    user_question=user_msg,
                    ai_response=response.get("text", ""),
                    matched_properties=matched_pids
                )
            except Exception as e:
                logger.warning(f"Firestore query logging error: {e}")

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in chat endpoint for session {req.session_id}: {e}", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        lang = (req.language or "en").lower()
        if lang == "pl":
            err_text = "Obecnie występuje tymczasowy problem z serwerem bazy danych. Prosimy spróbować ponownie za chwilę."
        elif lang == "th":
            err_text = "ขณะนี้เกิดข้อผิดพลาดชั่วคราวจากเซิร์ฟเวอร์ โปรดลองใหม่อีกครั้งในภายหลังครับ"
        else:
            err_text = "We are currently experiencing a temporary server error from our data server. We will be back soon! Please try again in a few moments."

        return {
            "type": "text",
            "text": err_text,
            "properties": [],
            "quick_replies": ["Show me properties in Phuket", "Search villas in Thailand"],
            "lead_captured": False,
            "preferences": getattr(conversation, "preferences", {}) or {},
            "session_id": getattr(conversation, "session_id", req.session_id),
            "conversation_id": str(getattr(conversation, "id", "")),
            "language": lang,
            "brochure_property_id": None
        }

@router.get(
    "/history/{session_id}",
    response_model=ChatHistoryResponse,
    summary="Get Chat History",
    description="Restores conversation history and saved preferences for a session."
)
def get_chat_history(
    session_id: str,
    db: Session = Depends(get_db)
):
    conversation = get_or_create_conversation(
        db=db,
        session_id=session_id,
        agent_type="public"
    )
    raw_history = list(conversation.message_history or [])
    enriched_history = []

    for turn in raw_history:
        turn_copy = dict(turn)
        if turn_copy.get("role") == "assistant":
            props = turn_copy.get("properties") or []
            # If properties array is empty but structured_data has property IDs:
            if not props and turn_copy.get("structured_data"):
                st = turn_copy["structured_data"]
                props = st.get("properties") or st.get("property_cards") or []

            # If props contains string IDs (e.g. ['PROP-PHK-001']), hydrate them from database
            if props and isinstance(props, list) and len(props) > 0 and isinstance(props[0], str):
                db_props = db.query(Property).filter(Property.property_id.in_(props)).all()
                # Maintain original ID ordering
                prop_map = {p.property_id: format_property_card(p) for p in db_props}
                props = [prop_map[pid] for pid in props if pid in prop_map]
            
            turn_copy["properties"] = props
        enriched_history.append(turn_copy)

    return {
        "session_id": session_id,
        "conversation_id": str(conversation.id),
        "site_origin": conversation.site_origin,
        "lead_score": conversation.lead_score or 0.0,
        "preferences": conversation.preferences or {},
        "messages": enriched_history
    }

@router.post(
    "/lead",
    response_model=LeadSubmitResponse,
    summary="Submit Direct Lead",
    description="Captures customer contact details directly from modal or callback forms."
)
def submit_direct_lead(
    req: LeadSubmitRequest,
    db: Session = Depends(get_db)
):
    conversation = get_or_create_conversation(
        db=db,
        session_id=req.session_id,
        agent_type="public"
    )
    lead = record_or_update_lead(
        db=db,
        conversation=conversation,
        contact_name=req.contact_name,
        contact_email=req.contact_email,
        contact_phone=req.contact_phone,
        preferred_location=req.preferred_location,
        notes=req.notes,
        buying_signal="Direct frontend lead submission"
    )

    if getattr(settings, "ENABLE_FIRESTORE_SYNC", False):
        try:
            from app.services.firestore_service import create_lead_in_firestore
            create_lead_in_firestore({
                "name": req.contact_name,
                "email": req.contact_email,
                "phone": req.contact_phone,
                "property_id": req.preferred_location or "",
                "message": req.notes or "Direct frontend lead submission",
                "brand": conversation.site_origin or "tajlandia"
            })
        except Exception as e:
            logger.warning(f"Firestore direct lead sync error: {e}")

    return {
        "status": "success",
        "lead_id": str(lead.id),
        "lead_score": lead.lead_score,
        "message": "Thank you! Your inquiry has been registered. An advisor will contact you shortly."
    }


@router.get(
    "/brochure/{property_id}",
    summary="Download Property Brochure PDF",
    description="Generates and downloads a professional property brochure PDF with images, specs, highlights, and amenities.",
    response_class=StreamingResponse,
)
def download_property_brochure(
    property_id: str,
    site_origin: Optional[str] = Query(None, description="Tenant site origin domain"),
    session_id: Optional[str] = Query(None, description="Optional session ID to link lead to conversation"),
    format: Optional[str] = Query(None, description="Optional response format ('json' returns download link)"),
    db: Session = Depends(get_db)
):
    """
    Public endpoint — generates a branded 8-page luxury presentation property brochure PDF on-demand.
    Modeled after Varsovia_Estate_Dariusz_Tajlandia 2.pdf.
    Stores buying signal against the conversation for staff follow-up.
    """
    try:
        from app.services.brochure_service import generate_single_property_brochure_pdf
        from app.core.tenant import resolve_tenant
        tenant = resolve_tenant(site_origin)

        res = generate_single_property_brochure_pdf(db=db, property_id=property_id, site_origin=site_origin)
        pdf_bytes = res["pdf_bytes"]
        file_url = res.get("file_url") or res.get("download_url")

        # Record lead against conversation for staff follow-up if session_id provided
        if session_id:
            try:
                conv = get_or_create_conversation(db=db, session_id=session_id, site_origin=tenant.site_origin)
                prop = db.query(Property).filter(Property.property_id == property_id.upper()).first()
                pref_loc = f"{prop.district}, {prop.province}" if prop and prop.district else (prop.province if prop else None)
                record_or_update_lead(
                    db=db,
                    conversation=conv,
                    preferred_location=pref_loc,
                    preferred_type=prop.property_type if prop else None,
                    buying_signal=f"Downloaded brochure for {property_id.upper()} ({prop.property_name if prop else ''})",
                    notes=f"Customer downloaded brochure PDF via {tenant.brand_name} ({tenant.domain})"
                )
            except Exception as le:
                logger.warning(f"Could not record lead on brochure download: {le}")

        if format == "json":
            return {
                "status": "success",
                "property_id": property_id.upper(),
                "file_url": file_url,
                "download_url": file_url,
                "filename": res.get("filename")
            }

        safe_brand = tenant.brand_name.replace(" ", "_")
        safe_name = property_id.upper().replace("/", "_").replace("\\", "_")
        filename = f"{safe_brand}_Property_{safe_name}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(pdf_bytes)),
            }
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Brochure generation failed for {property_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate property brochure. Please try again.")


@router.get(
    "/brochure/file/{filename}",
    summary="Get Local Stored Brochure File",
    response_class=FileResponse
)
def get_brochure_file(filename: str):
    storage_dir = settings.REPORT_STORAGE_DIR
    file_path = os.path.join(storage_dir, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Brochure file not found")
    return FileResponse(file_path, media_type="application/pdf", filename=filename)
