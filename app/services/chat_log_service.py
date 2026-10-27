"""
Persistent chat log in Firestore (admin project), written for both the user bot and the admin bot.

    chat_logs/{agent}__{session}                   one header per conversation
    chat_logs/{agent}__{session}/messages/m000001  one document per message, numbered in order

The existing `conversations` collection stays the bot's working memory (it is trimmed and rewritten). This
collection is the append-only history that administrators review.

Reads use only automatic single-field indexes, so no composite index has to be deployed: the list is read in
recency order (or through one array-contains filter), and every other filter, the access check and sorting run
in the backend (see chat_log_query.py).

Credentials found in text are removed before anything is stored; a failure to write a log never breaks a chat.
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.tenant import normalize_website_domain
from app.services.audit_service import scrub_text

logger = logging.getLogger(__name__)

LOGS = "chat_logs"
MESSAGES = "messages"
MAX_CONTENT_CHARS = 20000
MAX_PROPERTY_IDS = 200
MAX_TOKENS_PER_MESSAGE = 40
MAX_TOKENS_PER_HEADER = 400
PREVIEW_CHARS = 160
ERROR_STATUSES = {"error", "failed", "failure", "refused"}
TRANSACTION_ATTEMPTS = 25   # many writers on one conversation (e.g. a double submit) must not drop a message
WRITE_RETRIES = 3

_TOKEN_RE = re.compile(r"\w{3,}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client():
    from app.services.firestore_service import get_firestore_client

    return get_firestore_client(target="admin")


def log_key(agent_type: str, session_id: str) -> str:
    """Same key as the conversation document, so the two can always be matched."""
    from app.services.firestore_service import _conversation_doc_id

    return _conversation_doc_id(session_id, agent_type or "public")


def tokens_of(text: str, limit: int = MAX_TOKENS_PER_MESSAGE) -> List[str]:
    seen: List[str] = []
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token not in seen:
            seen.append(token)
            if len(seen) >= limit:
                break
    return seen


def _clean_text(value: Any, limit: int = MAX_CONTENT_CHARS) -> str:
    text = scrub_text(value if isinstance(value, str) else ("" if value is None else str(value))) or ""
    if len(text) > limit:
        text = text[:limit] + f" [truncated, {len(text) - limit} more characters]"
    return text


def _preview(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat[:PREVIEW_CHARS]


def collect_property_ids(*sources: Any) -> List[str]:
    """Property ids mentioned in structured data or tool results (bounded, shallow walk)."""
    found: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, (str, int)) and str(value).strip():
            pid = str(value).strip()
            if pid not in found and len(found) < MAX_PROPERTY_IDS:
                found.append(pid)

    def walk(node: Any, depth: int) -> None:
        if depth > 4 or len(found) >= MAX_PROPERTY_IDS:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("property_id", "propertyId"):
                    add(value)
                elif key == "property_ids" and isinstance(value, list):
                    for item in value[:MAX_PROPERTY_IDS]:
                        add(item)
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:100]:
                walk(item, depth + 1)

    for source in sources:
        walk(source, 0)
    return found


def summarize_tools(tool_calls: Optional[List[Dict[str, Any]]], tool_results: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """Which tools ran and how they ended. Arguments and results are never stored here."""
    summary: List[Dict[str, str]] = []
    for result in (tool_results or [])[:20]:
        if not isinstance(result, dict):
            continue
        name = result.get("tool") or result.get("name") or result.get("tool_name")
        if not name:
            continue
        outcome = result.get("status") or ("error" if result.get("error") else "ok")
        summary.append({"tool": _clean_text(name, 60), "status": _clean_text(outcome, 40)})
    if not summary:
        for call in (tool_calls or [])[:20]:
            if isinstance(call, dict) and (call.get("name") or call.get("tool")):
                summary.append({"tool": _clean_text(call.get("name") or call.get("tool"), 60), "status": "called"})
    return summary


def evidence_of(property_ids: List[str], tool_summary: List[Dict[str, str]],
                structured_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Where the answer came from: an approved correction, stored property records or tools, or nothing recorded."""
    correction_id = (structured_data or {}).get("correction_id") if isinstance(structured_data, dict) else None
    if correction_id:
        return {"evidenceStatus": "admin_knowledge",
                "sources": [{"type": "correction", "id": str(correction_id), "version": (structured_data or {}).get("correction_version")}]}
    sources: List[Dict[str, str]] = [{"type": "property", "id": pid} for pid in property_ids[:20]]
    sources += [{"type": "tool", "name": t["tool"]} for t in tool_summary[:10]]
    return {"evidenceStatus": "database" if (property_ids or tool_summary) else "none", "sources": sources}


