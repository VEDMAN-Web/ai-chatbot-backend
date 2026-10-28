"""Shared in-memory FakeFirestore for admin Stage F1 tests."""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional, Tuple

import pytest


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._data = None if data is None else copy.deepcopy(data)

    def to_dict(self):
        return None if self._data is None else copy.deepcopy(self._data)


class _Ref:
    def __init__(self, store, path):
        self.store = store
        self.path = tuple(path)
        self.id = self.path[-1]

    def get(self, transaction=None):
        with self.store.lock:
            return _Snap(self.id, self.store.docs.get(self.path))

    def set(self, data, merge=False):
        with self.store.lock:
            self.store.write(self.path, data, merge)

    def update(self, data):
        with self.store.lock:
            current = self.store.docs.get(self.path)
            if isinstance(current, dict):
                merged = copy.deepcopy(current)
                merged.update(copy.deepcopy(data))
                self.store.write(self.path, merged, merge=False)
            else:
                self.store.write(self.path, data, merge=False)

    def delete(self):
        with self.store.lock:
            self.store.docs.pop(self.path, None)

    def collection(self, name):
        return _Collection(self.store, self.path + (name,))


class _Query:
    def __init__(self, collection, filters=None, limit_n=None):
        self.collection = collection
        self.filters = list(filters or [])
        self.limit_n = limit_n

    def where(self, field, op=None, value=None, **kwargs):
        if op is None and value is None and "filter" in kwargs:
            f = kwargs["filter"]
            field, op, value = (
                getattr(f, "field_path", None),
                getattr(f, "op_string", "=="),
                getattr(f, "value", None),
            )
        return _Query(self.collection, self.filters + [(field, op, value)], self.limit_n)

    def limit(self, n):
        return _Query(self.collection, self.filters, n)

    def stream(self):
        snaps = list(self.collection.stream())
        out = []
        for snap in snaps:
            data = snap.to_dict() or {}
            ok = True
            for field, op, value in self.filters:
                actual = data.get(field)
                if op in ("==", None):
                    if actual != value:
                        ok = False
                        break
                else:
                    ok = False
                    break
            if ok:
                out.append(snap)
        if self.limit_n is not None:
            out = out[: int(self.limit_n)]
        return out


class _Collection:
    def __init__(self, store, path):
        self.store = store
        self.path = tuple(path)

    def document(self, doc_id):
        return _Ref(self.store, self.path + (str(doc_id),))

    def where(self, field, op=None, value=None, **kwargs):
        return _Query(self).where(field, op, value, **kwargs)

    def limit(self, n):
        return _Query(self).limit(n)

    def stream(self):
        with self.store.lock:
            snaps = []
            for path, data in self.store.docs.items():
                if len(path) == len(self.path) + 1 and path[: len(self.path)] == self.path:
                    snaps.append(_Snap(path[-1], data))
        return snaps


class _Txn:
    def __init__(self, store):
        self.store = store
        self.overlay = {}

    def get(self, ref):
        if ref.path in self.overlay:
            return _Snap(ref.id, self.overlay[ref.path])
        return ref.get()

    def set(self, ref, data, merge=False):
        current = self.overlay.get(ref.path)
        if current is None and merge:
            current = self.store.docs.get(ref.path)
        if merge and isinstance(current, dict):
            merged = copy.deepcopy(current)
            merged.update(copy.deepcopy(data))
            self.overlay[ref.path] = merged
        else:
            self.overlay[ref.path] = copy.deepcopy(data)


class FakeFirestore:
    def __init__(self):
        self.docs: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        self.lock = threading.RLock()

    def collection(self, name):
        return _Collection(self, (name,))

    def write(self, path, data, merge):
        if merge and isinstance(self.docs.get(path), dict):
            merged = copy.deepcopy(self.docs[path])
            merged.update(copy.deepcopy(data))
            self.docs[path] = merged
        else:
            self.docs[path] = copy.deepcopy(data)

    def run_transaction(self, fn):
        with self.lock:
            txn = _Txn(self)
            result = fn(txn)
            for path, data in txn.overlay.items():
                self.docs[path] = data
            return result


def patch_firestore(monkeypatch, fake: Optional[FakeFirestore] = None) -> FakeFirestore:
    """Patch Firestore client getters to use the shared fake."""
    fs = fake or FakeFirestore()
    monkeypatch.setattr(
        "app.services.firestore_service.get_admin_firestore_client",
        lambda: fs,
    )
    monkeypatch.setattr(
        "app.services.firestore_service.get_user_firestore_client",
        lambda: fs,
    )
    monkeypatch.setattr(
        "app.services.firestore_service.get_firestore_client",
        lambda target="user", *a, **k: fs,
    )
    return fs


def set_store_modes(
    monkeypatch,
    *,
    pending: str = "firestore",
    audit: str = "firestore",
    leads: str = "firestore",
) -> None:
    monkeypatch.setattr("app.core.config.settings.STORE_PENDING_ACTIONS", pending)
    monkeypatch.setattr("app.core.config.settings.STORE_AUDIT", audit)
    monkeypatch.setattr("app.core.config.settings.STORE_LEADS_READ", leads)
    monkeypatch.setattr("app.core.config.settings.ENABLE_AUDIT_LOGGING", True)


def docs_in_collection(fake: FakeFirestore, collection: str) -> List[Dict[str, Any]]:
    return [
        copy.deepcopy(data)
        for path, data in fake.docs.items()
        if len(path) == 2 and path[0] == collection
    ]


def pending_docs(fake: FakeFirestore) -> List[Dict[str, Any]]:
    return docs_in_collection(fake, "pending_actions")


def audit_docs(fake: FakeFirestore) -> List[Dict[str, Any]]:
    return docs_in_collection(fake, "audit_logs")


@pytest.fixture
def fake_fs(monkeypatch):
    return patch_firestore(monkeypatch)


@pytest.fixture
def firestore_store_modes(monkeypatch):
    set_store_modes(monkeypatch)
