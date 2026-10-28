"""
USER STAGE F1: public property index + Firestore leads (dict-based fake Firestore).
No live network. Does not touch admin modules.
"""
from __future__ import annotations

import copy
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.core.config import settings
from app.services.lead_service import (
    clear_session_leads,
    make_lead_dedupe_key,
    persist_lead_to_firestore,
    record_or_update_lead,
)
from app.services.property_index import (
    IndexUnavailable,
    get_property_index,
    reset_property_index_for_tests,
    sanitize_public_card,
)
from app.services.property_search import PropertyFilterParams, search_properties_safe, to_compact_projection
from app.services.public_agent import PublicAgentService, search_properties_unified


# ---------------------------------------------------------------------------
# Dict-based Fake Firestore
# ---------------------------------------------------------------------------

class _Snap:
    def __init__(self, doc_id: str, data: Optional[dict]):
        self.id = doc_id
        self.exists = data is not None
        self._data = None if data is None else copy.deepcopy(data)

    def to_dict(self):
        return None if self._data is None else copy.deepcopy(self._data)


class _Doc:
    def __init__(self, store: "FakeFirestore", collection: str, doc_id: str):
        self.store = store
        self.collection_name = collection
        self.id = doc_id

    def get(self):
        return _Snap(self.id, self.store.get(self.collection_name, self.id))

    def set(self, data, merge=False):
        self.store.set(self.collection_name, self.id, data, merge=merge)

    def update(self, data):
        self.store.update(self.collection_name, self.id, data)


class _Query:
    def __init__(self, store: "FakeFirestore", collection: str, filters: Optional[List[Tuple]] = None):
        self.store = store
        self.collection = collection
        self.filters = list(filters or [])
        self._limit = None

    def where(self, *args, **kwargs):
        field = kwargs.get("field_path") or (args[0] if args else None)
        op = kwargs.get("op_string") or (args[1] if len(args) > 1 else "==")
        value = kwargs.get("value")
        if value is None and len(args) >= 3:
            value = args[2]
        # FieldFilter objects from google SDK: treat as opaque; tests use string triples
        if field is not None:
            self.filters.append((str(field), str(op), value))
        return self

    def limit(self, n):
        self._limit = int(n)
        return self

    def stream(self):
        rows = []
        for doc_id, data in self.store.items(self.collection):
            ok = True
            for field, op, value in self.filters:
                cur = (data or {}).get(field)
                if op in ("==", "EQUAL") and cur != value:
                    ok = False
                    break
            if ok:
                rows.append(_Snap(doc_id, data))
        if self._limit is not None:
            rows = rows[: self._limit]
        return iter(rows)


class _Collection:
    def __init__(self, store: "FakeFirestore", name: str):
        self.store = store
        self.name = name

    def document(self, doc_id=None):
        if not doc_id:
            doc_id = uuid.uuid4().hex
        return _Doc(self.store, self.name, str(doc_id))

    def stream(self):
        for doc_id, data in self.store.items(self.name):
            yield _Snap(doc_id, data)

    def where(self, *args, **kwargs):
        q = _Query(self.store, self.name)
        return q.where(*args, **kwargs)

    def limit(self, n):
        return _Query(self.store, self.name).limit(n)


class FakeFirestore:
    def __init__(self):
        self.docs: Dict[Tuple[str, str], dict] = {}

    def collection(self, name: str):
        return _Collection(self, name)

    def get(self, collection: str, doc_id: str):
        return self.docs.get((collection, doc_id))

    def items(self, collection: str):
        return [(doc_id, data) for (col, doc_id), data in self.docs.items() if col == collection]

    def set(self, collection: str, doc_id: str, data: dict, merge: bool = False):
        key = (collection, doc_id)
        payload = copy.deepcopy(data)
        current = self.docs.get(key)
        if merge and isinstance(current, dict):
            merged = copy.deepcopy(current)
            merged.update(payload)
            self.docs[key] = merged
        else:
            self.docs[key] = payload

    def update(self, collection: str, doc_id: str, data: dict):
        key = (collection, doc_id)
        current = self.docs.get(key)
        if not isinstance(current, dict):
            raise RuntimeError(f"missing document {doc_id}")
        merged = copy.deepcopy(current)
        merged.update(copy.deepcopy(data))
        self.docs[key] = merged


def _project(
    doc_id: str,
    *,
    name: str,
    city: str = "phuket",
    country: str = "thailand",
    price: float = 10_000_000,
    beds: int = 3,
    ptype: str = "Villa",
    websites: Optional[List[str]] = None,
    is_active: bool = True,
    status: str = "Available",
    has_pool: bool = True,
    amenities: Optional[List[str]] = None,
) -> Tuple[str, dict]:
    return doc_id, {
        "name": {"en": name},
        "cityId": city,
        "countryId": country,
        "priceFrom": price,
        "currency": "THB",
        "propertyType": ptype,
        "layouts": [{"bedrooms": beds}],
        "areaFrom": 200,
        "isActive": is_active,
        "completionStatus": status,
        "websites": websites or ["varsoviaestate.com", "tajlandia.com"],
        "amenities": amenities or (["Pool"] if has_pool else []),
        "tags": amenities or (["Pool"] if has_pool else []),
        "highlights": [],
        "images": [f"https://cdn.example.com/{doc_id}.jpg"],
        "coverImage": f"https://cdn.example.com/{doc_id}.jpg",
        "slug": {"en": doc_id},
    }