def response_status_of(tool_summary: List[Dict[str, str]]) -> str:
    return "error" if any(t["status"].lower() in ERROR_STATUSES for t in tool_summary) else "ok"


def actor_of(conversation: Any) -> Dict[str, Optional[str]]:
    """Who is chatting: the signed-in staff member for the admin bot, an anonymous visitor otherwise."""
    actor = getattr(conversation, "log_actor", None)
    if isinstance(actor, dict) and (actor.get("user_id") or actor.get("email")):
        return {
            "userId": str(actor.get("user_id")) if actor.get("user_id") else None,
            "email": actor.get("email"),
            "role": actor.get("role") or "worker",
        }
    return {"userId": None, "email": None, "role": "public"}


def website_of(site_origin: Optional[str]) -> str:
    return normalize_website_domain(site_origin) or ""


def record_turn(
    conversation: Any,
    user_message: str,
    assistant_message: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
    structured_data: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
) -> bool:
    """Append one user message and the bot's reply to the log. Returns whether it was stored."""
    if not getattr(settings, "ENABLE_CHAT_LOG_STORE", True) or conversation is None:
        return False
    try:
        client = _client()
        if client is None:
            return False
        from google.cloud import firestore

        agent_type = getattr(conversation, "agent_type", "public") or "public"
        session_id = getattr(conversation, "session_id", None) or "anonymous"
        key = log_key(agent_type, session_id)
        site_origin = getattr(conversation, "site_origin", None) or ""
        website = website_of(site_origin)
        actor = actor_of(conversation)
        language = (getattr(conversation, "preferences", None) or {}).get("language")

        question = _clean_text(user_message)
        answer = _clean_text(assistant_message)
        property_ids = collect_property_ids(structured_data, tool_results)
        tool_summary = summarize_tools(tool_calls, tool_results)
        evidence = evidence_of(property_ids, tool_summary, structured_data)
        status = response_status_of(tool_summary)
        now = _now()

        header_ref = client.collection(LOGS).document(key)

        def message_doc(seq: int, role: str, content: str, created_at: datetime) -> Dict[str, Any]:
            doc: Dict[str, Any] = {
                "seq": seq, "role": role, "content": content, "createdAt": created_at,
                "agentType": agent_type, "website": website, "actor": actor,
                "language": language, "remarkStatus": "none",
                "tokens": tokens_of(content),
            }
            if role == "assistant":
                doc.update({
                    "propertyIds": property_ids, "evidenceStatus": evidence["evidenceStatus"], "sources": evidence["sources"],
                    "responseStatus": status, "toolSummary": tool_summary, "latencyMs": latency_ms,
                    "errorMessage": _clean_text(answer, 500) if status == "error" else None,
                })
            return doc

        @firestore.transactional
        def write(txn):
            snap = header_ref.get(transaction=txn)
            head = snap.to_dict() if snap.exists else {}
            base = int(head.get("messageCount") or 0)
            txn.set(header_ref.collection(MESSAGES).document(f"m{base + 1:06d}"), message_doc(base + 1, "user", question, now))
            txn.set(header_ref.collection(MESSAGES).document(f"m{base + 2:06d}"), message_doc(base + 2, "assistant", answer, now))

            def union(existing: Any, new: List[str], cap: int) -> List[str]:
                merged = list(existing or [])
                merged += [v for v in new if v not in merged]
                return merged[-cap:] if len(merged) > cap else merged

            actor_ids = union(head.get("actorUserIds"), [actor["userId"]] if actor["userId"] else [], 50)
            header = {
                "conversationId": key, "agentType": agent_type, "sessionId": session_id,
                "siteOrigin": site_origin, "website": website,
                "owner": head.get("owner") or actor,
                "actorUserIds": actor_ids,
                "createdAt": head.get("createdAt") or now,
                "lastMessageAt": now,
                "messageCount": base + 2,
                "propertyIds": union(head.get("propertyIds"), property_ids, MAX_PROPERTY_IDS),
                "remarkStatuses": head.get("remarkStatuses") or [],
                "searchTokens": union(head.get("searchTokens"), tokens_of(question + " " + answer, 80), MAX_TOKENS_PER_HEADER),
                "firstQuestionPreview": head.get("firstQuestionPreview") or _preview(question),
                "lastQuestionPreview": _preview(question),
                "language": language,
            }
            txn.set(header_ref, header, merge=True)

        for attempt in range(WRITE_RETRIES):
            try:
                write(client.transaction(max_attempts=TRANSACTION_ATTEMPTS))
                return True
            except Exception as exc:
                if attempt == WRITE_RETRIES - 1:
                    raise
                time.sleep(random.uniform(0.05, 0.25) * (attempt + 1))
        return False
    except Exception as exc:  # never let logging break a chat
        logger.warning("Chat log write failed (chat unaffected): %s", exc)
        return False
