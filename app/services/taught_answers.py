"""
Taught answers (Module 2): fixed FAQ answers an admin teaches the public bot.

Stored in Firestore collection `taught_answers` (same Firestore target as superadmin instructions).
Only near-exact FAQ matches are served, before any model call. Every answer is vetted by grounding_policy
when it is saved and again when it is served, so a hand-typed answer can never carry prices, yields or
guarantees. Listing facts always come from live data, never from here.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.repositories import _fs_utils as fs
from app.services import grounding_policy

logger = logging.getLogger(__name__)

COLLECTION = "taught_answers"
STATUSES = ("active", "archived")
QUESTION_MAX = 300
ANSWER_MAX = 1200
VARIANT_MAX = 10
MATCH_THRESHOLD = 0.8
CACHE_TTL_SEC = 60

_STOP = frozenset(
    "a an the is are do does did can could would you your i we me my our to of for in on at and or with "
    "please tell about what whats how it this that there any have has be".split()
)
_cache: Dict[str, Any] = {"at": 0.0, "rows": None}


class TaughtAnswerError(ValueError):
    pass


def _client():
    target = (getattr(settings, "INSTRUCTIONS_FIRESTORE_TARGET", "user") or "user").lower()
    return fs.admin_fs_client() if target == "admin" else fs.user_fs_client()


def _require_client():
    client = _client()
    if client is None:
        raise RuntimeError("Firestore client unavailable for taught answers.")
    return client


def invalidate_cache() -> None:
    _cache["at"] = 0.0
    _cache["rows"] = None


def normalize(text: str) -> str:
    value = re.sub(r"[^\w\s]", " ", (text or "").casefold(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(text: str) -> frozenset:
    return frozenset(t for t in normalize(text).split() if t not in _STOP)


def _site(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://", "", v).split("/")[0]
    return v[4:] if v.startswith("www.") else v


def _validate(question: str, answer: str, variants: List[str]) -> None:
    if not normalize(question):
        raise TaughtAnswerError("question is required")
    if len(question) > QUESTION_MAX:
        raise TaughtAnswerError(f"question is longer than {QUESTION_MAX} characters")
    if not (answer or "").strip():
        raise TaughtAnswerError("answer is required")
    if len(answer) > ANSWER_MAX:
        raise TaughtAnswerError(f"answer is longer than {ANSWER_MAX} characters")
    if len(variants) > VARIANT_MAX:
        raise TaughtAnswerError(f"at most {VARIANT_MAX} question variants")
    if any(len(v) > QUESTION_MAX for v in variants):
        raise TaughtAnswerError(f"a variant is longer than {QUESTION_MAX} characters")
    problems = grounding_policy.vet_taught_answer(answer)
    if problems:
        raise TaughtAnswerError("answer rejected: " + "; ".join(problems))


def _clean_list(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for v in values or []:
        v = str(v).strip()
        if v and v not in out:
            out.append(v)
    return out


def create(*, question: str, answer: str, variants: Optional[List[str]] = None,
           sites: Optional[List[str]] = None, language: str = "any", created_by: str = "") -> Dict[str, Any]:
    variants = _clean_list(variants)
    question, answer = (question or "").strip(), (answer or "").strip()
    _validate(question, answer, variants)
    client = _require_client()
    now = datetime.now(timezone.utc).isoformat()
    doc_id = f"ta_{uuid.uuid4().hex[:10]}"
    payload = {
        "question": question,
        "variants": variants,
        "answer": answer,
        "sites": sorted({_site(s) for s in (sites or []) if _site(s)}),
        "language": (language or "any").lower()[:2] if (language or "any").lower() != "any" else "any",
        "status": "active",
        "version": 1,
        "hits": 0,
        "createdBy": created_by,
        "createdAt": now,
        "updatedAt": now,
    }
    fs.set_document(client, COLLECTION, doc_id, payload, merge=False)
    invalidate_cache()
    return {"id": doc_id, **payload}


def get(doc_id: str) -> Optional[Dict[str, Any]]:
    data = fs.get_document(_require_client(), COLLECTION, doc_id)
    return None if data is None else {"id": doc_id, **data}


def update(doc_id: str, *, question: Optional[str] = None, answer: Optional[str] = None,
           variants: Optional[List[str]] = None, sites: Optional[List[str]] = None,
           language: Optional[str] = None, status: Optional[str] = None, updated_by: str = "") -> Dict[str, Any]:
    current = get(doc_id)
    if current is None:
        raise KeyError(doc_id)
    if status is not None and status not in STATUSES:
        raise TaughtAnswerError("status must be active or archived")
    new_q = (question if question is not None else current["question"]).strip()
    new_a = (answer if answer is not None else current["answer"]).strip()
    new_v = _clean_list(variants) if variants is not None else list(current.get("variants") or [])
    _validate(new_q, new_a, new_v)
    patch = {
        "question": new_q, "answer": new_a, "variants": new_v,
        "version": int(current.get("version") or 1) + 1,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "updatedBy": updated_by,
    }
    if sites is not None:
        patch["sites"] = sorted({_site(s) for s in sites if _site(s)})
    if language is not None:
        patch["language"] = language.lower()[:2] if language.lower() != "any" else "any"
    if status is not None:
        patch["status"] = status
    fs.set_document(_require_client(), COLLECTION, doc_id, patch, merge=True)
    invalidate_cache()
    return {**current, **patch}


def archive(doc_id: str, *, updated_by: str = "") -> Dict[str, Any]:
    return update(doc_id, status="archived", updated_by=updated_by)


def list_answers(*, status: Optional[str] = None, site: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = []
    for snap in fs.stream_collection(_require_client(), COLLECTION):
        data = fs.doc_to_dict(snap)
        data["id"] = data.get("id") or getattr(snap, "id", None)
        rows.append(data)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if site:
        s = _site(site)
        rows = [r for r in rows if not r.get("sites") or s in r["sites"]]
    return sorted(rows, key=lambda r: r.get("createdAt") or "")


def _active_rows() -> List[Dict[str, Any]]:
    now = time.time()
    if _cache["rows"] is not None and now - _cache["at"] < CACHE_TTL_SEC:
        return _cache["rows"]
    rows = list_answers(status="active")
    _cache["rows"], _cache["at"] = rows, now
    return rows


def _score(message: str, row: Dict[str, Any]) -> float:
    msg_norm, msg_tok = normalize(message), _tokens(message)
    best = 0.0
    for q in [row.get("question") or ""] + list(row.get("variants") or []):
        if normalize(q) == msg_norm:
            return 1.0
        q_tok = _tokens(q)
        if len(q_tok) < 2 or len(msg_tok) < 2:
            continue
        union = q_tok | msg_tok
        best = max(best, len(q_tok & msg_tok) / len(union))
    return best


def find_match(message: str, *, site_origin: Optional[str] = None, language: str = "en") -> Optional[Dict[str, Any]]:
    """Best active taught answer for this message, or None. Never raises."""
    try:
        if not normalize(message) or len(message) > QUESTION_MAX * 2:
            return None
        site = _site(site_origin)
        lang = (language or "en").lower()[:2]
        best, best_score = None, 0.0
        for row in _active_rows():
            if row.get("sites") and site not in row["sites"]:
                continue
            if row.get("language") not in (None, "", "any", lang):
                continue
            score = _score(message, row)
            if score >= MATCH_THRESHOLD and score > best_score:
                best, best_score = row, score
        if best is None:
            return None
        if grounding_policy.vet_taught_answer(best.get("answer") or ""):
            logger.warning("Taught answer %s failed grounding policy at serve time; skipped.", best.get("id"))
            return None
        _record_hit(best)
        return {"id": best.get("id"), "answer": best["answer"], "score": round(best_score, 3)}
    except Exception as exc:
        logger.warning("Taught answer lookup failed: %s", exc)
        return None


def _record_hit(row: Dict[str, Any]) -> None:
    try:
        fs.set_document(_require_client(), COLLECTION, row["id"], {"hits": int(row.get("hits") or 0) + 1}, merge=True)
        row["hits"] = int(row.get("hits") or 0) + 1
    except Exception:
        pass