@pytest.fixture
def fake_fs(monkeypatch):
    reset_property_index_for_tests()
    clear_session_leads()
    fs = FakeFirestore()
    for doc_id, data in [
        _project("p1", name="Alpha Villa Phuket", city="phuket", price=12_000_000, beds=3),
        _project("p2", name="Beta Condo Samui", city="samui", price=8_000_000, beds=2, ptype="Condominium", has_pool=False),
        _project("p3", name="Gamma Villa Phuket", city="phuket", price=25_000_000, beds=4),
        _project("inactive1", name="Hidden Inactive", is_active=False),
        _project("archived1", name="Hidden Archived", status="archived"),
        _project("othersite1", name="Other Site Only", websites=["other-brand.com"]),
    ]:
        fs.set("projects", doc_id, data)
    fs.set("meta", "properties_version", {"version": 1, "updated_at": "2026-01-01T00:00:00Z"})

    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda target="user": fs)
    monkeypatch.setattr("app.services.firestore_service.get_user_firestore_client", lambda: fs)
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "firestore")
    monkeypatch.setattr(settings, "STORE_LEADS_USER", "firestore")
    monkeypatch.setattr(settings, "ENABLE_LEAD_SAVING", True)

    idx = get_property_index()
    idx.set_client_override(fs)
    idx.ensure_fresh(force=True)
    yield fs
    reset_property_index_for_tests()
    clear_session_leads()


# ---------------------------------------------------------------------------
# Index load / version / down
# ---------------------------------------------------------------------------

def test_index_loads_public_projects_only(fake_fs):
    idx = get_property_index()
    rows = idx.all_records()
    ids = {r["property_id"] for r in rows}
    assert "p1" in ids and "p2" in ids and "p3" in ids
    assert "inactive1" not in ids
    assert "archived1" not in ids
    for r in rows:
        assert "raw_firestore_data" not in r


def test_version_bump_triggers_reload(fake_fs):
    idx = get_property_index()
    assert idx.get_by_id("p1") is not None
    fake_fs.set("projects", "p4", _project("p4", name="Delta New Villa")[1])
    fake_fs.set("meta", "properties_version", {"version": 2, "updated_at": "2026-01-02T00:00:00Z"})
    # Force version check window
    idx._version_checked_at = 0.0
    idx._loaded_at = 0.0
    idx.ensure_fresh()
    assert idx.get_by_id("p4") is not None


def test_firestore_down_with_cache_serves_last_good(fake_fs, monkeypatch):
    idx = get_property_index()
    assert idx.has_cache()
    monkeypatch.setattr(idx, "_client", lambda: None)
    idx._version_checked_at = 0.0
    idx._loaded_at = 0.0
    idx.ensure_fresh()  # should not raise
    assert idx.get_by_id("p1") is not None


def test_firestore_down_without_cache_raises(monkeypatch):
    reset_property_index_for_tests()
    idx = get_property_index()
    idx.set_client_override(None)
    monkeypatch.setattr("app.services.firestore_service.get_firestore_client", lambda target="user": None)
    with pytest.raises(IndexUnavailable):
        idx.ensure_fresh(force=True)


def test_service_busy_reply_on_index_unavailable(monkeypatch):
    reset_property_index_for_tests()
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "firestore")
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None

    def _boom(*a, **k):
        raise IndexUnavailable("no cache")

    monkeypatch.setattr(agent, "_run_conversation_internal", _boom)
    conv = SimpleNamespace(session_id="s", id="c", site_origin="varsoviaestate.com", preferences={})
    out = agent.run_conversation(db=None, conversation=conv, user_message="show villas")
    assert out["fallback_type"] == "error"
    assert "busy" in out["text"].lower()


# ---------------------------------------------------------------------------
# Filters / sort / pagination / visibility
# ---------------------------------------------------------------------------

def test_filters_location_type_price_beds(fake_fs):
    idx = get_property_index()
    cards = idx.search(PropertyFilterParams(
        location="Phuket",
        property_type="Villa",
        max_price=Decimal("20000000"),
        bedrooms=3,
        websites=["varsoviaestate.com"],
        limit=10,
    ))
    ids = [c["property_id"] for c in cards]
    assert ids == ["p1"]


def test_sort_cheapest_and_expensive(fake_fs):
    idx = get_property_index()
    cheap = idx.search(PropertyFilterParams(sort_by_price="asc", limit=10))
    pricey = idx.search(PropertyFilterParams(sort_by_price="desc", limit=10))
    assert _price(cheap[0]) <= _price(cheap[-1])
    assert _price(pricey[0]) >= _price(pricey[-1])


