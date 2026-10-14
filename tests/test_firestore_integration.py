import pytest
from app.services.firestore_service import (
    check_firestore_health,
    fetch_properties_from_firestore,
    search_properties_firestore,
    create_lead_in_firestore,
    log_query_to_firestore,
    create_audit_log_in_firestore,
    get_property_by_id_from_firestore
)


def test_firestore_health_connection():
    """Verifies live Firestore connection to varsovia-8c135 database."""
    health = check_firestore_health()
    assert health["connected"] is True
    assert health["project_id"] in ["varsovia-test-clone-26", "varsovia-8c135"]
    assert health["collections_count"] > 0


def test_fetch_properties_from_firestore():
    """Verifies fetching and normalizing real projects from Firestore."""
    props = fetch_properties_from_firestore(limit=5, active_only=True)
    assert len(props) > 0
    p = props[0]
    assert "property_id" in p
    assert "name" in p
    assert "price" in p
    assert "location" in p
    assert "images" in p


def test_search_properties_firestore_with_maps():
    """Verifies searching Firestore projects and attaching dynamic Google Maps."""
    matched = search_properties_firestore(limit=3)
    assert len(matched) > 0
    p = matched[0]
    assert "maps_url" in p
    assert "https://www.google.com/maps/search/?" in p["maps_url"]
    assert "maps_embed_url" in p


def test_create_lead_in_firestore():
    """Verifies creating a customer lead in Firestore leads collection."""
    lead_data = {
        "name": "Integration Test Lead",
        "email": "test-lead@tajlandia.com",
        "phone": "+48 500 000 000",
        "brand": "tajlandia",
        "source_type": "automated_test",
        "message": "Interested in pool villas in Koh Samui"
    }
    lead_id = create_lead_in_firestore(lead_data)
    try:
        assert lead_id is not None
        assert len(lead_id) > 5
    finally:
        from app.services.firestore_service import get_firestore_client
        db = get_firestore_client()
        if db and lead_id:
            db.collection("leads").document(lead_id).delete()


def test_log_query_to_firestore():
    """Verifies logging chatbot questions, answers, and error state."""
    log_id = log_query_to_firestore(
        session_id="test-session-int",
        user_question="Are there villas with private pool?",
        ai_response="Yes, we have luxury pool villas available.",
        matched_properties=["0R2y0WWvso5xobl6uFw1"],
        latency_ms=120
    )
    try:
        assert log_id is not None
        assert len(log_id) > 5
    finally:
        from app.services.firestore_service import get_firestore_client
        db = get_firestore_client()
        if db and log_id:
            db.collection("chat_query_logs").document(log_id).delete()


def test_create_audit_log_in_firestore():
    """Verifies recording administrative change audit log in admin_audit_log collection."""
    audit_id = create_audit_log_in_firestore(
        admin_email="admin@tajlandia.com",
        action="property_spec_updated",
        details={"field": "price", "old": "7.5M", "new": "7.99M"},
        target_type="property",
        target_id="PROP-TEST-001"
    )
    try:
        assert audit_id is not None
        assert len(audit_id) > 5
    finally:
        from app.services.firestore_service import get_firestore_client
        db = get_firestore_client()
        if db and audit_id:
            db.collection("admin_audit_log").document(audit_id).delete()


def test_firestore_failure_raises_error_not_fallback(monkeypatch):
    """
    Verifies that when Firestore is unavailable, FirestoreDatabaseError is raised,
    rather than silently masking it with PostgreSQL fallbacks.
    """
    from app.services.firestore_service import FirestoreDatabaseError
    import app.services.firestore_service as fs_module

    # Force get_firestore_client to return None
    monkeypatch.setattr(fs_module, "get_firestore_client", lambda: None)

    with pytest.raises(FirestoreDatabaseError):
        fs_module.fetch_properties_from_firestore()


def test_get_property_by_name_and_slug_from_firestore():
    """Verifies that properties can be found by exact title or slug in Firestore."""
    # Look up Sea Heaven Phuket Naithon Phase 3.1
    prop = get_property_by_id_from_firestore("Sea Heaven Phuket Naithon Phase 3.1")
    assert prop is not None
    assert prop["property_id"] == "sea-heaven-phuket-naithon-phase-3-1"
    assert "Sea Heaven" in prop["name"]
    assert prop["detail_page_url"] is not None
    assert "sea-heaven-phuket-naithon-phase-3-1" in prop["detail_page_url"]


def test_clean_response_text_removes_em_dashes():
    """Verifies that clean_response_text normalizes em-dashes and en-dashes to professional hyphens/points."""
    from app.services.public_agent import clean_response_text, PublicAgentService

    # 1. Spaced em-dash
    text1 = "Welcome to Varsovia Estate — your advisor for Thailand real estate."
    assert clean_response_text(text1) == "Welcome to Varsovia Estate - your advisor for Thailand real estate."

    # 2. Number range with en-dash and em-dash
    text2 = "Bedrooms: 1–4 Beds, Area: 50—120 sqm"
    assert clean_response_text(text2) == "Bedrooms: 1-4 Beds, Area: 50-120 sqm"

    # 3. Post processing dictionary
    agent = PublicAgentService.__new__(PublicAgentService)
    payload = {
        "text": "Top Option: 8 Atelier Residence — Phuket — 1–4 Beds",
        "preferences": {
            "focus_summary": "Focusing on Phuket — Villas",
            "what_we_fetched": ["Verified properties — Phuket"]
        },
        "properties": [
            {
                "property_id": "prop-1",
                "bedrooms": "1–4 Beds",
                "area": "80–200 sqm",
                "price": "from 10M THB (~€260k — ~1.1M PLN)"
            }
        ]
    }
    processed = agent._post_process_response(payload)
    assert "—" not in processed["text"]
    assert "–" not in processed["text"]
    assert "—" not in processed["preferences"]["focus_summary"]
    assert "—" not in processed["preferences"]["what_we_fetched"][0]
    assert "—" not in processed["properties"][0]["bedrooms"]
    assert "–" not in processed["properties"][0]["bedrooms"]
    assert "–" not in processed["properties"][0]["area"]
    assert "—" not in processed["properties"][0]["price"]

