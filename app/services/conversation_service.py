from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import uuid
from app.db.models import Conversation
import logging

logger = logging.getLogger(__name__)

# In-session memory store: keeps conversation sessions active in memory across 50+ back-to-back questions
# Guarantees that in-session memory stays ON even when permanent database logging is OFF.
_IN_SESSION_CONVERSATIONS: Dict[str, Any] = {}

def get_or_create_conversation(
    db: Session,
    session_id: str,
    site_origin: Optional[str] = None,
    agent_type: str = "public"
) -> Conversation:
    """
    Retrieves an existing conversation session or cleanly initializes a new one.
    Maintains in-session memory across 50+ back-to-back questions.
    Respects ENABLE_CHAT_LOGGING flag: if False, permanent persistence stays OFF while in-session memory stays ON.
    """
    from app.core.config import settings
    clean_session_id = session_id.strip() if session_id else "anonymous"
    clean_site_origin = (site_origin or getattr(settings, "SITE_ORIGIN", "varsoviaestate.com")).strip()
    session_key = f"{agent_type}:{clean_session_id}"

    # 1. Check active in-session memory store
    if session_key in _IN_SESSION_CONVERSATIONS:
        return _IN_SESSION_CONVERSATIONS[session_key]

    chat_logging_on = getattr(settings, "ENABLE_CHAT_LOGGING", False)

    # 2. Fetch from Firestore if permanent chat logging is enabled
    if chat_logging_on:
        sync_enabled = (
            settings.get_admin_enable_firestore_sync()
            if agent_type == "admin"
            else settings.get_user_enable_firestore_sync()
        )
        if sync_enabled:
            try:
                from app.services.firestore_service import get_or_create_conversation_firestore
                conv = get_or_create_conversation_firestore(
                    session_id=clean_session_id,
                    site_origin=clean_site_origin,
                    agent_type=agent_type
                )
                _IN_SESSION_CONVERSATIONS[session_key] = conv
                return conv
            except Exception as e:
                logger.warning(f"Firestore get_or_create_conversation notice: {e}")

    # 3. Check local SQL session if permanent chat logging is enabled
    if chat_logging_on and db is not None:
        try:
            conv = db.query(Conversation).filter(
                Conversation.session_id == clean_session_id,
                Conversation.agent_type == agent_type
            ).first()
            if conv:
                _IN_SESSION_CONVERSATIONS[session_key] = conv
                return conv
        except Exception as e:
            logger.debug(f"SQL get conversation fallback: {e}")

    # 4. Initialize clean conversation object with in-session memory
    conversation = Conversation(
        session_id=clean_session_id,
        site_origin=clean_site_origin,
        agent_type=agent_type,
        message_history=[],
        preferences={},
        lead_score=0.0
    )
    # Ensure ephemeral ID exists
    if not getattr(conversation, "id", None):
        conversation.id = uuid.uuid4()

    if chat_logging_on and db is not None:
        try:
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    _IN_SESSION_CONVERSATIONS[session_key] = conversation
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
    Appends a full conversational turn.
    Maintains complete in-session memory for 50+ back-to-back questions (up to 120 turns).
    Permanent database persistence stays OFF when ENABLE_CHAT_LOGGING is False.
    """
    from app.core.config import settings

    c_agent_type = getattr(conversation, "agent_type", "public")
    chat_logging_on = getattr(settings, "ENABLE_CHAT_LOGGING", False)
    clean_session_id = getattr(conversation, "session_id", "anonymous")
    session_key = f"{c_agent_type}:{clean_session_id}"

    # Sync to Firestore conversations collection ONLY if permanent chat logging is ON
    if chat_logging_on:
        sync_enabled = (
            settings.get_admin_enable_firestore_sync()
            if c_agent_type == "admin"
            else settings.get_user_enable_firestore_sync()
        )
        if sync_enabled:
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

    # Bound message history length to 120 messages (60 turns) to easily support 50+ back-to-back questions
    if len(history) > 120:
        history = history[-120:]

    # For turns older than the last 4 turns, remove bulky tool metadata to conserve tokens while preserving conversation
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

    # Keep in-session memory updated
    _IN_SESSION_CONVERSATIONS[session_key] = conversation

    # Only commit to local SQL DB if permanent chat logging is explicitly ON
    if chat_logging_on and hasattr(conversation, "__table__") and db is not None:
        try:
            db.commit()
            db.refresh(conversation)
        except Exception as e:
            logger.warning(f"Local DB commit skipped: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    return conversation
