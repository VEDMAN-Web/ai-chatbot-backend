"""
Remarks and answer corrections on logged chat responses (Firestore).

    chat_log_remarks/{RM-id}                 an internal remark on one bot response (+ /revisions/r0001...)
    answer_corrections/{COR-id}              a proposed corrected answer for one response (+ /revisions/r0001...)

A remark is internal feedback. It never changes what the bot says. A correction is a separate record with its own
review: only a correction a super admin has approved is returned by `active_corrections()`. The logged messages
are never edited, and a correction keeps a copy of the original question and answer.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.auth import AuthenticatedStaff
from app.core.config import settings
from app.core.permissions import CHAT_LOGS_APPROVE_CORRECTION, permissions_for
from app.core.tenant import normalize_website_list
from app.services import chat_log_query as query
from app.services.audit_service import scrub_text
from app.services.chat_log_service import LOGS, MESSAGES

REMARKS = "chat_log_remarks"
CORRECTIONS = "answer_corrections"
REVISIONS = "revisions"

REMARK_STATUSES = ("open", "in_review", "resolved", "rejected")
CORRECTION_STATUSES = ("proposed", "approved", "rejected", "inactive")
EDITABLE_REMARK_STATUSES = ("open", "in_review")
ISSUE_MIN, ISSUE_MAX = 3, 2000
ANSWER_MIN, ANSWER_MAX = 3, 5000
NOTE_MAX = 1000
LIST_SCAN_CAP = 1000


class RemarkError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _identity(staff: AuthenticatedStaff) -> Dict[str, Optional[str]]:
    return {"userId": str(staff.user_id), "email": staff.email, "role": staff.role}


def _is_reviewer(staff: AuthenticatedStaff) -> bool:
    return CHAT_LOGS_APPROVE_CORRECTION in permissions_for(staff.role)


def _text(value: Any, label: str, low: int, high: int, required: bool = True) -> Optional[str]:
    if value is None:
        if required:
            raise RemarkError(422, f"{label} is required.")
        return None
    cleaned = (scrub_text(str(value)) or "").strip()
    if not cleaned and not required:
        return None
    if len(cleaned) < low or len(cleaned) > high:
        raise RemarkError(422, f"{label} must be between {low} and {high} characters.")
    return cleaned


def _iso(value: Any) -> Optional[str]:
    return query._iso(value)


def _serialize_remark(doc_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "remark_id": doc_id,
        "conversation_id": data.get("conversationId"),
        "message_id": data.get("messageId"),
        "kind": "remark",
        "issue": data.get("issue"),
        "suggested_answer": data.get("suggestedAnswer"),
        "status": data.get("status"),
        "author": {"user_id": (data.get("author") or {}).get("userId"), "email": (data.get("author") or {}).get("email"),
                   "role": (data.get("author") or {}).get("role")},
        "created_at": _iso(data.get("createdAt")),
        "updated_at": _iso(data.get("updatedAt")),
        "revision": data.get("revision") or 1,
        "correction_id": data.get("correctionId"),
        "knowledge_entry_id": data.get("knowledgeEntryId"),
        "website": data.get("website") or None,
    }


def _serialize_correction(doc_id: str, data: Dict[str, Any], include_embedding: bool = False) -> Dict[str, Any]:
    def person(key: str) -> Optional[Dict[str, Any]]:
        who = data.get(key)
        return {"user_id": who.get("userId"), "email": who.get("email"), "role": who.get("role")} if who else None

    result = {
        "correction_id": doc_id,
        "kind": "answer_correction",
        "remark_id": data.get("remarkId"),
        "conversation_id": data.get("conversationId"),
        "message_id": data.get("messageId"),
        "question": data.get("question"),
        "original_answer": data.get("originalAnswer"),
        "corrected_answer": data.get("correctedAnswer"),
        "scope": data.get("scope") or {},
        "scope_type": data.get("scopeType"),
        "category": data.get("category"),
        "expires_at": _iso(data.get("expiresAt")),
        "status": data.get("status"),
        "version": data.get("version") or 0,
        "revision": data.get("revision") or 1,
        "proposed_by": person("proposedBy"),
        "proposed_at": _iso(data.get("proposedAt")),
        "reviewed_by": person("reviewedBy"),
        "reviewed_at": _iso(data.get("reviewedAt")),
        "review_note": data.get("reviewNote"),
    }
    if include_embedding:
        result["embedding"] = data.get("embedding")
    return result


# ---- helpers ------------------------------------------------------------------------------------

def _header_for(client, staff: AuthenticatedStaff, conversation_id: str) -> Dict[str, Any]:
    """The conversation's header if the caller may see it; otherwise 404 (its existence is not revealed)."""
    if not query.valid_key(conversation_id):
        raise RemarkError(404, "Conversation not found.")
    snap = client.collection(LOGS).document(conversation_id).get()
    header = snap.to_dict() if snap.exists else None
    if not header or not query.can_view(header, staff):
        raise RemarkError(404, "Conversation not found.")
    return header


