"""
Admin-side chat and visitor log helpers.

Reads chat_query_logs (visitor) and writes/reads admin_chat_logs.
Never raises to the caller for logging failures. Tolerant of missing fields.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

VISITOR_COLLECTION = "chat_query_logs"
ADMIN_COLLECTION = "admin_chat_logs"
_UNAVAILABLE = "Logs are not available right now."
_STATS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_STATS_TTL_SEC = 60.0

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(
    r"(?<!\w)(\+?\d[\d\s().-]{6,}\d)(?!\w)"
)
_SECRET_KEY = re.compile(
    r"(confirmation[_-]?token|password|api[_-]?key|authorization|secret|access_token)",
    re.I,
)


def mask_pii(text: Optional[str]) -> str:
    """Keep first 2 and last 2 characters of local-part / phone digits."""
    if text is None:
        return ""
    raw = str(text)

    def _email(match: re.Match) -> str:
        local, domain = match.group(1), match.group(2)
        if len(local) <= 4:
            shown = local[:1] + "***" + (local[-1:] if len(local) > 1 else "")
        else:
            shown = local[:2] + "***" + local[-2:]
        return f"{shown}@{domain}"

    def _phone(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) <= 4:
            return "***"
        return digits[:2] + "***" + digits[-2:]

    masked = _EMAIL_RE.sub(_email, raw)
    masked = _PHONE_RE.sub(_phone, masked)
    return masked


def _truncate(text: Any, limit: int = 4000) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


_FIELD_ALIASES = {
    "log_id": ("log_id", "logId", "id"),
    "session_id": ("session_id", "sessionId"),
    "conversation_id": ("conversation_id", "conversationId"),
    "site_origin": ("site_origin", "siteOrigin", "site"),
    "created_at": ("created_at", "createdAt", "timestamp"),
    "turn_index": ("turn_index", "turnIndex"),
    "user_message": ("user_message", "userMessage", "userQuestion", "question"),
    "bot_reply": ("bot_reply", "botReply", "aiResponse", "answer", "reply"),
    "response_type": ("response_type", "responseType"),
    "matched_property_ids": ("matched_property_ids", "matchedPropertyIds", "matchedProperties"),
    "cards_count": ("cards_count", "cardsCount"),
    "quick_replies": ("quick_replies", "quickReplies"),
    "used_llm": ("used_llm", "usedLlm"),
    "fallback_type": ("fallback_type", "fallbackType"),
    "latency_ms": ("latency_ms", "latencyMs"),
    "flagged": ("flagged",),
    "corrected": ("corrected",),
    "correction_id": ("correction_id", "correctionId"),
    "staff_email": ("staff_email", "staffEmail"),
    "staff_role": ("staff_role", "staffRole"),
    "tools_used": ("tools_used", "toolsUsed"),
    "pending_action_id": ("pending_action_id", "pendingActionId"),
    "action_status": ("action_status", "actionStatus"),
    "language": ("language", "lang"),
    "intent": ("intent",),
}


def _get(row: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        for candidate in _FIELD_ALIASES.get(key, (key,)):
            if candidate in row and row[candidate] is not None:
                return row[candidate]
        if key in row and row[key] is not None:
            return row[key]
    return default


def normalize_log_row(raw: Any, *, collection: str) -> Dict[str, Any]:
    """Map a Firestore document (any shape) to the contract fields."""
    if not isinstance(raw, dict):
        return {}
    row = dict(raw)
    log_id = _get(row, "log_id", default=row.get("id") or "")
    matched = _get(row, "matched_property_ids", default=[]) or []
    if isinstance(matched, str):
        matched = [matched]
    tools = _get(row, "tools_used", default=[]) or []
    if isinstance(tools, str):
        tools = [tools]
    tool_names = []
    for item in tools:
        if isinstance(item, dict):
            name = item.get("name") or item.get("tool") or ""
            if name:
                tool_names.append(str(name))
        elif item:
            tool_names.append(str(item))
    out = {
        "log_id": str(log_id or ""),
        "session_id": str(_get(row, "session_id", default="") or ""),
        "conversation_id": str(_get(row, "conversation_id", default="") or ""),
        "site_origin": str(_get(row, "site_origin", default="") or ""),
        "language": str(_get(row, "language", default="") or ""),
        "created_at": str(_get(row, "created_at", default="") or ""),
        "turn_index": _get(row, "turn_index", default=0) or 0,
        "user_message": str(_get(row, "user_message", default="") or ""),
        "bot_reply": str(_get(row, "bot_reply", default="") or ""),
        "response_type": str(_get(row, "response_type", default="") or ""),
        "intent": str(_get(row, "intent", default="") or ""),
        "matched_property_ids": [str(x) for x in matched],
        "cards_count": int(_get(row, "cards_count", default=0) or 0),
        "quick_replies": list(_get(row, "quick_replies", default=[]) or []),
        "used_llm": bool(_get(row, "used_llm", default=False)),
        "fallback_type": str(_get(row, "fallback_type", default="none") or "none"),
        "latency_ms": _get(row, "latency_ms", default=None),
        "flagged": bool(_get(row, "flagged", default=False)),
        "corrected": bool(_get(row, "corrected", default=False)),
        "correction_id": _get(row, "correction_id", default=None),
        "staff_email": str(_get(row, "staff_email", default="") or ""),
        "staff_role": str(_get(row, "staff_role", default="") or ""),
        "tools_used": tool_names,
        "pending_action_id": _get(row, "pending_action_id", default=None),
        "action_status": _get(row, "action_status", default=None),
        "_collection": collection,
    }
    try:
        out["turn_index"] = int(out["turn_index"] or 0)
    except (TypeError, ValueError):
        out["turn_index"] = 0
    return out


def _firestore_client(target: str):
    from app.services.firestore_service import get_firestore_client

    return get_firestore_client(target=target)


def _stream_collection(collection: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Return (rows, error_message). rows is None only when Firestore is down."""
    target = "admin" if collection == ADMIN_COLLECTION else "user"
    try:
        client = _firestore_client(target)
        if client is None:
            return None, _UNAVAILABLE
        rows: List[Dict[str, Any]] = []
        for snap in client.collection(collection).stream():
            data = snap.to_dict() if hasattr(snap, "to_dict") else None
            if not isinstance(data, dict):
                data = {}
            payload = dict(data)
            payload.setdefault("id", getattr(snap, "id", None) or payload.get("log_id"))
            payload.setdefault("log_id", payload.get("id"))
            rows.append(normalize_log_row(payload, collection=collection))
        return rows, None
    except Exception as exc:
        logger.warning("Failed to read %s: %s", collection, exc)
        return None, _UNAVAILABLE


