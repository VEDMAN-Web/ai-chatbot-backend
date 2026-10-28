"""Inquire form support: contact numbers + localized copy, and a form submission that never injects a chat prompt."""
import pytest

from app.core.tenant import resolve_tenant
from app.services.contact_info import FORM_COPY, build_contact_info
from app.api.v1 import chat as chat_api


def test_varsovia_contacts_come_from_the_site_footer():
    info = build_contact_info(resolve_tenant("varsoviaestate.com"), "en")
    phones = {c["label"]: c for c in info["contacts"]}
    assert phones["Thailand"]["phone"] == "+66 81 679 4414"
    assert phones["Thailand"]["tel_url"] == "tel:+66816794414"
    assert phones["Thailand"]["whatsapp_url"] == "https://wa.me/66816794414"
    assert phones["Cambodia"]["whatsapp_url"] == "https://wa.me/855963015807"


@pytest.mark.parametrize("lang", ["en", "pl", "th"])
def test_form_copy_is_complete_in_every_language(lang):
    info = build_contact_info(resolve_tenant("varsoviaestate.com"), lang)
    assert info["language"] == lang
    assert set(info["inquiry_form"]) == set(FORM_COPY["en"])
    assert all(v.strip() for v in info["inquiry_form"].values())


def test_unknown_language_falls_back_to_english():
    assert build_contact_info(resolve_tenant("varsoviaestate.com"), "xx")["language"] == "en"


def test_sites_without_known_numbers_expose_no_invented_ones():
    for origin in ("thai.estate", "tajlandia.com"):
        assert build_contact_info(resolve_tenant(origin), "en")["contacts"] == []


def test_contact_info_endpoint_function():
    out = chat_api.get_contact_info(site_origin="varsoviaestate.com", language="pl")
    assert out["inquiry_form"]["title"].startswith("Cieszymy")
    assert len(out["contacts"]) == 2


def test_public_request_accepts_listing_reference_for_the_form():
    req = chat_api.PublicChatRequest(session_id="s1", contact_email="a@b.com", property_id="MIijGwytZdYL1EiGumjV")
    assert req.property_id == "MIijGwytZdYL1EiGumjV" and not req.message


def test_widget_copy_present_for_all_languages():
    from app.core.tenant import resolve_tenant
    from app.services.contact_info import build_contact_info
    for lang in ("en", "pl", "th"):
        w = build_contact_info(resolve_tenant(None), lang)["widget"]
        assert w["cta_action"] == "open_inquiry_form"
        assert w["cta_label"] and w["greeting"] and w["suggestions"]
    assert build_contact_info(resolve_tenant(None), "en")["widget"]["cta_label"] == "Talk to an advisor"
    assert build_contact_info(resolve_tenant(None), "pl")["widget"]["cta_label"] == "Porozmawiaj z doradcą"