def _rollup(statuses: List[str]) -> str:
    for status in REMARK_STATUSES:
        if status in statuses:
            return status
    return "none"


def _sync_statuses(txn, client, conversation_id: str, message_id: str, overrides: Dict[str, str]) -> None:
    """Refresh the status shown on the message and the filter list on the header. Call after all reads, before writes."""
    all_remarks = {d.id: d.to_dict() for d in client.collection(REMARKS).where("conversationId", "==", conversation_id).stream(transaction=txn)}
    for remark_id, status in overrides.items():
        all_remarks.setdefault(remark_id, {})["status"] = status
        all_remarks[remark_id].setdefault("messageId", message_id)
    header_statuses = sorted({r.get("status") for r in all_remarks.values() if r.get("status")}, key=REMARK_STATUSES.index)
    message_statuses = [r.get("status") for r in all_remarks.values() if r.get("messageId") == message_id]
    header_ref = client.collection(LOGS).document(conversation_id)
    txn.update(header_ref.collection(MESSAGES).document(message_id), {"remarkStatus": _rollup(message_statuses)})
    txn.update(header_ref, {"remarkStatuses": header_statuses})


def _transaction(client):
    return client.transaction(max_attempts=10)


# ---- remarks ------------------------------------------------------------------------------------

def create_remark(staff: AuthenticatedStaff, conversation_id: str, message_id: str,
                  issue: Any, suggested_answer: Any = None) -> Dict[str, Any]:
    client = query._require_client()
    from google.cloud import firestore

    issue_text = _text(issue, "The issue", ISSUE_MIN, ISSUE_MAX)
    suggestion = _text(suggested_answer, "The suggested answer", ANSWER_MIN, ANSWER_MAX, required=False)
    header = _header_for(client, staff, conversation_id)
    message_ref = client.collection(LOGS).document(conversation_id).collection(MESSAGES).document(message_id)
    remark_id = f"RM-{uuid.uuid4().hex[:12].upper()}"
    remark_ref = client.collection(REMARKS).document(remark_id)
    now = _now()

    @firestore.transactional
    def write(txn):
        message = message_ref.get(transaction=txn)
        if not message.exists:
            raise RemarkError(404, "Message not found.")
        if (message.to_dict() or {}).get("role") != "assistant":
            raise RemarkError(422, "Remarks are added to the bot's responses.")
        doc = {
            "conversationId": conversation_id, "messageId": message_id, "issue": issue_text, "suggestedAnswer": suggestion,
            "status": "open", "author": _identity(staff), "createdAt": now, "updatedAt": now, "revision": 1,
            "correctionId": None, "knowledgeEntryId": None, "website": header.get("website") or "",
            "agentType": header.get("agentType"),
        }
        _sync_statuses(txn, client, conversation_id, message_id, {remark_id: "open"})
        txn.set(remark_ref, doc)
        txn.set(remark_ref.collection(REVISIONS).document("r0001"), {
            "revision": 1, "at": now, "by": _identity(staff), "change": "created",
            "snapshot": {"issue": issue_text, "suggestedAnswer": suggestion, "status": "open"},
        })
        return doc

    return _serialize_remark(remark_id, write(_transaction(client)))


def _load_remark(client, staff: AuthenticatedStaff, remark_id: str) -> Dict[str, Any]:
    snap = client.collection(REMARKS).document(remark_id).get() if query.valid_key(remark_id) else None
    if snap is None or not snap.exists:
        raise RemarkError(404, "Remark not found.")
    data = snap.to_dict()
    _header_for(client, staff, data["conversationId"])      # same visibility as the conversation
    return data