def _site_allowed(site: str, allowed: Optional[List[str]], *, super_admin: bool) -> bool:
    if super_admin:
        return True
    if not allowed:
        return False
    token = (site or "").strip().lower()
    return any(token == str(item).strip().lower() or token.endswith("." + str(item).strip().lower()) for item in allowed)


def _filter_rows(
    rows: List[Dict[str, Any]],
    *,
    site: Optional[str] = None,
    language: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    fallback_type: Optional[str] = None,
    flagged: Optional[bool] = None,
    corrected: Optional[bool] = None,
    property_id: Optional[str] = None,
    intent: Optional[str] = None,
    allowed_websites: Optional[List[str]] = None,
    super_admin: bool = False,
) -> List[Dict[str, Any]]:
    needle = (q or "").strip().casefold()
    out = []
    for row in rows:
        if not _site_allowed(str(row.get("site_origin") or ""), allowed_websites, super_admin=super_admin):
            continue
        if site and str(row.get("site_origin") or "").lower() != site.strip().lower():
            continue
        if language and str(row.get("language") or "").lower() != language.strip().lower():
            continue
        if session_id and str(row.get("session_id") or "") != session_id:
            continue
        if fallback_type is not None and str(row.get("fallback_type") or "none").lower() != fallback_type.strip().lower():
            continue
        if flagged is not None and bool(row.get("flagged")) is not flagged:
            continue
        if corrected is not None and bool(row.get("corrected")) is not corrected:
            continue
        if intent and str(row.get("intent") or "").lower() != intent.strip().lower():
            continue
        if property_id:
            ids = [str(x).lower() for x in (row.get("matched_property_ids") or [])]
            if property_id.strip().lower() not in ids:
                continue
        created = _parse_dt(row.get("created_at"))
        if date_from and (created is None or created < date_from):
            continue
        if date_to and (created is None or created > date_to):
            continue
        if needle:
            blob = f"{row.get('user_message') or ''} {row.get('bot_reply') or ''}".casefold()
            if needle not in blob:
                continue
        out.append(row)
    out.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return out


