from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.db.models import Conversation
import logging

logger = logging.getLogger(__name__)

def get_or_create_conversation(
    db: Session,
    session_id: str,
    site_origin: Optional[str] = None,
    agent_type: str = "public"
) -> Conversation:
    """Retrieves an existing conversation session or cleanly initializes a new one with Firestore synchronization."""
    from app.core.config import settings
    clean_session_id = session_id.strip() if session_id else "anonymous"
    clean_site_origin = (site_origin or getattr(settings, "SITE_ORIGIN", "varsoviaestate.com")).strip()

    # 1. Fetch from Firestore if enabled
    if getattr(settings, "ENABLE_FIRESTORE_SYNC", False):
        try:
            from app.services.firestore_service import get_or_create_conversation_firestore
            return get_or_create_conversation_firestore(
                session_id=clean_session_id,
                site_origin=clean_site_origin,
                agent_type=agent_type
            )
        except Exception as e:
            logger.warning(f"Firestore get_or_create_conversation notice: {e}")

    # Fallback to local SQL session
    conversation = db.query(Conversation).filter(
        Conversation.session_id == clean_session_id,
        Conversation.agent_type == agent_type
    ).first()

    if not conversation:
        conversation = Conversation(
            session_id=clean_session_id,
            site_origin=clean_site_origin,
            agent_type=agent_type,
            message_history=[],
            preferences={},
            lead_score=0.0
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    return conversation

def append_conversation_turn(
    db: Session,
    conversation: Conversation,
    user_message: str,
    assistant_message: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
    structured_data: Optional[Dict[str, Any]] = None,
    new_preferences: Optional[Dict[str, Any]] = None
) -> Conversation:
    """
    Appends a full conversational turn and persists directly in Firestore `conversations` collection.
    """
    from app.core.config import settings

    # Sync to Firestore conversations collection
    if getattr(settings, "ENABLE_FIRESTORE_SYNC", False):
        try:
            from app.services.firestore_service import append_conversation_turn_firestore
            append_conversation_turn_firestore(
                conversation=conversation,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_calls=tool_calls,
                tool_results=tool_results,
                structured_data=structured_data,
                new_preferences=new_preferences
            )
        except Exception as e:
            logger.warning(f"Firestore append_conversation_turn notice: {e}")

    history = list(conversation.message_history or [])
    timestamp = datetime.now(timezone.utc).isoformat()
    
    # Store user turn
    history.append({
        "role": "user",
        "content": user_message,
        "timestamp": timestamp
    })

    # Store assistant turn
    asst_turn = {
        "role": "assistant",
        "content": assistant_message,
        "timestamp": timestamp
    }
    if tool_calls:
        asst_turn["tool_calls"] = tool_calls
    if tool_results:
        asst_turn["tool_results"] = tool_results
    if structured_data:
        asst_turn["structured_data"] = structured_data
        if "properties" in structured_data:
            asst_turn["properties"] = structured_data["properties"]
        elif "property_cards" in structured_data:
            asst_turn["properties"] = structured_data["property_cards"]

    def _clean_for_json(obj: Any) -> Any:
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        elif hasattr(obj, "to_dict"):
            return _clean_for_json(obj.to_dict())
        elif isinstance(obj, dict):
            return {
                k: _clean_for_json(v)
                for k, v in obj.items()
                if k not in ("raw_firestore_data", "raw_data", "html_content", "base64", "pdf_bytes")
            }
        elif isinstance(obj, (list, tuple, set)):
            return [_clean_for_json(item) for item in obj]
        elif isinstance(obj, (int, float, str, bool)) or obj is None:
            return obj
        return str(obj)

    safe_turn = _clean_for_json(asst_turn)
    history.append(safe_turn)

    # Bound message history length and strip heavy tool dumps from older turns
    if len(history) > 30:
        history = history[-30:]
    for i in range(max(0, len(history) - 4)):
        if isinstance(history[i], dict):
            history[i].pop("tool_results", None)
            history[i].pop("tool_calls", None)
            history[i].pop("properties", None)
            history[i].pop("structured_data", None)

    conversation.message_history = _clean_for_json(history)

    # Update session preferences
    if new_preferences:
        prefs = dict(conversation.preferences or {})
        for k, v in new_preferences.items():
            if v is not None:
                prefs[k] = _clean_for_json(v)
        conversation.preferences = prefs

    # Only commit to local SQL DB if the object is an attached SQLAlchemy model
    if hasattr(conversation, "__table__"):
        try:
            db.commit()
            db.refresh(conversation)
        except Exception as e:
            logger.warning(f"Local DB commit skipped (using Firestore): {e}")
            try:
                db.rollback()
            except Exception:
                pass

    return conversation
