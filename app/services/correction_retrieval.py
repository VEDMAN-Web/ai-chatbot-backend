"""
Finding the approved correction that applies to a question (so the same question, even reworded, gets the
corrected answer).

Only corrections a super admin approved are considered (chat_log_remarks.active_corrections). A question matches
when all of these hold:
  * its website, language and property fit the correction's scope;
  * it is semantically close to the question the correction was written for (provider embeddings, cosine
    similarity; a word-overlap measure is used only if a correction has no stored embedding);
  * every proper name and number of the corrected question also appears in the new question, so a correction
    about one property is never applied to a question about another.
The best match wins; when two approved corrections both match, the one approved most recently wins.
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 60            # other server instances pick up an approval or deactivation within this time
MAX_EMBED_CHARS = 2000
_cache: Dict[str, Any] = {"at": 0.0, "items": []}
_WORD = re.compile(r"[\w'-]+", re.UNICODE)


def invalidate_cache() -> None:
    _cache["at"] = 0.0


def embed(text: str) -> Optional[List[float]]:
    """Embedding vector from the configured provider, or None when it is not available."""
    cleaned = (text or "").strip()
    api_key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not cleaned or not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=8.0, max_retries=1)
        response = client.embeddings.create(model=getattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small"),
                                            input=cleaned[:MAX_EMBED_CHARS])
        return list(response.data[0].embedding)
    except Exception as exc:
        logger.warning("Embedding unavailable: %s", exc)
        return None


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _words(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text or "") if len(w) > 2]


def word_overlap(a: str, b: str) -> float:
    left, right = set(_words(a)), set(_words(b))
    return len(left & right) / max(len(left | right), 1)


def entities_of(text: str) -> List[str]:
    """Proper names and numbers in a question (capitalised words after the first, and digits)."""
    tokens = _WORD.findall(text or "")
    found = [t.lower() for i, t in enumerate(tokens) if i > 0 and t[:1].isupper() and len(t) > 1]
    found += [t for t in tokens if any(ch.isdigit() for ch in t)]
    return found


def entities_present(corrected_question: str, new_question: str) -> bool:
    haystack = (new_question or "").lower()
    return all(e.lower() in haystack for e in entities_of(corrected_question))


def _approved() -> List[Dict[str, Any]]:
    now = time.time()
    if now - _cache["at"] < CACHE_TTL_SEC:
        return _cache["items"]
    from app.services import chat_log_remarks

    try:
        _cache["items"] = chat_log_remarks.active_corrections(include_embedding=True)
    except Exception as exc:
        logger.warning("Could not load approved corrections: %s", exc)
        _cache["items"] = []
    _cache["at"] = now
    return _cache["items"]


def _in_scope(correction: Dict[str, Any], website: Optional[str], language: Optional[str], property_ids: List[str]) -> bool:
    scope = correction.get("scope") or {}
    sites = scope.get("websites") or []
    if sites and website not in sites:
        return False
    langs = scope.get("languages") or []
    if langs and (language or "").lower() not in langs:
        return False
    pids = scope.get("propertyIds") or []
    if pids and not set(pids) & set(property_ids or []):
        return False
    return True


def find_applicable(question: str, website: Optional[str], language: Optional[str] = None,
                    property_ids: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """The approved correction for this question, or None. Nothing is returned for an unapproved correction."""
    if not getattr(settings, "ENABLE_ANSWER_CORRECTIONS", True) or not (question or "").strip():
        return None
    candidates = [c for c in _approved() if _in_scope(c, website, language, property_ids or [])]
    if not candidates:
        return None
    query_vector = embed(question) if any(c.get("embedding") for c in candidates) else None
    threshold = float(getattr(settings, "CORRECTION_MATCH_THRESHOLD", 0.88))
    best: List[tuple] = []
    for c in candidates:
        if not entities_present(c.get("question") or "", question):
            continue
        if c.get("embedding") and query_vector:
            score, needed = cosine(query_vector, c["embedding"]), threshold
        else:
            score, needed = word_overlap(question, c.get("question") or ""), 0.8
        if score >= needed:
            best.append((score, c.get("reviewed_at") or "", c))
    if not best:
        return None
    best.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(best) > 1 and best[0][2]["corrected_answer"] != best[1][2]["corrected_answer"]:
        logger.warning("Conflicting approved corrections matched one question: %s, %s",
                       best[0][2]["correction_id"], best[1][2]["correction_id"])
    chosen = dict(best[0][2])
    chosen.pop("embedding", None)
    chosen["match_score"] = round(best[0][0], 4)
    return chosen