def query_logs(
    collection: str,
    *,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 50,
    offset: int = 0,
    allowed_websites: Optional[List[str]] = None,
    super_admin: bool = False,
) -> Dict[str, Any]:
    filters = filters or {}
    rows, err = _stream_collection(collection)
    if rows is None:
        return {"total": 0, "rows": [], "message": err or _UNAVAILABLE}
    filtered = _filter_rows(
        rows,
        site=filters.get("site"),
        language=filters.get("language"),
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
        session_id=filters.get("session_id"),
        q=filters.get("q"),
        fallback_type=filters.get("fallback_type"),
        flagged=filters.get("flagged"),
        corrected=filters.get("corrected"),
        property_id=filters.get("property_id"),
        intent=filters.get("intent"),
        allowed_websites=allowed_websites,
        super_admin=super_admin,
    )
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    page = filtered[offset : offset + limit]
    return {"total": len(filtered), "rows": page}


def query_sessions(
    collection: str,
    *,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 50,
    offset: int = 0,
    allowed_websites: Optional[List[str]] = None,
    super_admin: bool = False,
) -> Dict[str, Any]:
    result = query_logs(
        collection,
        filters=filters,
        limit=10_000,
        offset=0,
        allowed_websites=allowed_websites,
        super_admin=super_admin,
    )
    if result.get("message") and not result.get("rows") and result.get("total") == 0:
        return {"total": 0, "rows": [], "message": result.get("message")}
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in result.get("rows") or []:
        sid = str(row.get("session_id") or "") or f"unknown-{row.get('log_id')}"
        groups[sid].append(row)
    sessions = []
    for sid, turns in groups.items():
        ordered = sorted(turns, key=lambda item: (str(item.get("created_at") or ""), int(item.get("turn_index") or 0)))
        first = ordered[0]
        last = ordered[-1]
        sessions.append({
            "session_id": sid,
            "site_origin": last.get("site_origin") or first.get("site_origin") or "",
            "language": last.get("language") or first.get("language") or "",
            "first_seen": first.get("created_at") or "",
            "last_seen": last.get("created_at") or "",
            "turns": len(ordered),
            "flagged_turns": sum(1 for item in ordered if item.get("flagged")),
            "last_question": last.get("user_message") or "",
        })
    sessions.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    return {"total": len(sessions), "rows": sessions[offset : offset + limit]}


def query_session_turns(
    collection: str,
    session_id: str,
    *,
    allowed_websites: Optional[List[str]] = None,
    super_admin: bool = False,
) -> Dict[str, Any]:
    result = query_logs(
        collection,
        filters={"session_id": session_id},
        limit=1000,
        offset=0,
        allowed_websites=allowed_websites,
        super_admin=super_admin,
    )
    if result.get("message") and not result.get("rows"):
        return {"session_id": session_id, "total": 0, "rows": [], "message": result.get("message")}
    rows = sorted(
        result.get("rows") or [],
        key=lambda item: (int(item.get("turn_index") or 0), str(item.get("created_at") or "")),
    )
    return {"session_id": session_id, "total": len(rows), "rows": rows}