def get_remark(staff: AuthenticatedStaff, remark_id: str) -> Dict[str, Any]:
    client = query._require_client()
    return _serialize_remark(remark_id, _load_remark(client, staff, remark_id))


def update_remark(staff: AuthenticatedStaff, remark_id: str, issue: Any = None, suggested_answer: Any = None,
                  status: Optional[str] = None, clear_suggestion: bool = False) -> Dict[str, Any]:
    client = query._require_client()
    from google.cloud import firestore

    if issue is None and suggested_answer is None and status is None and not clear_suggestion:
        raise RemarkError(422, "Nothing to change.")
    if status is not None and status not in REMARK_STATUSES:
        raise RemarkError(422, f"status must be one of {', '.join(REMARK_STATUSES)}.")
    new_issue = _text(issue, "The issue", ISSUE_MIN, ISSUE_MAX, required=False) if issue is not None else None
    new_suggestion = _text(suggested_answer, "The suggested answer", ANSWER_MIN, ANSWER_MAX, required=False) if suggested_answer is not None else None
    current = _load_remark(client, staff, remark_id)
    remark_ref = client.collection(REMARKS).document(remark_id)
    is_author = (current.get("author") or {}).get("userId") == str(staff.user_id)
    wants_text_change = new_issue is not None or new_suggestion is not None or clear_suggestion
    if wants_text_change and not (is_author or _is_reviewer(staff)):
        raise RemarkError(403, "Only the author or a reviewer can edit this remark.")
    if wants_text_change and current.get("status") not in EDITABLE_REMARK_STATUSES:
        raise RemarkError(409, "A resolved or rejected remark can no longer be edited.")
    if status is not None and status != current.get("status") and not _is_reviewer(staff):
        raise RemarkError(403, "Only a reviewer can change the status of a remark.")
    now = _now()

    @firestore.transactional
    def write(txn):
        snap = remark_ref.get(transaction=txn)
        data = snap.to_dict()
        changes: Dict[str, Any] = {}
        if new_issue is not None and new_issue != data.get("issue"):
            changes["issue"] = new_issue
        if clear_suggestion and data.get("suggestedAnswer") is not None:
            changes["suggestedAnswer"] = None
        elif new_suggestion is not None and new_suggestion != data.get("suggestedAnswer"):
            changes["suggestedAnswer"] = new_suggestion
        if status is not None and status != data.get("status"):
            changes["status"] = status
        if not changes:
            return data
        revision = int(data.get("revision") or 1) + 1
        updated = {**data, **changes, "updatedAt": now, "revision": revision}
        _sync_statuses(txn, client, data["conversationId"], data["messageId"], {remark_id: updated["status"]})
        txn.update(remark_ref, {**changes, "updatedAt": now, "revision": revision})
        txn.set(remark_ref.collection(REVISIONS).document(f"r{revision:04d}"), {
            "revision": revision, "at": now, "by": _identity(staff), "change": "updated",
            "changed": {k: {"from": data.get(k), "to": v} for k, v in changes.items()},
            "snapshot": {"issue": updated.get("issue"), "suggestedAnswer": updated.get("suggestedAnswer"), "status": updated.get("status")},
        })
        return updated

    return _serialize_remark(remark_id, write(_transaction(client)))


def list_revisions(staff: AuthenticatedStaff, kind: str, item_id: str) -> List[Dict[str, Any]]:
    client = query._require_client()
    if kind == "remark":
        _load_remark(client, staff, item_id)
        parent = client.collection(REMARKS).document(item_id)
    else:
        _load_correction(client, staff, item_id)
        parent = client.collection(CORRECTIONS).document(item_id)
    out = []
    for doc in parent.collection(REVISIONS).order_by("revision").stream():
        d = doc.to_dict()
        out.append({"revision": d.get("revision"), "at": _iso(d.get("at")), "by": d.get("by"), "change": d.get("change"),
                    "changed": d.get("changed"), "snapshot": d.get("snapshot"), "note": d.get("note")})
    return out


