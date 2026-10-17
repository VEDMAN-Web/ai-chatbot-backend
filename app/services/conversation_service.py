"""
Conversation memory for the public/admin chatbots.

ALL history, preferences, and property_context are stored in Cloud Firestore.
PostgreSQL is NOT used for chat memory (admin RBAC / pending actions still use SQL elsewhere).
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

# Hot in-process cache (same Cloud Run instance). Source of truth after restart = Firestore.
_IN_SESSION_CONVERSATIONS: Dict[str, Any] = {}


def _firestore_sync_enabled(agent_type: str) -> bool:
    from app.core.config import settings
    if agent_type == "admin":
        return bool(settings.get_admin_enable_firestore_sync())
    return bool(settings.get_user_enable_firestore_sync())


def bind_conversation_site_origin(conversation, site_origin: Optional[str]) -> Any:
    """
    Force conversation branding to THIS request's validated site_origin.

    The web client may reuse one session_id across tajlandia.com / thai.estate /
    varsoviaestate.com. Cached or Firestore-stored siteOrigin must not stick.
    """
    from app.core.config import settings
    from app.services.firestore_service import update_conversation_site_origin_firestore

    clean = (site_origin or getattr(settings, "SITE_ORIGIN", "varsoviaestate.com") or "").strip()
    if not conversation or not clean:
        return conversation

    prev = getattr(conversation, "site_origin", None)
    if prev == clean:
        return conversation

    conversation.site_origin = clean
    agent_type = getattr(conversation, "agent_type", "public") or "public"
    if _firestore_sync_enabled(agent_type):
        try:
            update_conversation_site_origin_firestore(conversation, clean)
        except Exception as e:
            logger.warning("Failed to persist site_origin=%s for session %s: %s", clean, getattr(conversation, "session_id", "?"), e)
    return conversation


def get_or_create_conversation(
    db=None,
    session_id: str = "",
    site_origin: Optional[str] = None,
    agent_type: str = "public",
):
    """
    Load or create a conversation. Persists entirely in Firestore (conversations collection).
    `db` is accepted for API compatibility but is not used for chat storage.

    Always rebinds site_origin to the current request so multi-site session reuse
    cannot leave stale brand / advisor email on the conversation.
    """
    from app.core.config import settings
    from app.services.firestore_service import get_or_create_conversation_firestore

    clean_session_id = session_id.strip() if session_id else "anonymous"
    clean_site_origin = (site_origin or getattr(settings, "SITE_ORIGIN", "varsoviaestate.com")).strip()
    session_key = f"{agent_type}:{clean_session_id}"

    conversation = None

    # 1. In-process cache
    if session_key in _IN_SESSION_CONVERSATIONS:
        conversation = _IN_SESSION_CONVERSATIONS[session_key]
    # 2. Firestore is the durable store for ALL chat history + property_context
    elif _firestore_sync_enabled(agent_type):
        try:
            conversation = get_or_create_conversation_firestore(
                session_id=clean_session_id,
                site_origin=clean_site_origin,
                agent_type=agent_type,
            )
            _IN_SESSION_CONVERSATIONS[session_key] = conversation
        except Exception as e:
            logger.error(f"Firestore get_or_create_conversation failed: {e}")

    # 3. Ephemeral fallback only if Firestore is unavailable (not written to Postgres)
    if conversation is None:
        from app.services.firestore_service import FirestoreConversation

        now_iso = datetime.now(timezone.utc).isoformat()
        conversation = FirestoreConversation(
            clean_session_id,
            {
                "sessionId": clean_session_id,
                "siteOrigin": clean_site_origin,
                "agentType": agent_type,
                "messageHistory": [],
                "preferences": {},
                "leadScore": 0.0,
                "createdAt": now_iso,
                "updatedAt": now_iso,
            },
        )
        _IN_SESSION_CONVERSATIONS[session_key] = conversation
        logger.warning(
            "Conversation %s created in-memory only — Firestore unavailable. "
            "History will not survive restart until Firestore is reachable.",
            session_key,
        )

    return bind_conversation_site_origin(conversation, clean_site_origin)


def append_conversation_turn(
    db=None,
    conversation=None,
    user_message: str = "",
    assistant_message: str = "",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
    structured_data: Optional[Dict[str, Any]] = None,
    new_preferences: Optional[Dict[str, Any]] = None,
):
    """
    Append a turn and persist history + preferences (including property_context) to Firestore.
    Does not write chat memory to PostgreSQL.
    """
    from app.core.config import settings
    from app.services.firestore_service import append_conversation_turn_firestore

    if conversation is None:
        raise ValueError("conversation is required")

    c_agent_type = getattr(conversation, "agent_type", "public")
    clean_session_id = getattr(conversation, "session_id", "anonymous")
    session_key = f"{c_agent_type}:{clean_session_id}"

    # Always update in-memory object first (and Firestore when sync enabled)
    if _firestore_sync_enabled(c_agent_type):
        try:
            conversation = append_conversation_turn_firestore(
                conversation=conversation,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_calls=tool_calls,
                tool_results=tool_results,
                structured_data=structured_data,
                new_preferences=new_preferences,
            )
            _IN_SESSION_CONVERSATIONS[session_key] = conversation
            return conversation
        except Exception as e:
            logger.error(f"Firestore append_conversation_turn failed: {e}")

    # Firestore down: still keep in-session memory for this process
    history = list(getattr(conversation, "message_history", None) or [])
    timestamp = datetime.now(timezone.utc).isoformat()
    history.append({"role": "user", "content": user_message, "timestamp": timestamp})
    asst_turn: Dict[str, Any] = {
        "role": "assistant",
        "content": assistant_message,
        "timestamp": timestamp,
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
    history.append(asst_turn)

    max_msgs = int(getattr(settings, "SESSION_MEMORY_MESSAGES", 220) or 220)
    if len(history) > max_msgs:
        history = history[-max_msgs:]
    conversation.message_history = history

    if new_preferences:
        prefs = dict(getattr(conversation, "preferences", None) or {})
        for k, v in new_preferences.items():
            if v is not None:
                prefs[k] = v
        conversation.preferences = prefs

    _IN_SESSION_CONVERSATIONS[session_key] = conversation
    return conversation


def get_session_message_count(conversation) -> int:
    """
    Read preferences['session_message_count'] (survives message_history trim).
    Defaults to 0 when missing or invalid.
    """
    prefs = getattr(conversation, "preferences", None) or {}
    raw = prefs.get("session_message_count", 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def increment_session_message_count(conversation) -> int:
    """
    Increment preferences['session_message_count'] in memory.
    Caller must persist (e.g. persist_conversation_preferences) after a successful turn.
    """
    prefs = dict(getattr(conversation, "preferences", None) or {})
    new_count = get_session_message_count(conversation) + 1
    prefs["session_message_count"] = new_count
    conversation.preferences = prefs
    return new_count


def persist_conversation_preferences(conversation) -> None:
    """
    Persist preferences (including session_message_count) without appending a chat turn.
    Updates the in-process cache and merges into Firestore when sync is enabled.
    Does not write to PostgreSQL.
    """
    if conversation is None:
        return

    c_agent_type = getattr(conversation, "agent_type", "public")
    clean_session_id = getattr(conversation, "session_id", "anonymous")
    session_key = f"{c_agent_type}:{clean_session_id}"
    _IN_SESSION_CONVERSATIONS[session_key] = conversation

    if not _firestore_sync_enabled(c_agent_type):
        return

    try:
        from app.services.firestore_service import (
            get_firestore_client,
            _conversation_doc_id,
        )

        client = get_firestore_client(
            target="admin" if c_agent_type == "admin" else "user"
        )
        if not client or not clean_session_id:
            return
        doc_id = _conversation_doc_id(clean_session_id, c_agent_type)
        now_iso = datetime.now(timezone.utc).isoformat()
        client.collection("conversations").document(doc_id).set(
            {
                "preferences": getattr(conversation, "preferences", None) or {},
                "updatedAt": now_iso,
            },
            merge=True,
        )
        conversation.updated_at = now_iso
    except Exception as e:
        logger.error(f"persist_conversation_preferences failed: {e}")


def clear_in_session_conversation(session_id: str, agent_type: str = "public") -> None:
    """Drop process-local cache (Firestore remains source of truth)."""
    key = f"{agent_type}:{(session_id or '').strip()}"
    _IN_SESSION_CONVERSATIONS.pop(key, None)