def query_stats(
    collection: str,
    *,
    allowed_websites: Optional[List[str]] = None,
    super_admin: bool = False,
) -> Dict[str, Any]:
    cache_key = f"{collection}|{sorted(allowed_websites or [])}|{super_admin}"
    cached = _STATS_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _STATS_TTL_SEC:
        return dict(cached[1])

    rows, err = _stream_collection(collection)
    if rows is None:
        payload = {
            "days": [],
            "top_questions": [],
            "top_flagged_questions": [],
            "top_matched_property_ids": [],
            "message": err or _UNAVAILABLE,
        }
        return payload

    filtered = _filter_rows(rows, allowed_websites=allowed_websites, super_admin=super_admin)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    recent = []
    for row in filtered:
        created = _parse_dt(row.get("created_at"))
        if created and created >= cutoff:
            recent.append(row)

    by_day: Dict[str, Dict[str, Any]] = {}
    question_counts: Counter = Counter()
    flagged_counts: Counter = Counter()
    property_counts: Counter = Counter()
    sessions_by_day: Dict[str, set] = defaultdict(set)

    for row in recent:
        created = _parse_dt(row.get("created_at"))
        day = created.strftime("%Y-%m-%d") if created else "unknown"
        bucket = by_day.setdefault(day, {"date": day, "turns": 0, "sessions": 0, "flagged": 0})
        bucket["turns"] += 1
        if row.get("flagged"):
            bucket["flagged"] += 1
        sid = str(row.get("session_id") or "")
        if sid:
            sessions_by_day[day].add(sid)
        q = (row.get("user_message") or "").strip()
        if q:
            question_counts[q] += 1
            if row.get("flagged"):
                flagged_counts[q] += 1
        for pid in row.get("matched_property_ids") or []:
            if pid:
                property_counts[str(pid)] += 1

    for day, sids in sessions_by_day.items():
        by_day[day]["sessions"] = len(sids)

    days = sorted(by_day.values(), key=lambda item: item["date"])
    payload = {
        "days": days,
        "top_questions": [{"question": q, "count": c} for q, c in question_counts.most_common(20)],
        "top_flagged_questions": [{"question": q, "count": c} for q, c in flagged_counts.most_common(20)],
        "top_matched_property_ids": [{"property_id": p, "count": c} for p, c in property_counts.most_common(20)],
    }
    _STATS_CACHE[cache_key] = (now, payload)
    return dict(payload)


def reset_stats_cache() -> None:
    _STATS_CACHE.clear()