def list_remarks(staff: AuthenticatedStaff, conversation_id: Optional[str] = None, message_id: Optional[str] = None,
                 status: Optional[str] = None, author: Optional[str] = None, page: int = 1, page_size: int = 25) -> Dict[str, Any]:
    client = query._require_client()
    coll = client.collection(REMARKS)
    if conversation_id:
        _header_for(client, staff, conversation_id)
        docs = [(d.id, d.to_dict()) for d in coll.where("conversationId", "==", conversation_id).limit(LIST_SCAN_CAP).stream()]
    else:
        docs = [(d.id, d.to_dict()) for d in coll.order_by("createdAt", direction="DESCENDING").limit(LIST_SCAN_CAP).stream()]
    headers: Dict[str, bool] = {}

    def visible(conv_id: str) -> bool:
        if conv_id not in headers:
            snap = client.collection(LOGS).document(conv_id).get()
            headers[conv_id] = bool(snap.exists and query.can_view(snap.to_dict(), staff))
        return headers[conv_id]

    rows = [(i, d) for i, d in docs if visible(d.get("conversationId", ""))]
    if message_id:
        rows = [(i, d) for i, d in rows if d.get("messageId") == message_id]
    if status:
        rows = [(i, d) for i, d in rows if d.get("status") == status]
    if author:
        needle = author.strip().lower()
        rows = [(i, d) for i, d in rows if (d.get("author") or {}).get("userId") == author.strip()
                or needle in ((d.get("author") or {}).get("email") or "").lower()]
    rows.sort(key=lambda r: query._aware(r[1].get("createdAt")), reverse=True)
    start = (page - 1) * page_size
    return {"items": [_serialize_remark(i, d) for i, d in rows[start:start + page_size]], "page": page,
            "page_size": page_size, "total": len(rows)}


# ---- corrections --------------------------------------------------------------------------------

def _load_correction(client, staff: AuthenticatedStaff, correction_id: str) -> Dict[str, Any]:
    snap = client.collection(CORRECTIONS).document(correction_id).get() if query.valid_key(correction_id) else None
    if snap is None or not snap.exists:
        raise RemarkError(404, "Correction not found.")
    data = snap.to_dict()
    _header_for(client, staff, data["conversationId"])
    return data


def _scope(staff: AuthenticatedStaff, header: Dict[str, Any], websites: Any, languages: Any, property_ids: Any) -> Dict[str, Any]:
    allowed = set(staff.allowed_websites or [])
    sites = normalize_website_list(websites) if websites else ([header["website"]] if header.get("website") else [])
    if not sites:
        raise RemarkError(422, "Choose at least one website for the correction.")
    outside = [s for s in sites if s not in allowed]
    if outside:
        raise RemarkError(403, f"You do not have access to website(s): {outside}.")
    langs = sorted({str(x).strip().lower() for x in (languages or []) if str(x).strip()})[:10]
    pids = sorted({str(x).strip() for x in (property_ids or []) if str(x).strip()})[:50]
    return {"websites": sites, "languages": langs, "propertyIds": pids}


def propose_correction(staff: AuthenticatedStaff, remark_id: str, corrected_answer: Any, websites: Any = None,
                       languages: Any = None, property_ids: Any = None) -> Dict[str, Any]:
    client = query._require_client()
    from google.cloud import firestore

    corrected = _text(corrected_answer, "The corrected answer", ANSWER_MIN, ANSWER_MAX)
    remark = _load_remark(client, staff, remark_id)
    header = _header_for(client, staff, remark["conversationId"])
    conv_ref = client.collection(LOGS).document(remark["conversationId"])
    answer_ref = conv_ref.collection(MESSAGES).document(remark["messageId"])
    if property_ids is None:
        # Unless told otherwise, a correction only applies where the corrected response was about the same properties,
        # so a fix for one property is not given as the answer for another. Pass an empty list for a general correction.
        property_ids = (answer_ref.get().to_dict() or {}).get("propertyIds") or []
    scope = _scope(staff, header, websites, languages, property_ids)
    existing = [d for d in client.collection(CORRECTIONS).where("remarkId", "==", remark_id).stream() if (d.to_dict() or {}).get("status") != "rejected"]
    if existing:
        raise RemarkError(409, "This remark already has a correction. Edit it instead.")
    correction_id = f"COR-{uuid.uuid4().hex[:12].upper()}"
    correction_ref = client.collection(CORRECTIONS).document(correction_id)
    remark_ref = client.collection(REMARKS).document(remark_id)
    now = _now()

    @firestore.transactional
    def write(txn):
        answer = answer_ref.get(transaction=txn).to_dict() or {}
        question_ref = conv_ref.collection(MESSAGES).document(f"m{int(answer.get('seq') or 2) - 1:06d}")
        question = question_ref.get(transaction=txn).to_dict() or {}
        doc = {
            "remarkId": remark_id, "conversationId": remark["conversationId"], "messageId": remark["messageId"],
            "question": question.get("content") or "", "originalAnswer": answer.get("content") or "",
            "correctedAnswer": corrected, "scope": scope, "status": "proposed", "version": 0, "revision": 1,
            "proposedBy": _identity(staff), "proposedAt": now, "reviewedBy": None, "reviewedAt": None, "reviewNote": None,
            "language": answer.get("language"),
        }
        _sync_statuses(txn, client, remark["conversationId"], remark["messageId"], {remark_id: "in_review"})
        txn.set(correction_ref, doc)
        txn.set(correction_ref.collection(REVISIONS).document("r0001"), {
            "revision": 1, "at": now, "by": _identity(staff), "change": "proposed",
            "snapshot": {"correctedAnswer": corrected, "scope": scope, "status": "proposed"},
        })
        txn.update(remark_ref, {"correctionId": correction_id, "status": "in_review", "updatedAt": now,
                                "revision": int(remark.get("revision") or 1) + 1})
        txn.set(remark_ref.collection(REVISIONS).document(f"r{int(remark.get('revision') or 1) + 1:04d}"), {
            "revision": int(remark.get("revision") or 1) + 1, "at": now, "by": _identity(staff), "change": "correction_proposed",
            "snapshot": {"status": "in_review", "correctionId": correction_id},
        })
        return doc

    return _serialize_correction(correction_id, write(_transaction(client)))


