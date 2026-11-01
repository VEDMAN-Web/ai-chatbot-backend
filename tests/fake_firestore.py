"""Shared in-memory FakeFirestore for admin Stage F1 tests."""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

try:
    from google.cloud.firestore_v1 import DELETE_FIELD as _DELETE_FIELD
except Exception:  # pragma: no cover
    _DELETE_FIELD = object()


def _apply_deletes(merged):
    """Real Firestore removes a field set to DELETE_FIELD; mimic that."""
    for key in [k for k, v in merged.items() if v is _DELETE_FIELD]:
        merged.pop(key, None)
    return merged


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
            stats = getattr(self.store, "stats", None)
            if stats is not None:
                stats["doc_gets"] = stats.get("doc_gets", 0) + 1
            return _Snap(self.id, self.store.docs.get(self.path))

    def set(self, data, merge=False):
        with self.store.lock:
            self.store.write(self.path, data, merge)

    def update(self, data):
        with self.store.lock:
            current = self.store.docs.get(self.path)
            if isinstance(current, dict):
                merged = copy.deepcopy(current)
                merged.update(dict(data))
                self.store.write(self.path, _apply_deletes(merged), merge=False)
            else:
                self.store.write(self.path, data, merge=False)

    def delete(self):
        with self.store.lock:
            self.store.docs.pop(self.path, None)

    def collection(self, name):
        return _Collection(self.store, self.path + (name,))


_TYPE_ORDER = ((bool, 1), (int, 2), (float, 2), (datetime, 3), (str, 4), (bytes, 5), (list, 8), (dict, 9))


def _rank(value):
    """Firestore's cross-type ordering: null < bool < number < timestamp < string < bytes < ... < array < map."""
    if value is None:
        return 0
    for typ, rank in _TYPE_ORDER:
        if isinstance(value, typ):
            return rank
    return 7


