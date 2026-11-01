"""Regression: lead scores were 0 for every lead (QA BUG 4).

Root causes covered:
  * ``create_lead_in_firestore`` (public chat contact capture + POST /chat/lead
    Firestore sync) never wrote a score at all.
  * ``LeadRecord`` read ``lead_score``/``score`` only and fell back to 0.0, so
    any doc without a stored score (portal/imported/older/camelCase docs) read 0
    and was dropped by any ``min_score`` filter ("high-intent buyer leads").
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.repositories import leads_read_repo
from app.repositories.leads_read_repo import LeadRecord
from app.services.lead_service import calculate_lead_score


def _client():
    from app.services.firestore_service import get_firestore_client

    return get_firestore_client("user")


def _seed(doc_id, doc):
    _client().collection("leads").document(doc_id).set(doc)


def _leads_docs(fake):
    return {path[-1]: data for path, data in fake.docs.items() if len(path) == 2 and path[0] == "leads"}


# --------------------------------------------------------------------------
# Read side
# --------------------------------------------------------------------------

def test_unscored_doc_with_strong_signals_gets_computed_score():
    rec = LeadRecord({
        "contact_name": "Anna",
        "contact_email": "anna@example.com",
        "budget_max": 9_000_000,
        "preferred_location": "Phuket",
    }, "l1")
    expected = calculate_lead_score(has_contact=True, budget_specified=True, property_requested=True)
    assert rec.lead_score == expected
    assert rec.lead_score >= 70


def test_unscored_camelcase_portal_doc_gets_computed_score():
    """Docs written by create_lead_in_firestore / the portal use camelCase fields."""
    rec = LeadRecord({
        "name": "Ben",
        "email": "ben@example.com",
        "phone": "+66812345678",
        "propertyId": "prop-123",
        "message": "Interested in the villa",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }, "l2")
    assert rec.lead_score == calculate_lead_score(has_contact=True, property_requested=True)
    assert rec.lead_score > 0


def test_explicit_stored_score_is_kept():
    assert LeadRecord({"lead_score": 42, "contact_email": "x@example.com"}, "a").lead_score == 42.0
    assert LeadRecord({"leadScore": 77.5}, "b").lead_score == 77.5
    assert LeadRecord({"score": "64"}, "c").lead_score == 64.0


def test_placeholder_zero_score_is_recomputed():
    """calculate_lead_score never returns < 10, so a stored 0 is a default placeholder."""
    rec = LeadRecord({"lead_score": 0, "contact_email": "z@example.com", "budget": 5_000_000}, "z")
    assert rec.lead_score == calculate_lead_score(has_contact=True, budget_specified=True)


def test_placeholder_contact_strings_are_not_contact():
    rec = LeadRecord({"contact_email": "Not specified", "contact_phone": "Not specified"}, "n")
    assert rec.lead_score == calculate_lead_score()


# --------------------------------------------------------------------------
# Write side — every creation path stores a score
# --------------------------------------------------------------------------

def test_create_lead_in_firestore_stores_score(_default_fake_firestore):
    from app.services.firestore_service import create_lead_in_firestore

    doc_id = create_lead_in_firestore({
        "name": "Cara",
        "email": "cara@example.com",
        "phone": "+66811111111",
        "property_id": "prop-9",
        "budget_max": 7_000_000,
        "message": "Call me",
    })
    assert doc_id
    stored = _leads_docs(_default_fake_firestore)[doc_id]
    assert stored["lead_score"] == calculate_lead_score(
        has_contact=True, budget_specified=True, property_requested=True
    )
    assert stored["lead_score"] > 0


def test_create_lead_in_firestore_minimal_still_scored(_default_fake_firestore):
    from app.services.firestore_service import create_lead_in_firestore

    doc_id = create_lead_in_firestore({"name": "Dan", "email": "dan@example.com"})
    stored = _leads_docs(_default_fake_firestore)[doc_id]
    assert stored["lead_score"] == calculate_lead_score(has_contact=True)


def test_session_lead_persist_path_stores_score(_default_fake_firestore):
    from app.services.lead_service import _persist_firestore_lead, record_or_update_lead

    conv = type("Conv", (), {
        "id": "c-1", "session_id": "s-score-1", "agent_type": "public",
        "message_history": [{}, {}, {}], "site_origin": "tajlandia.com", "lead_score": 0.0,
    })()
    lead = record_or_update_lead(
        conversation=conv, contact_email="eve@example.com",
        preferred_location="Phuket", budget_max=8_000_000,
    )
    doc_id = _persist_firestore_lead(conv, lead)
    stored = _leads_docs(_default_fake_firestore)[doc_id]
    assert stored["lead_score"] == 100.0


def test_direct_lead_endpoint_firestore_sync_stores_score(monkeypatch, _default_fake_firestore):
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "ENABLE_LEAD_SAVING", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_FIRESTORE_SYNC", True, raising=False)
    r = TestClient(app).post("/api/v1/chat/lead", json={
        "session_id": "lead-score-endpoint",
        "contact_name": "Finn",
        "contact_email": "finn@example.com",
        "preferred_location": "Pattaya",
    })
    assert r.status_code == 200, r.text
    # Background session-lead save runs in a thread; give it a moment.
    deadline = time.time() + 3
    while time.time() < deadline and len(_leads_docs(_default_fake_firestore)) < 2:
        time.sleep(0.05)
    docs = _leads_docs(_default_fake_firestore)
    synced = [d for d in docs.values() if d.get("sourceType")]
    assert synced, f"create_lead_in_firestore path not exercised: {docs}"
    for d in docs.values():
        assert float(d.get("lead_score") or 0) > 0, d


# --------------------------------------------------------------------------
# Consumers — high-intent filter / score ordering
# --------------------------------------------------------------------------

def test_high_intent_filter_returns_historical_unscored_leads():
    now = datetime.now(timezone.utc)
    # Historical doc, never scored, strong signals
    _seed("hist-hot", {
        "name": "Hot Buyer", "email": "hot@example.com", "phone": "+66899999999",
        "budget_max": 12_000_000, "propertyName": "Sea View Villa",
        "createdAt": (now - timedelta(days=40)).isoformat(),
    })
    # Weak doc (no contact, nothing else)
    _seed("hist-cold", {"name": "Website Visitor", "createdAt": now.isoformat()})
    # Explicitly stored low score must be respected
    _seed("stored-low", {
        "contact_email": "low@example.com", "budget_max": 1, "preferred_location": "X",
        "lead_score": 20, "created_at": now.isoformat(),
    })

    hot = leads_read_repo.query_leads(None, min_score=70, limit=50)
    ids = [r.id for r in hot]
    assert ids == ["hist-hot"]
    assert leads_read_repo.count_leads(None, min_score=70) == 1

    ordered = leads_read_repo.query_leads(None, limit=50, order="score")
    assert ordered[0].id == "hist-hot"
    assert all(r.lead_score > 0 for r in ordered)