def update_correction(staff: AuthenticatedStaff, correction_id: str, corrected_answer: Any = None, websites: Any = None,
                      languages: Any = None, property_ids: Any = None) -> Dict[str, Any]:
    """Edit a correction. Any edit puts it back to 'proposed': an edited answer must be approved again."""
    client = query._require_client()
    from google.cloud import firestore

    current = _load_correction(client, staff, correction_id)
    is_author = (current.get("proposedBy") or {}).get("userId") == str(staff.user_id)
    if not (is_author or _is_reviewer(staff)):
        raise RemarkError(403, "Only the author or a reviewer can edit this correction.")
    corrected = _text(corrected_answer, "The corrected answer", ANSWER_MIN, ANSWER_MAX, required=False) if corrected_answer is not None else None
    header = _header_for(client, staff, current["conversationId"])
    scope = _scope(staff, header, websites or current["scope"].get("websites"), languages if languages is not None else current["scope"].get("languages"),
                   property_ids if property_ids is not None else current["scope"].get("propertyIds")) if (websites is not None or languages is not None or property_ids is not None) else None
    if corrected is None and scope is None:
        raise RemarkError(422, "Nothing to change.")
    ref = client.collection(CORRECTIONS).document(correction_id)
    now = _now()

    @firestore.transactional
    def write(txn):
        data = ref.get(transaction=txn).to_dict()
        revision = int(data.get("revision") or 1) + 1
        changes: Dict[str, Any] = {"status": "proposed", "reviewedBy": None, "reviewedAt": None, "reviewNote": None,
                                   "revision": revision}
        if corrected is not None:
            changes["correctedAnswer"] = corrected
        if scope is not None:
            changes["scope"] = scope
        txn.update(ref, changes)
        txn.set(ref.collection(REVISIONS).document(f"r{revision:04d}"), {
            "revision": revision, "at": now, "by": _identity(staff), "change": "edited",
            "changed": {"correctedAnswer": {"from": data.get("correctedAnswer"), "to": corrected} if corrected is not None else None,
                        "status": {"from": data.get("status"), "to": "proposed"}},
            "snapshot": {"correctedAnswer": changes.get("correctedAnswer", data.get("correctedAnswer")),
                         "scope": changes.get("scope", data.get("scope")), "status": "proposed"},
        })
        return {**data, **changes}

    return _serialize_correction(correction_id, write(_transaction(client)))


_DECISIONS = {
    # decision: (allowed current statuses, new status)
    "approve": (("proposed", "inactive"), "approved"),
    "reject": (("proposed",), "rejected"),
    "deactivate": (("approved",), "inactive"),
}