def log_admin_chat_turn(
    *,
    session_id: Optional[str],
    conversation_id: Optional[str],
    staff: Any,
    site_origin: Optional[str],
    user_message: Optional[str],
    reply: Optional[Dict[str, Any]],
    language: Optional[str],
    latency_ms: Optional[int] = None,
    turn_index: Optional[int] = None,
) -> Optional[str]:
    """Write one admin_chat_logs row. Never raises. Returns log_id or None."""
    from app.core.config import settings

    if not getattr(settings, "ADMIN_CHAT_LOGGING", True):
        return None
    try:
        client = _firestore_client("admin")
        if client is None:
            return None
        text = "" if reply is None else str(reply.get("text") or "")
        tools = []
        for item in (reply or {}).get("tools_used") or []:
            if isinstance(item, dict):
                name = item.get("name") or item.get("tool")
                if name:
                    tools.append(str(name))
            elif item:
                tools.append(str(item))
        user_text = str(user_message or "")
        bot_text = _truncate(text, 4000)
        if getattr(settings, "CHAT_LOG_MASK_PII", True):
            user_text = mask_pii(user_text)
            bot_text = mask_pii(bot_text)
        # Drop secrets if somehow present in message text
        for pattern in (
            r"(?i)confirmation[_ ]?token\s*[:=]\s*\S+",
            r"(?i)authorization\s*[:=]\s*\S+",
            r"(?i)api[_-]?key\s*[:=]\s*\S+",
            r"(?i)password\s*[:=]\s*\S+",
        ):
            user_text = re.sub(pattern, "[REDACTED]", user_text)
            bot_text = re.sub(pattern, "[REDACTED]", bot_text)

        log_id = uuid.uuid4().hex
        payload = {
            "log_id": log_id,
            "session_id": session_id or "",
            "conversation_id": str(conversation_id or (reply or {}).get("conversation_id") or ""),
            "staff_email": getattr(staff, "email", None) or "",
            "staff_role": getattr(staff, "role", None) or "",
            "site_origin": site_origin or "",
            "created_at": _now_iso(),
            "turn_index": int(turn_index or 0),
            "user_message": user_text,
            "bot_reply": bot_text,
            "response_type": str((reply or {}).get("type") or "text"),
            "tools_used": tools,
            "pending_action_id": (reply or {}).get("pending_action_id"),
            "action_status": (reply or {}).get("action_status"),
            "latency_ms": latency_ms,
            "language": language or (reply or {}).get("language") or "en",
        }
        # Never store confirmation tokens even if keys sneak in
        for key in list(payload.keys()):
            if _SECRET_KEY.search(str(key)):
                payload.pop(key, None)
        client.collection(ADMIN_COLLECTION).document(log_id).set(payload)
        return log_id
    except Exception as exc:
        logger.warning("admin chat log write failed: %s", exc)
        return None


def purge_chat_logs(*, dry_run: bool = True) -> Dict[str, Any]:
    """Delete (or count) rows older than CHAT_LOG_RETENTION_DAYS from both collections."""
    from app.core.config import settings

    days = int(getattr(settings, "CHAT_LOG_RETENTION_DAYS", 180) or 180)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    counts = {VISITOR_COLLECTION: 0, ADMIN_COLLECTION: 0}
    deleted = {VISITOR_COLLECTION: 0, ADMIN_COLLECTION: 0}
    errors = []

    for collection, target in ((VISITOR_COLLECTION, "user"), (ADMIN_COLLECTION, "admin")):
        try:
            client = _firestore_client(target)
            if client is None:
                errors.append(f"{collection}: unavailable")
                continue
            snaps = list(client.collection(collection).stream())
            for snap in snaps:
                data = snap.to_dict() if hasattr(snap, "to_dict") else {}
                if not isinstance(data, dict):
                    data = {}
                created = _parse_dt(_get(data, "created_at", default=data.get("timestamp")))
                if created is None or created >= cutoff:
                    continue
                counts[collection] += 1
                if not dry_run:
                    try:
                        snap.reference.delete()
                        deleted[collection] += 1
                    except Exception as exc:
                        errors.append(f"{collection}/{getattr(snap, 'id', '?')}: {exc}")
        except Exception as exc:
            errors.append(f"{collection}: {exc}")

    return {
        "dry_run": bool(dry_run),
        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "retention_days": days,
        "eligible": counts,
        "deleted": deleted if not dry_run else {VISITOR_COLLECTION: 0, ADMIN_COLLECTION: 0},
        "total_eligible": sum(counts.values()),
        "total_deleted": sum(deleted.values()) if not dry_run else 0,
        "errors": errors,
    }


def parse_bool_param(value: Optional[str]) -> Optional[bool]:
    if value is None or value == "":
        return None
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError("invalid boolean")


def parse_date_param(value: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            dt = dt + timedelta(hours=23, minutes=59, seconds=59)
        return dt
    dt = _parse_dt(text)
    if dt is None:
        raise ValueError("invalid date")
    return dt
