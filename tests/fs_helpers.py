"""Read helpers for tests: query the in-memory Firestore fake where old tests used SQL queries.

`fs` is a fake exposing `.items(collection) -> [(doc_id, data)]` and `.get(collection, doc_id)`.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

from app.services.firestore_service import normalize_firestore_project
from app.services.property_search import FirestorePropertyAdapter


def raw_project(fs, property_id: str) -> Optional[dict]:
    return fs.get("projects", property_id)


def _row(doc_id: str, data: dict):
    adapter = FirestorePropertyAdapter(normalize_firestore_project(doc_id, data))
    adapter.raw_metadata = (data or {}).get("raw_metadata") or adapter.raw_metadata or {}
    adapter.raw = data
    return adapter


def prop_rows(fs) -> List[Any]:
    return [_row(doc_id, data) for doc_id, data in fs.items("projects")]


def prop_first(fs, property_id: Optional[str] = None, name: Optional[str] = None):
    for row in prop_rows(fs):
        if property_id is not None and row.property_id == property_id:
            return row
        if name is not None and row.property_name == name:
            return row
    return None


def prop_one(fs, property_id: Optional[str] = None, name: Optional[str] = None):
    row = prop_first(fs, property_id=property_id, name=name)
    assert row is not None, f"no property {property_id or name}"
    return row


def _plain(doc_id: str, data: dict):
    fields = {k: v for k, v in (data or {}).items() if k.isidentifier() and k != "id"}
    return SimpleNamespace(id=doc_id, **fields)


def pending_rows(fs, status: Optional[str] = None, tool: Optional[str] = None) -> List[Any]:
    out = []
    for doc_id, data in fs.items("pending_actions"):
        if status is not None and (data or {}).get("status") != status:
            continue
        if tool is not None and (data or {}).get("tool_name") != tool:
            continue
        out.append(_plain(doc_id, data))
    return out


def pending_one(fs, status: str):
    rows = pending_rows(fs, status)
    assert len(rows) == 1, f"expected one {status} pending action, got {len(rows)}"
    return rows[0]


def audit_rows(fs, tool: Optional[str] = None) -> List[Any]:
    out = []
    for doc_id, data in fs.items("audit_logs"):
        if tool is not None and (data or {}).get("tool_name") != tool:
            continue
        out.append(_plain(doc_id, data))
    return out