def decide_correction(db, staff: AuthenticatedStaff, correction_id: str, decision: str, note: Any = None) -> Dict[str, Any]:
    """Approve, reject or deactivate. Sensitive: the audit entry is written first and a failure blocks the change."""
    from google.cloud import firestore
    from app.services.audit_service import record_audit_log, record_sensitive_audit

    if decision not in _DECISIONS:
        raise RemarkError(422, "decision must be approve, reject or deactivate.")
    if not _is_reviewer(staff):
        raise RemarkError(403, "Only a super admin can approve, reject or deactivate corrections.")
    client = query._require_client()
    review_note = _text(note, "The note", 0, NOTE_MAX, required=False) if note is not None else None
    current = _load_correction(client, staff, correction_id)
    allowed_from, new_status = _DECISIONS[decision]
    if current.get("status") not in allowed_from:
        raise RemarkError(409, f"A {current.get('status')} correction cannot be {decision}d.")
    record_sensitive_audit(
        db=db, tool_name=f"correction:{decision}", staff_user_id=staff.email,
        proposed_payload={"correction_id": correction_id, "conversation_id": current["conversationId"],
                          "message_id": current["messageId"], "from_status": current.get("status"), "to_status": new_status,
                          "scope": current.get("scope"), "note": review_note},
        confirmed_by_staff=True, status="PENDING",
    )
    ref = client.collection(CORRECTIONS).document(correction_id)
    remark_ref = client.collection(REMARKS).document(current["remarkId"])
    now = _now()
    vector = None
    if new_status == "approved":
        from app.services import correction_retrieval

        vector = correction_retrieval.embed(current.get("question") or "")

    @firestore.transactional
    def write(txn):
        data = ref.get(transaction=txn).to_dict()
        if data.get("status") not in allowed_from:
            raise RemarkError(409, f"A {data.get('status')} correction cannot be {decision}d.")
        remark = remark_ref.get(transaction=txn).to_dict() or {}
        revision = int(data.get("revision") or 1) + 1
        changes: Dict[str, Any] = {"status": new_status, "reviewedBy": _identity(staff), "reviewedAt": now,
                                   "reviewNote": review_note, "revision": revision}
        if new_status == "approved":
            changes["version"] = int(data.get("version") or 0) + 1
            changes["embedding"] = vector
            changes["embeddingModel"] = getattr(settings, "EMBEDDING_MODEL", None) if vector else None
        remark_status = {"approve": "resolved", "reject": "rejected"}.get(decision)
        if remark_status:
            _sync_statuses(txn, client, data["conversationId"], data["messageId"], {current["remarkId"]: remark_status})
            r_rev = int(remark.get("revision") or 1) + 1
            txn.update(remark_ref, {"status": remark_status, "updatedAt": now, "revision": r_rev,
                                    "knowledgeEntryId": correction_id if decision == "approve" else None})
            txn.set(remark_ref.collection(REVISIONS).document(f"r{r_rev:04d}"), {
                "revision": r_rev, "at": now, "by": _identity(staff), "change": f"correction_{decision}d",
                "snapshot": {"status": remark_status, "correctionId": correction_id}})
        txn.update(ref, changes)
        txn.set(ref.collection(REVISIONS).document(f"r{revision:04d}"), {
            "revision": revision, "at": now, "by": _identity(staff), "change": decision, "note": review_note,
            "snapshot": {"status": new_status, "correctedAnswer": data.get("correctedAnswer"), "scope": data.get("scope")}})
        return {**data, **changes}

    def outcome(status: str, error: Optional[str] = None) -> None:
        try:  # the intent was already audited; this records how it ended
            record_audit_log(db=db, tool_name=f"correction:{decision}", staff_user_id=staff.email,
                             proposed_payload={"correction_id": correction_id, "to_status": new_status},
                             confirmed_by_staff=True, status=status, error_message=error)
        except Exception:
            pass

    try:
        result = write(_transaction(client))
    except Exception as exc:
        outcome("FAILED", str(exc)[:300])
        raise
    outcome("SUCCESS")
    from app.services import correction_retrieval

    correction_retrieval.invalidate_cache()
    return _serialize_correction(correction_id, result)


def get_correction(staff: AuthenticatedStaff, correction_id: str) -> Dict[str, Any]:
    client = query._require_client()
    return _serialize_correction(correction_id, _load_correction(client, staff, correction_id))


