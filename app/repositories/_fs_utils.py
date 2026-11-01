"""Shared Firestore helpers for admin repositories (tolerant of FakeFirestore)."""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def admin_fs_client():
    from app.services.firestore_service import get_firestore_client

    return get_firestore_client(target="admin")


def user_fs_client():
    from app.services.firestore_service import get_firestore_client

    return get_firestore_client(target="user")


def to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    text = str(value).strip()
    return text or None


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def stream_collection(client: Any, name: str) -> List[Any]:
    if client is None:
        return []
    try:
        col = client.collection(name)
        return list(col.stream())
    except Exception as e:
        logger.warning("Firestore stream %s failed: %s", name, e)
        return []


def doc_to_dict(snap: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        raw = snap.to_dict() if hasattr(snap, "to_dict") else None
        if isinstance(raw, dict):
            data = copy.deepcopy(raw)
    except Exception:
        data = {}
    data["id"] = getattr(snap, "id", None) or data.get("id")
    return data


def set_document(client: Any, collection: str, doc_id: str, payload: Dict[str, Any], merge: bool = False) -> None:
    if client is None:
        raise RuntimeError("Firestore client unavailable")
    ref = client.collection(collection).document(str(doc_id))
    ref.set(copy.deepcopy(payload), merge=merge)


def delete_document(client: Any, collection: str, doc_id: str) -> None:
    if client is None:
        return
    try:
        client.collection(collection).document(str(doc_id)).delete()
    except Exception as e:
        logger.warning("Firestore delete %s/%s failed: %s", collection, doc_id, e)


def get_document(client: Any, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    if client is None or not doc_id:
        return None
    try:
        snap = client.collection(collection).document(str(doc_id)).get()
        if not getattr(snap, "exists", False):
            return None
        return doc_to_dict(snap)
    except Exception as e:
        logger.warning("Firestore get %s/%s failed: %s", collection, doc_id, e)
        return None


def run_transaction(client: Any, fn: Callable[[Any], Any]) -> Any:
    """Run fn(txn). FakeFirestore and google clients both expose run_transaction."""
    if client is None:
        raise RuntimeError("Firestore client unavailable")
    if not hasattr(client, "run_transaction"):
        raise RuntimeError("Firestore client has no run_transaction")
    return client.run_transaction(fn)


def filter_in_memory(
    docs: Iterable[Dict[str, Any]],
    *,
    predicates: Optional[List[Callable[[Dict[str, Any]], bool]]] = None,
) -> List[Dict[str, Any]]:
    out = list(docs)
    for pred in predicates or []:
        out = [d for d in out if pred(d)]
    return out
