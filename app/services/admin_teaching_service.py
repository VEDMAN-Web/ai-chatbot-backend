"""
Admin Teaching & Persistent Rule Ingestion (Requirement 7).

Functions:
  is_teaching_intent(text: str) -> bool
    - Detects if admin message is a teaching command
    - Triggers on: "remember:", "learn that", "rule:", "note:", "teach me", "from now on",
      "always respond", "pamiętaj:", "zapamiętaj", "zarejestruj", "ustaw regułę"

  extract_teaching_content(text: str) -> dict
    - Parses the teaching message into {subject, rule_text, raw_text}
    - subject: first noun phrase after trigger keyword, max 80 chars
    - rule_text: everything after the trigger phrase, max 2000 chars
    - Falls back gracefully

  stage_knowledge_entry(db, rule_text: str, subject: str, staff_user, conversation_id: str) -> dict
    - Creates a knowledge entry in Firestore collection "knowledge_entries"
    - Status: "proposed"
    - Returns: {entry_id, subject, rule_text, status, preview_message}
    - preview_message is the bot's echo-back for admin to confirm:
      "Understood Admin. I have recorded the following rule:\n- **Subject:** {subject}\n- **Rule:** {rule_text}\n- **Scope:** Global\nShould I activate this rule across all future answers?"

  activate_knowledge_entry(entry_id: str, staff_user) -> dict
    - Changes status to "active" in Firestore
    - Computes embedding via correction_retrieval.embed(rule_text) and stores it
    - Writes AuditLog record via app.services.audit_service.record_audit_log
    - Returns: {entry_id, status: "active", activated_at}

  search_knowledge_entries(question: str, website: str = None) -> Optional[dict]
    - Cosine similarity search against active knowledge entries
    - Uses correction_retrieval.embed + correction_retrieval.cosine
    - Threshold: 0.82 (slightly lower than corrections since rules are broader)
    - Returns the best matching entry or None
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

KNOWLEDGE_ENTRIES_COLLECTION = "knowledge_entries"
KNOWLEDGE_SIMILARITY_THRESHOLD = 0.82
MAX_SUBJECT_CHARS = 80
MAX_RULE_TEXT_CHARS = 2000

# ---------------------------------------------------------------------------
# Teaching trigger patterns
# ---------------------------------------------------------------------------

_TEACHING_TRIGGER_PATTERNS: List[re.Pattern] = [
    re.compile(r"remember\s*:", re.IGNORECASE),
    re.compile(r"learn\s+that\b", re.IGNORECASE),
    re.compile(r"rule\s*:", re.IGNORECASE),
    re.compile(r"note\s*:", re.IGNORECASE),
    re.compile(r"teach\s+me\b", re.IGNORECASE),
    re.compile(r"teach\b", re.IGNORECASE),
    re.compile(r"from\s+now\s+on\b", re.IGNORECASE),
    re.compile(r"always\s+respond\b", re.IGNORECASE),
    re.compile(r"pamiętaj\s*:", re.IGNORECASE),
    re.compile(r"zapamiętaj\b", re.IGNORECASE),
    re.compile(r"zarejestruj\b", re.IGNORECASE),
    re.compile(r"ustaw\s+regułę\b", re.IGNORECASE),
]

# Ordered list of (trigger_re, split_re) to extract content after the trigger phrase
_SPLIT_PATTERNS: List[re.Pattern] = [
    re.compile(r"(?i)remember\s*:\s*"),
    re.compile(r"(?i)learn\s+that\s+"),
    re.compile(r"(?i)rule\s*:\s*"),
    re.compile(r"(?i)note\s*:\s*"),
    re.compile(r"(?i)teach\s+me\s+"),
    re.compile(r"(?i)teach\s+"),
    re.compile(r"(?i)from\s+now\s+on[,\s]+"),
    re.compile(r"(?i)always\s+respond\s+"),
    re.compile(r"(?i)pamiętaj\s*:\s*"),
    re.compile(r"(?i)zapamiętaj\s*"),
    re.compile(r"(?i)zarejestruj\s*"),
    re.compile(r"(?i)ustaw\s+regułę\s*:\s*"),
]

_NOUN_PHRASE_RE = re.compile(r"^((?:[A-Za-z\u00C0-\u024F\u0E00-\u0E7F]+\s*){1,6})", re.UNICODE)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _firestore_client():
    try:
        from app.services.firestore_service import get_firestore_client
        return get_firestore_client(target="admin")
    except Exception as exc:
        logger.warning("Firestore client unavailable: %s", exc)
        return None


def _staff_user_id(staff_user: Any) -> Optional[str]:
    if staff_user is None:
        return None
    if isinstance(staff_user, dict):
        return str(staff_user.get("user_id") or staff_user.get("id") or "")
    return str(getattr(staff_user, "user_id", None) or getattr(staff_user, "id", None) or "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_teaching_intent(text: str) -> bool:
    """
    Return True if the text contains a teaching/rule-ingestion command.
    """
    if not text or not text.strip():
        return False
    for pattern in _TEACHING_TRIGGER_PATTERNS:
        if pattern.search(text):
            return True
    return False


def extract_teaching_content(text: str) -> Dict[str, str]:
    """
    Parse a teaching message into {subject, rule_text, raw_text}.

    subject  : first short noun phrase after the trigger keyword (max 80 chars)
    rule_text: everything after the trigger phrase (max 2000 chars)
    raw_text : the original input (truncated to 2000 chars)
    """
    raw_text = (text or "").strip()[:MAX_RULE_TEXT_CHARS]

    rule_text = raw_text
    for split_re in _SPLIT_PATTERNS:
        m = split_re.search(raw_text)
        if m:
            rule_text = raw_text[m.end():].strip()
            break

    rule_text = rule_text[:MAX_RULE_TEXT_CHARS]

    # Extract a short subject: first noun phrase of rule_text (up to 6 words)
    subject = ""
    if rule_text:
        nm = _NOUN_PHRASE_RE.match(rule_text)
        if nm:
            subject = nm.group(1).strip()
    if not subject:
        subject = rule_text[:MAX_SUBJECT_CHARS].split(".")[0].strip()
    subject = subject[:MAX_SUBJECT_CHARS]

    return {
        "subject": subject,
        "rule_text": rule_text,
        "raw_text": raw_text,
    }


def stage_knowledge_entry(
    db: Any,
    rule_text: str,
    subject: str,
    staff_user: Any,
    conversation_id: str,
) -> Dict[str, Any]:
    """
    Create a knowledge entry in Firestore with status "proposed".

    Returns dict with keys: entry_id, subject, rule_text, status, preview_message.
    Never raises — returns a stub dict on failure.
    """
    entry_id = str(uuid.uuid4())
    subject = (subject or "").strip()[:MAX_SUBJECT_CHARS]
    rule_text = (rule_text or "").strip()[:MAX_RULE_TEXT_CHARS]

    preview_message = (
        f"Understood Admin. I have recorded the following rule:\n"
        f"- **Subject:** {subject}\n"
        f"- **Rule:** {rule_text}\n"
        f"- **Scope:** Global\n"
        f"Should I activate this rule across all future answers?"
    )

    stub_result: Dict[str, Any] = {
        "entry_id": entry_id,
        "subject": subject,
        "rule_text": rule_text,
        "status": "proposed",
        "preview_message": preview_message,
    }

    try:
        client = _firestore_client()
        if client is None:
            logger.warning("No Firestore client — knowledge entry staged in memory only.")
            return stub_result

        user_id = _staff_user_id(staff_user)
        now = _now_utc()

        doc = {
            "entry_id": entry_id,
            "subject": subject,
            "rule_text": rule_text,
            "status": "proposed",
            "conversation_id": conversation_id or "",
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
            "embedding": None,
            "website": None,
        }

        client.collection(KNOWLEDGE_ENTRIES_COLLECTION).document(entry_id).set(doc)
        logger.info("Knowledge entry staged: %s (%s)", entry_id, subject)

    except Exception as exc:
        logger.warning("Failed to write knowledge entry to Firestore (non-fatal): %s", exc)

    return stub_result


def activate_knowledge_entry(entry_id: str, staff_user: Any) -> Dict[str, Any]:
    """
    Set a knowledge entry status to "active", compute its embedding, and write an audit log.

    Returns: {entry_id, status: "active", activated_at}
    """
    now = _now_utc()
    result: Dict[str, Any] = {
        "entry_id": entry_id,
        "status": "active",
        "activated_at": now.isoformat(),
    }

    try:
        client = _firestore_client()
        if client is None:
            logger.warning("No Firestore client — cannot activate knowledge entry %s.", entry_id)
            return result

        doc_ref = client.collection(KNOWLEDGE_ENTRIES_COLLECTION).document(entry_id)
        snapshot = doc_ref.get()
        if not snapshot.exists:
            logger.warning("Knowledge entry %s not found in Firestore.", entry_id)
            return result

        entry_data = snapshot.to_dict() or {}
        rule_text = entry_data.get("rule_text", "")

        # Compute embedding
        embedding = None
        try:
            from app.services.correction_retrieval import embed
            embedding = embed(rule_text)
        except Exception as exc:
            logger.warning("Embedding computation failed for %s: %s", entry_id, exc)

        update_payload: Dict[str, Any] = {
            "status": "active",
            "activated_at": now,
            "activated_by": _staff_user_id(staff_user),
            "updated_at": now,
        }
        if embedding is not None:
            update_payload["embedding"] = embedding

        doc_ref.update(update_payload)

        # Write audit log
        try:
            from app.services.audit_service import record_audit_log
            record_audit_log(
                db=None,
                tool_name="activate_knowledge_entry",
                staff_user_id=_staff_user_id(staff_user),
                tool_arguments={"entry_id": entry_id},
                proposed_payload={"status": "active", "subject": entry_data.get("subject", "")},
                confirmed_by_staff=True,
                execution_result=f"Knowledge entry {entry_id} activated.",
                status="SUCCESS",
            )
        except Exception as exc:
            logger.warning("Audit log write failed for knowledge activation %s: %s", entry_id, exc)

    except Exception as exc:
        logger.warning("Failed to activate knowledge entry %s (non-fatal): %s", entry_id, exc)

    return result


def search_knowledge_entries(
    question: str,
    website: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Cosine-similarity search across active knowledge entries.

    Uses correction_retrieval.embed and correction_retrieval.cosine.
    Threshold: 0.82 — slightly lower than answer corrections since rules are broader.

    Returns the best matching entry dict or None.
    """
    if not question or not question.strip():
        return None

    try:
        client = _firestore_client()
        if client is None:
            return None

        # Fetch active entries
        query = client.collection(KNOWLEDGE_ENTRIES_COLLECTION).where("status", "==", "active")
        if website:
            # Allow entries scoped to this website OR global (website == None)
            pass  # We filter in Python below since Firestore OR queries require composite indexes
        docs = list(query.stream())
        if not docs:
            return None

        entries: List[Dict[str, Any]] = []
        for doc in docs:
            d = doc.to_dict() or {}
            entry_website = d.get("website")
            # Include if global (no website scope) or matching website
            if entry_website is None or entry_website == "" or entry_website == website:
                entries.append(d)

        if not entries:
            return None

        from app.services.correction_retrieval import embed, cosine

        query_vec = embed(question)
        if query_vec is None:
            # Fallback: no embeddings available — return None
            return None

        best_score = 0.0
        best_entry: Optional[Dict[str, Any]] = None

        for entry in entries:
            stored_embedding = entry.get("embedding")
            if not stored_embedding:
                continue
            score = cosine(query_vec, stored_embedding)
            if score >= KNOWLEDGE_SIMILARITY_THRESHOLD and score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None:
            result = dict(best_entry)
            result.pop("embedding", None)
            result["match_score"] = round(best_score, 4)
            return result

    except Exception as exc:
        logger.warning("Knowledge entry search failed (non-fatal): %s", exc)

    return None