def list_corrections(staff: AuthenticatedStaff, status: Optional[str] = None, conversation_id: Optional[str] = None,
                     page: int = 1, page_size: int = 25) -> Dict[str, Any]:
    client = query._require_client()
    coll = client.collection(CORRECTIONS)
    if conversation_id:
        _header_for(client, staff, conversation_id)
        docs = [(d.id, d.to_dict()) for d in coll.where("conversationId", "==", conversation_id).limit(LIST_SCAN_CAP).stream()]
    elif status:
        docs = [(d.id, d.to_dict()) for d in coll.where("status", "==", status).limit(LIST_SCAN_CAP).stream()]
    else:
        docs = [(d.id, d.to_dict()) for d in coll.order_by("proposedAt", direction="DESCENDING").limit(LIST_SCAN_CAP).stream()]
    seen: Dict[str, bool] = {}

    def visible(conv_id: str) -> bool:
        if conv_id not in seen:
            snap = client.collection(LOGS).document(conv_id).get()
            seen[conv_id] = bool(snap.exists and query.can_view(snap.to_dict(), staff))
        return seen[conv_id]

    rows = [(i, d) for i, d in docs if visible(d.get("conversationId", ""))]
    if status:
        rows = [(i, d) for i, d in rows if d.get("status") == status]
    rows.sort(key=lambda r: query._aware(r[1].get("proposedAt")), reverse=True)
    start = (page - 1) * page_size
    return {"items": [_serialize_correction(i, d) for i, d in rows[start:start + page_size]], "page": page,
            "page_size": page_size, "total": len(rows)}


def active_corrections(include_embedding: bool = False) -> List[Dict[str, Any]]:
    """Approved corrections only. This is the one place the bot may read corrections from."""
    client = query._client()
    if client is None:
        return []
    now = _now()
    results = []
    for d in client.collection(CORRECTIONS).where("status", "==", "approved").stream():
        data = d.to_dict()
        expires_raw = data.get("expiresAt")
        if expires_raw is not None:
            try:
                from datetime import timezone as _tz
                exp_dt = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=_tz.utc)
                if now > exp_dt:
                    continue  # expired, skip
            except Exception:
                pass  # unparseable expiry: treat as non-expiring
        results.append(_serialize_correction(d.id, data, include_embedding))
    return results


# ---- direct correction (no linked conversation/remark) -----------------------------------------

VALID_SCOPE_TYPES = ("global", "site_specific", "admin_only")
VALID_CATEGORIES = ("property_fact", "agency_policy", "pricing_rule", "contact_info", "clarification")


def _load_correction_direct(client, staff: AuthenticatedStaff, correction_id: str) -> Dict[str, Any]:
    """Load a correction that may not have a linked conversation (direct corrections)."""
    snap = client.collection(CORRECTIONS).document(correction_id).get() if query.valid_key(correction_id) else None
    if snap is None or not snap.exists:
        raise RemarkError(404, "Correction not found.")
    data = snap.to_dict()
    # Only check visibility for conversation-linked corrections
    if data.get("conversationId"):
        _header_for(client, staff, data["conversationId"])
    elif not _is_reviewer(staff):
        raise RemarkError(403, "Only a reviewer can access direct corrections.")
    return data


