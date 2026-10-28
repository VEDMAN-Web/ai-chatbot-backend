"""
Reading the chat log: who may see which conversation, filters, search, sorting and pagination.

Everything here runs in the backend. Firestore is asked for one of: a single conversation, one array-contains
match (property, remark status or a search word), or the newest conversations in a date range. Access checks, the
other filters, sorting and paging are then applied in Python, so no composite index is needed. The scan is bounded;
`truncated` says when more matching conversations may exist beyond it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.auth import AuthenticatedStaff
from app.core.permissions import (
    CHAT_LOGS_VIEW_ALL,
    CHAT_LOGS_VIEW_OWN,
    CHAT_LOGS_VIEW_PUBLIC,
    permissions_for,
)
from app.services import chat_log_service
from app.services.chat_log_service import LOGS, MESSAGES, tokens_of

SCAN_CAP = 2000          # headers examined for one list request
SEARCH_CAP = 300         # headers examined when searching text (their messages are read too)
MESSAGE_READ_CAP = 2000  # messages read for one conversation
SORTS = {"last_message_at": "lastMessageAt", "created_at": "createdAt", "message_count": "messageCount"}
REMARK_STATUSES = {"none", "open", "in_review", "resolved", "rejected"}


def _client():
    return chat_log_service._client()


class ChatLogUnavailable(Exception):
    """Firestore is not configured or cannot be reached."""


@dataclass
class ListFilters:
    agent_type: Optional[str] = None
    role: Optional[str] = None
    user: Optional[str] = None
    website: Optional[str] = None
    conversation_id: Optional[str] = None
    property_id: Optional[str] = None
    remark_status: Optional[str] = None
    q: Optional[str] = None
    model: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None


# ---- access -------------------------------------------------------------------------------------

def can_view(header: Dict[str, Any], staff: AuthenticatedStaff) -> bool:
    """Whether this staff member may open the conversation at all."""
    perms = permissions_for(staff.role)
    website = header.get("website") or ""
    allowed = set(staff.allowed_websites or [])
    in_scope = website in allowed
    if header.get("agentType") == "admin":
        if CHAT_LOGS_VIEW_ALL in perms:
            return in_scope or not website
        if CHAT_LOGS_VIEW_OWN in perms:
            return str(staff.user_id) in (header.get("actorUserIds") or [])
        return False
    if CHAT_LOGS_VIEW_ALL in perms:
        return in_scope or not website
    return CHAT_LOGS_VIEW_PUBLIC in perms and in_scope


def _own_messages_only(header: Dict[str, Any], staff: AuthenticatedStaff) -> bool:
    """In an admin chat, a reviewer without 'view all' sees only their own messages (a session id can be shared)."""
    return header.get("agentType") == "admin" and CHAT_LOGS_VIEW_ALL not in permissions_for(staff.role)


# ---- serialising --------------------------------------------------------------------------------

def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return value if isinstance(value, str) else None


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def summarize(header: Dict[str, Any]) -> Dict[str, Any]:
    owner = header.get("owner") or {}
    return {
        "conversation_id": header.get("conversationId"),
        "agent_type": header.get("agentType"),
        "session_id": header.get("sessionId"),
        "website": header.get("website") or None,
        "user": {"user_id": owner.get("userId"), "email": owner.get("email"), "role": owner.get("role"), "name": owner.get("name")},
        "model": header.get("model"),
        "first_answer": header.get("firstAnswerPreview"),
        "message_count": header.get("messageCount") or 0,
        "created_at": _iso(header.get("createdAt")),
        "last_message_at": _iso(header.get("lastMessageAt")),
        "first_question": header.get("firstQuestionPreview"),
        "last_question": header.get("lastQuestionPreview"),
        "property_ids": header.get("propertyIds") or [],
        "remark_statuses": header.get("remarkStatuses") or [],
        "language": header.get("language"),
    }


def serialize_message(doc_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    actor = data.get("actor") or {}
    return {
        "message_id": doc_id,
        "seq": data.get("seq"),
        "role": data.get("role"),
        "content": data.get("content"),
        "created_at": _iso(data.get("createdAt")),
        "actor": {"user_id": actor.get("userId"), "email": actor.get("email"), "role": actor.get("role")},
        "property_ids": data.get("propertyIds") or [],
        "evidence_status": data.get("evidenceStatus"),
        "sources": data.get("sources") or [],
        "response_status": data.get("responseStatus"),
        "error_message": data.get("errorMessage"),
        "tool_summary": data.get("toolSummary") or [],
        "remark_status": data.get("remarkStatus") or "none",
        "language": data.get("language"),
        "latency_ms": data.get("latencyMs"),
        "model": data.get("model"),
    }


def valid_key(key: Optional[str]) -> bool:
    """A conversation id is a Firestore document id: a '/' would address a nested document, so it is never valid."""
    return bool(key) and "/" not in key and len(key) <= 400


# ---- reading ------------------------------------------------------------------------------------

def _require_client():
    client = _client()
    if client is None:
        raise ChatLogUnavailable("The chat log store is not available.")
    return client


def _read_messages(client, key: str, limit: int = MESSAGE_READ_CAP) -> List[Dict[str, Any]]:
    ref = client.collection(LOGS).document(key).collection(MESSAGES)
    return [serialize_message(d.id, d.to_dict() or {}) for d in ref.order_by("seq").limit(limit).stream()]


def _candidates(client, f: ListFilters) -> tuple[List[Dict[str, Any]], bool]:
    """Headers to examine, using at most one Firestore filter (so no composite index is needed)."""
    coll = client.collection(LOGS)
    if f.conversation_id:
        if not valid_key(f.conversation_id):
            return [], False
        snap = coll.document(f.conversation_id).get()
        return ([snap.to_dict()] if snap.exists else []), False
    cap = SCAN_CAP
    if f.property_id:
        query = coll.where("propertyIds", "array_contains", f.property_id)
    elif f.remark_status and f.remark_status != "none":
        query = coll.where("remarkStatuses", "array_contains", f.remark_status)
    elif f.q and tokens_of(f.q, 10):
        word = max(tokens_of(f.q, 10), key=len)
        query = coll.where("searchTokens", "array_contains", word)
        cap = SEARCH_CAP
    else:
        query = coll.order_by("lastMessageAt", direction="DESCENDING")
        if f.date_from:
            query = query.where("lastMessageAt", ">=", _aware(f.date_from))
        if f.date_to:
            query = query.where("lastMessageAt", "<=", _aware(f.date_to))
    docs = [d.to_dict() for d in query.limit(cap).stream()]
    return [d for d in docs if d], len(docs) >= cap


def _matches(header: Dict[str, Any], f: ListFilters) -> bool:
    owner = header.get("owner") or {}
    if f.agent_type and header.get("agentType") != f.agent_type:
        return False
    if f.role and (owner.get("role") or "").lower() != f.role.lower():
        return False
    if f.website and header.get("website") != f.website:
        return False
    if f.model and f.model.strip().lower() not in (header.get("model") or "").lower():
        return False
    if f.user:
        needle = f.user.strip().lower()
        if not (str(owner.get("userId") or "") == f.user.strip()
                or needle in (owner.get("email") or "").lower()
                or needle in (header.get("sessionId") or "").lower()):
            return False
    if f.property_id and f.property_id not in (header.get("propertyIds") or []):
        return False
    if f.remark_status:
        statuses = header.get("remarkStatuses") or []
        if f.remark_status == "none":
            if statuses:
                return False
        elif f.remark_status not in statuses:
            return False
    last = _aware(header.get("lastMessageAt"))
    if f.date_from and last < _aware(f.date_from):
        return False
    if f.date_to and last > _aware(f.date_to):
        return False
    return True


def _contains_text(client, header: Dict[str, Any], staff: AuthenticatedStaff, needle: str) -> bool:
    own_only = _own_messages_only(header, staff)
    for message in _read_messages(client, header["conversationId"]):
        if own_only and (message["actor"]["user_id"] or "") != str(staff.user_id):
            continue
        if needle in (message["content"] or "").lower():
            return True
    return False


def list_conversations(staff: AuthenticatedStaff, f: ListFilters, sort: str = "last_message_at",
                       order: str = "desc", page: int = 1, page_size: int = 25) -> Dict[str, Any]:
    client = _require_client()
    candidates, truncated = _candidates(client, f)
    visible = [h for h in candidates if can_view(h, staff) and _matches(h, f)]
    if f.q and f.q.strip():
        needle = f.q.strip().lower()
        visible = [h for h in visible if _contains_text(client, h, staff, needle)]
    field = SORTS.get(sort, "lastMessageAt")
    visible.sort(key=lambda h: _aware(h.get(field)) if field != "messageCount" else (h.get(field) or 0),
                 reverse=(order != "asc"))
    total = len(visible)
    start = (page - 1) * page_size
    return {
        "items": [summarize(h) for h in visible[start:start + page_size]],
        "page": page,
        "page_size": page_size,
        "total": total,
        "truncated": truncated,
    }


def get_conversation(staff: AuthenticatedStaff, conversation_id: str, page: int = 1, page_size: int = 100) -> Optional[Dict[str, Any]]:
    """The conversation and a page of its messages in order, or None when it does not exist or is not visible."""
    client = _require_client()
    if not valid_key(conversation_id):
        return None
    snap = client.collection(LOGS).document(conversation_id).get()
    header = snap.to_dict() if snap.exists else None
    if not header or not can_view(header, staff):
        return None
    messages = _read_messages(client, conversation_id)
    if _own_messages_only(header, staff):
        messages = [m for m in messages if (m["actor"]["user_id"] or "") == str(staff.user_id)]
    start = (page - 1) * page_size
    return {
        "conversation": summarize(header),
        "messages": messages[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total_messages": len(messages),
    }


def resolve_log_key(client, conversation_id: str) -> Optional[str]:
    """The log key for an id: the key itself, or a bare session id (what the Admin Panel lists) of either bot."""
    coll = client.collection(LOGS)
    if coll.document(conversation_id).get().exists:
        return conversation_id
    found = []
    for agent in ("public", "admin"):
        snap = coll.document(f"{agent}__{conversation_id}").get()
        if snap.exists:
            found.append(((snap.to_dict() or {}).get("lastMessageAt") or datetime.min.replace(tzinfo=timezone.utc), snap.id))
    return max(found)[1] if found else None


def read_for_review(conversation_id: str) -> Optional[List[Dict[str, Any]]]:
    """Messages of a conversation as {role, content, timestamp}, for the super-admin review. None if not logged."""
    try:
        client = _client()
        if client is None or not valid_key(conversation_id):
            return None
        key = resolve_log_key(client, conversation_id)
        if key is None:
            return None
        return [{"role": m["role"], "content": m["content"], "timestamp": m["created_at"]}
                for m in _read_messages(client, key)]
    except Exception:
        return None