def _price(card):
    return float(card.get("price_thb_min") or card.get("price_numeric_min") or 0)


def test_pagination_and_exclude_seen(fake_fs):
    idx = get_property_index()
    page1 = idx.search(PropertyFilterParams(limit=1, offset=0, sort_by_price="asc"))
    page2 = idx.search(PropertyFilterParams(limit=1, offset=1, sort_by_price="asc"))
    assert page1[0]["property_id"] != page2[0]["property_id"]
    excluded = idx.search(
        PropertyFilterParams(limit=10),
        exclude_property_ids=[page1[0]["property_id"]],
    )
    assert page1[0]["property_id"] not in {c["property_id"] for c in excluded}


def test_site_scoping_hides_other_site(fake_fs):
    idx = get_property_index()
    assert idx.get_by_id("othersite1", site="varsoviaestate.com") is None
    assert idx.get_by_id("p1", site="varsoviaestate.com") is not None


def test_inactive_and_archived_hidden(fake_fs):
    idx = get_property_index()
    assert idx.get_by_id("inactive1") is None
    assert idx.get_by_id("archived1") is None


def test_compact_projection_unchanged_shape(fake_fs):
    from app.services.property_search import FirestorePropertyAdapter
    card = get_property_index().get_by_id("p1")
    compact = to_compact_projection(FirestorePropertyAdapter(card), intent="search")
    assert set(compact.keys()) >= {"id", "name", "price", "location", "bedrooms", "area", "pool"}
    assert "raw_firestore_data" not in compact


def test_search_performance_under_50ms(fake_fs):
    idx = get_property_index()
    # Expand to ~200 synthetic docs in-memory
    for i in range(200):
        fake_fs.set("projects", f"bulk-{i}", _project(f"bulk-{i}", name=f"Bulk Villa {i}", price=5_000_000 + i * 1000)[1])
    fake_fs.set("meta", "properties_version", {"version": 99, "updated_at": "x"})
    idx._version_checked_at = 0.0
    idx._loaded_at = 0.0
    idx.ensure_fresh(force=True)
    t0 = time.perf_counter()
    for _ in range(5):
        idx.search(PropertyFilterParams(location="phuket", max_price=Decimal("50000000"), limit=6))
    elapsed_ms = (time.perf_counter() - t0) / 5 * 1000
    assert elapsed_ms < 50, f"avg search {elapsed_ms:.1f}ms"


# ---------------------------------------------------------------------------
# Leads dedupe
# ---------------------------------------------------------------------------

def test_leads_dedupe_firestore_mode(fake_fs, monkeypatch):
    monkeypatch.setattr(settings, "STORE_LEADS_USER", "firestore")
    conv = SimpleNamespace(
        session_id="lead-sess-1", id="c1", site_origin="varsoviaestate.com",
        message_history=[], preferences={}, agent_type="public",
    )
    lead1 = record_or_update_lead(
        conversation=conv,
        contact_name="Pat",
        contact_email="pat@example.com",
        contact_phone="+66811112222",
        notes="property_id=p1; property_name=Alpha; summary=first",
    )
    doc_id = persist_lead_to_firestore(
        lead1, conv, notes="property_id=p1; property_name=Alpha; summary=first",
        client_override=fake_fs,
    )
    assert doc_id
    leads = list(fake_fs.items("leads"))
    assert len(leads) == 1
    # Second contact same phone+site updates summary, no duplicate
    lead2 = record_or_update_lead(
        conversation=conv,
        contact_name="Pat",
        contact_phone="+66811112222",
        notes="property_id=p3; property_name=Gamma; summary=second",
    )
    persist_lead_to_firestore(
        lead2, conv, notes="property_id=p3; property_name=Gamma; summary=second",
        client_override=fake_fs,
    )
    leads = list(fake_fs.items("leads"))
    assert len(leads) == 1
    body = leads[0][1]
    assert body.get("dedupe_key") == make_lead_dedupe_key(
        phone="+66811112222", site="varsoviaestate.com"
    )
    assert "second" in (body.get("summary") or "")
    assert body.get("property_id") == "p3"


def test_public_chat_no_sql_uses_index(fake_fs, monkeypatch):
    monkeypatch.setattr(settings, "STORE_PROPERTIES_READ_USER", "firestore")
    monkeypatch.setattr(settings, "USER_DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/none")
    cards = search_properties_unified(
        db=None,
        params=PropertyFilterParams(
            location="Phuket",
            property_type="Villa",
            websites=["varsoviaestate.com"],
            limit=5,
        ),
        limit=5,
    )
    assert cards
    assert all(c["property_id"] in {"p1", "p3"} for c in cards)
    assert all("raw_firestore_data" not in c for c in cards)


def test_config_status_exposes_store_modes():
    from app.core.config import get_public_config_status
    st = get_public_config_status()
    assert "store_properties_read_user" in st
    assert "store_leads_user" in st