def _norm(value):
    """Naive datetimes are treated as UTC, like Firestore timestamps."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _order_key(value):
    rank = _rank(value)
    return (rank, _norm(value)) if rank in (1, 2, 3, 4, 5) else (rank, 0)


def _field(data, path):
    """Dotted paths read nested maps. Returns (present, value)."""
    cur = data
    for part in str(path).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _matches(data, field, op, value):
    present, actual = _field(data, field)
    op = "==" if op is None else str(op).replace("-", "_").lower()
    if op == "==":
        return present and actual == value
    if op == "in":
        return present and actual in list(value or [])
    if op == "array_contains":
        return present and isinstance(actual, list) and value in actual
    if op == "array_contains_any":
        return present and isinstance(actual, list) and any(v in actual for v in (value or []))
    if op == "!=":  # Firestore excludes documents without the field, and nulls
        return present and actual is not None and actual != value
    if op == "not_in":
        return present and actual is not None and actual not in list(value or [])
    if op in ("<", "<=", ">", ">="):
        # Range filters only match values of the operand's type.
        if not present or actual is None or _rank(actual) != _rank(value):
            return False
        a, b = _norm(actual), _norm(value)
        try:
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        except TypeError:
            return False
    return False


class _AggResult:
    def __init__(self, alias, value):
        self.alias = alias
        self.value = value


class _CountQuery:
    """query.count(alias=...).get() -> [[AggregationResult]], as in google-cloud-firestore."""

    def __init__(self, query, alias):
        self.query = query
        self.alias = alias or "count"

    def get(self, *a, **k):
        n = len(self.query._run())
        store = self.query.collection.store
        with store.lock:
            store.stats["aggregations"] += 1
        return [[_AggResult(self.alias, n)]]

    def stream(self, *a, **k):
        return iter(self.get())


class _Query:
    """Subset of a Firestore Query: where / order_by / limit / offset / count.

    Like real Firestore, a document missing an order_by field is not returned, and range filters only match values
    of the operand's type. Every document a query returns is counted as one read in store.stats.
    """

    ASCENDING = "ASCENDING"
    DESCENDING = "DESCENDING"

    def __init__(self, collection, filters=None, limit_n=None, orders=None, offset_n=None, cursor=None):
        self.collection = collection
        self.filters = list(filters or [])
        self.limit_n = limit_n
        self.orders = list(orders or [])
        self.offset_n = offset_n
        self.cursor = cursor  # ("after"|"at", snapshot or field dict)

    def _copy(self, **changes):
        params = dict(filters=self.filters, limit_n=self.limit_n, orders=self.orders, offset_n=self.offset_n,
                      cursor=self.cursor)
        params.update(changes)
        return _Query(self.collection, **params)

    def where(self, field=None, op=None, value=None, **kwargs):
        if "filter" in kwargs:
            f = kwargs["filter"]
            field, op, value = (
                getattr(f, "field_path", None),
                getattr(f, "op_string", "=="),
                getattr(f, "value", None),
            )
        return self._copy(filters=self.filters + [(field, op, value)])

    def order_by(self, field, direction="ASCENDING"):
        return self._copy(orders=self.orders + [(field, str(direction).upper())])

    def limit(self, n):
        return self._copy(limit_n=n)

    def offset(self, n):
        return self._copy(offset_n=n)

    def start_after(self, document_fields_or_snapshot):
        return self._copy(cursor=("after", document_fields_or_snapshot))

    def start_at(self, document_fields_or_snapshot):
        return self._copy(cursor=("at", document_fields_or_snapshot))

    def count(self, alias=None):
        return _CountQuery(self, alias)

    def _effective_orders(self):
        """Firestore always breaks ties by document id (__name__), in the direction of the last order_by."""
        orders = list(self.orders)
        if orders and not any(f == "__name__" for f, _ in orders):
            orders.append(("__name__", orders[-1][1]))
        return orders

    def _cursor_key(self, orders):
        _, ref = self.cursor
        if hasattr(ref, "to_dict"):
            data, doc_id = ref.to_dict() or {}, getattr(ref, "id", None)
        else:
            data, doc_id = dict(ref or {}), (ref or {}).get("__name__")
        return tuple(_order_key(doc_id if f == "__name__" else _field(data, f)[1]) for f, _ in orders)

    def _run(self):
        out = []
        for snap in self.collection._snaps():
            data = snap.to_dict() or {}
            if not all(_matches(data, f, op, v) for f, op, v in self.filters):
                continue
            if any(f != "__name__" and not _field(data, f)[0] for f, _ in self.orders):
                continue
            out.append(snap)
        orders = self._effective_orders()

        def value(snap, field):
            return snap.id if field == "__name__" else _field(snap.to_dict() or {}, field)[1]

        for field, direction in reversed(orders):
            out.sort(key=lambda s: _order_key(value(s, field)), reverse=(direction == "DESCENDING"))
        if self.cursor is not None and orders:
            mode = self.cursor[0]
            ckey = self._cursor_key(orders)

            def past_cursor(snap):
                for (field, direction), c in zip(orders, ckey):
                    k = _order_key(value(snap, field))
                    if k == c:
                        continue
                    return (k > c) if direction != "DESCENDING" else (k < c)
                return mode == "at"  # equal on every order field: the cursor document itself

            out = [s for s in out if past_cursor(s)]
        if self.offset_n:
            out = out[int(self.offset_n):]
        if self.limit_n is not None:
            out = out[: int(self.limit_n)]
        return out

    def stream(self, *a, **k):
        out = self._run()
        self.collection._record(len(out))
        return out

    def get(self, *a, **k):
        return self.stream()


class _Collection:
    def __init__(self, store, path):
        self.store = store
        self.path = tuple(path)

    def document(self, doc_id=None):
        # Real Firestore auto-generates an id when none is given.
        if doc_id is None:
            import uuid

            doc_id = uuid.uuid4().hex[:20]
        return _Ref(self.store, self.path + (str(doc_id),))

    def where(self, field=None, op=None, value=None, **kwargs):
        return _Query(self).where(field, op, value, **kwargs)

    def order_by(self, field, direction="ASCENDING"):
        return _Query(self).order_by(field, direction)

    def limit(self, n):
        return _Query(self).limit(n)

    def offset(self, n):
        return _Query(self).offset(n)

    def count(self, alias=None):
        return _Query(self).count(alias)

    def start_after(self, document_fields_or_snapshot):
        return _Query(self).start_after(document_fields_or_snapshot)

    def _snaps(self):
        with self.store.lock:
            snaps = []
            for path, data in self.store.docs.items():
                if len(path) == len(self.path) + 1 and path[: len(self.path)] == self.path:
                    snaps.append(_Snap(path[-1], data))
        return snaps

    def _record(self, n):
        with self.store.lock:
            stats = self.store.stats
            stats["streams"] += 1
            stats["reads"] += int(n)
            name = "/".join(self.path)
            stats["reads_by_collection"][name] = stats["reads_by_collection"].get(name, 0) + int(n)

    def stream(self, *a, **k):
        snaps = self._snaps()
        self._record(len(snaps))
        return snaps

    def get(self, *a, **k):
        return self.stream()


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
        self.reset_stats()

    def reset_stats(self):
        """Read accounting, so tests can assert that a request does not stream a whole collection."""
        self.stats = {"reads": 0, "streams": 0, "aggregations": 0, "reads_by_collection": {}}

    def collection(self, name):
        return _Collection(self, (name,))

    def write(self, path, data, merge):
        if merge and isinstance(self.docs.get(path), dict):
            merged = copy.deepcopy(self.docs[path])
            merged.update({k: (v if v is _DELETE_FIELD else copy.deepcopy(v)) for k, v in data.items()})
            self.docs[path] = _apply_deletes(merged)
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


def set_store_modes(monkeypatch, **_ignored: str) -> None:
    """Kept for old tests: STORE_* flags are gone (Firestore only). Only turns audit logging on."""
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