def create_correction_direct(
    staff: AuthenticatedStaff,
    trigger_query: str,
    corrected_answer: str,
    original_answer: Optional[str] = None,
    scope_type: str = "global",
    category: str = "clarification",
    expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a correction without a linked conversation/remark.

    Status is initially 'proposed'. Callers that want to auto-approve (e.g. super_admin)
    should call decide_correction_direct() immediately after.
    """
    if scope_type not in VALID_SCOPE_TYPES:
        raise RemarkError(422, f"scope_type must be one of {VALID_SCOPE_TYPES}.")
    if category not in VALID_CATEGORIES:
        raise RemarkError(422, f"category must be one of {VALID_CATEGORIES}.")

    question = _text(trigger_query, "trigger_query", ISSUE_MIN, ISSUE_MAX)
    corrected = _text(corrected_answer, "corrected_answer", ANSWER_MIN, ANSWER_MAX)
    original = _text(original_answer, "original_answer", ANSWER_MIN, ANSWER_MAX, required=False)

    # Validate expires_at is a parseable ISO 8601 string if provided
    if expires_at is not None:
        try:
            from datetime import timezone as _tz
            exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=_tz.utc)
            expires_at = exp_dt.isoformat()
        except Exception:
            raise RemarkError(422, "expires_at must be a valid ISO 8601 date/time string.")

    client = query._require_client()
    correction_id = f"COR-{uuid.uuid4().hex[:12].upper()}"
    correction_ref = client.collection(CORRECTIONS).document(correction_id)
    now = _now()

    doc: Dict[str, Any] = {
        "remarkId": None,
        "conversationId": None,
        "messageId": None,
        "question": question,
        "originalAnswer": original or "",
        "correctedAnswer": corrected,
        "scope": {},
        "scopeType": scope_type,
        "category": category,
        "status": "proposed",
        "version": 0,
        "revision": 1,
        "proposedBy": _identity(staff),
        "proposedAt": now,
        "reviewedBy": None,
        "reviewedAt": None,
        "reviewNote": None,
    }
    if expires_at is not None:
        doc["expiresAt"] = expires_at

    correction_ref.set(doc)
    correction_ref.collection(REVISIONS).document("r0001").set({
        "revision": 1, "at": now, "by": _identity(staff), "change": "proposed_direct",
        "snapshot": {
            "correctedAnswer": corrected, "scope": {}, "scopeType": scope_type,
            "category": category, "status": "proposed",
        },
    })
    return _serialize_correction(correction_id, doc)


def decide_correction_direct(
    db,
    staff: AuthenticatedStaff,
    correction_id: str,
    decision: str,
    note: Any = None,
) -> Dict[str, Any]:
    """Approve, reject or deactivate a direct (no-conversation) correction."""
    from google.cloud import firestore
    from app.services.audit_service import record_audit_log, record_sensitive_audit

    if decision not in _DECISIONS:
        raise RemarkError(422, "decision must be approve, reject or deactivate.")
    if not _is_reviewer(staff):
        raise RemarkError(403, "Only a super admin can approve, reject or deactivate corrections.")
    client = query._require_client()
    review_note = _text(note, "The note", 0, NOTE_MAX, required=False) if note is not None else None
    current = _load_correction_direct(client, staff, correction_id)
    allowed_from, new_status = _DECISIONS[decision]
    if current.get("status") not in allowed_from:
        raise RemarkError(409, f"A {current.get('status')} correction cannot be {decision}d.")
    record_sensitive_audit(
        db=db, tool_name=f"correction_direct:{decision}", staff_user_id=staff.email,
        proposed_payload={"correction_id": correction_id, "from_status": current.get("status"),
                          "to_status": new_status, "note": review_note},
        confirmed_by_staff=True, status="PENDING",
    )
    ref = client.collection(CORRECTIONS).document(correction_id)
    now = _now()
    vector = None
    if new_status == "approved":
        from app.services import correction_retrieval
        vector = correction_retrieval.embed(current.get("question") or "")

    @firestore.transactional
    def write(txn):
        data = ref.get(transaction=txn).to_dict()
        if data.get("status") not in allowed_from:
            raise RemarkError(409, f"A {data.get('status')} correction cannot be {decision}d.")
        revision = int(data.get("revision") or 1) + 1
        changes: Dict[str, Any] = {"status": new_status, "reviewedBy": _identity(staff), "reviewedAt": now,
                                   "reviewNote": review_note, "revision": revision}
        if new_status == "approved":
            changes["version"] = int(data.get("version") or 0) + 1
            changes["embedding"] = vector
            changes["embeddingModel"] = getattr(settings, "EMBEDDING_MODEL", None) if vector else None
        txn.update(ref, changes)
        txn.set(ref.collection(REVISIONS).document(f"r{revision:04d}"), {
            "revision": revision, "at": now, "by": _identity(staff), "change": decision, "note": review_note,
            "snapshot": {"status": new_status, "correctedAnswer": data.get("correctedAnswer")}})
        return {**data, **changes}

    def outcome(status: str, error: Optional[str] = None) -> None:
        try:
            record_audit_log(db=db, tool_name=f"correction_direct:{decision}", staff_user_id=staff.email,
                             proposed_payload={"correction_id": correction_id, "to_status": new_status},
                             confirmed_by_staff=True, status=status, error_message=error)
        except Exception:
            pass

    try:
        result = write(_transaction(client))
    except Exception as exc:
        outcome("FAILED", str(exc)[:300])
        raise
    outcome("SUCCESS")
    from app.services import correction_retrieval
    correction_retrieval.invalidate_cache()
    return _serialize_correction(correction_id, result)
