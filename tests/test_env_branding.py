import pytest
from app.core.config import settings
from app.core.tenant import resolve_tenant, get_default_tenant_profile
from app.services.firestore_service import normalize_firestore_project


def test_default_env_branding():
    """Verifies that default settings and tenant profile are loaded correctly."""
    profile = get_default_tenant_profile()
    assert profile.website_url == "https://varsoviaestate.com/en"
    assert profile.brand_name == "Varsovia Estate"
    assert profile.bot_name == "Varsovia Estate Advisor"
    assert profile.advisor_email == "advisor@varsoviaestate.com"
    assert profile.site_origin == "varsoviaestate.com"


def test_property_detail_url_generation():
    """Verifies that property detail URLs dynamically use BASE_WEBSITE_URL from settings and proper paths."""
    # Default without city/country (defaults to thailand/phuket for varsovia)
    url = settings.get_property_detail_url("luxury-villa-phuket")
    assert url == "https://varsoviaestate.com/en/properties/thailand/phuket/luxury-villa-phuket"

    # With specific city and country
    url_samui = settings.get_property_detail_url("the-horizon-silk-residences", country="thailand", city="samui")
    assert url_samui == "https://varsoviaestate.com/en/properties/thailand/samui/the-horizon-silk-residences"


def test_normalize_firestore_project_uses_env_detail_url():
    """Verifies that normalize_firestore_project formats detail_page_url with country and city."""
    raw_doc = {
        "title": {"en": "Sky Ocean Villa"},
        "slug": {"en": "sky-ocean-villa", "pl": "willa-sky-ocean"},
        "propertyType": "Villa",
        "priceNumeric": 15000000,
        "countryId": "thailand",
        "cityId": "samui",
        "isActive": True
    }
    normalized = normalize_firestore_project("test_doc_123", raw_doc)
    assert normalized["detail_page_url"] == "https://varsoviaestate.com/en/properties/thailand/samui/sky-ocean-villa"


def test_dynamic_env_override(monkeypatch):
    """
    Verifies that changing the domain or branding in settings (.env)
    immediately changes all URLs and tenant profiles without code changes.
    """
    monkeypatch.setattr(settings, "BASE_WEBSITE_URL", "https://new-realestate-brand.com/en")
    monkeypatch.setattr(settings, "SITE_ORIGIN", "new-realestate-brand.com")
    monkeypatch.setattr(settings, "BRAND_NAME", "New Real Estate")
    monkeypatch.setattr(settings, "BOT_NAME", "New Advisor")
    monkeypatch.setattr(settings, "ADVISOR_EMAIL", "advisor@new-realestate-brand.com")

    # Dynamic tenant resolution
    profile = resolve_tenant()
    assert profile.website_url == "https://new-realestate-brand.com/en"
    assert profile.brand_name == "New Real Estate"
    assert profile.bot_name == "New Advisor"
    assert profile.advisor_email == "advisor@new-realestate-brand.com"

    # Dynamic property detail URL
    url = settings.get_property_detail_url("modern-condo")
    assert url == "https://new-realestate-brand.com/en/oferty/modern-condo"


def test_tajlandia_resolves_to_tajlandia_tenant():
    """Explicit tajlandia.com site_origin must use Tajlandia branding and advisor email."""
    profile = resolve_tenant("tajlandia.com")
    assert profile.site_origin == "tajlandia.com"
    assert profile.advisor_email == "advisor@tajlandia.com"
    assert "Tajlandia" in profile.brand_name
    # Must not leak Varsovia advisor onto Tajlandia chats
    assert profile.advisor_email != "advisor@varsoviaestate.com"


def test_hello_greeting_with_firestore_conversation():
    """
    Verifies that sending 'hello' to public_agent with a FirestoreConversation
    returns a proper greeting rather than crashing or triggering server error.
    """
    from unittest.mock import MagicMock
    from app.services.public_agent import public_agent
    from app.services.firestore_service import FirestoreConversation

    mock_db = MagicMock()
    conv = FirestoreConversation("test-session-hello", {
        "sessionId": "test-session-hello",
        "siteOrigin": "varsoviaestate.com",
        "messageHistory": [],
        "preferences": {}
    })

    res = public_agent.run_conversation(
        db=mock_db,
        conversation=conv,
        user_message="hello",
        requested_language="en"
    )

    assert res["type"] == "text"
    assert "error" not in res.get("text", "").lower()
    assert "temporary server error" not in res.get("text", "").lower()
    assert len(res.get("text", "")) > 10
    assert len(res.get("quick_replies", [])) > 0
