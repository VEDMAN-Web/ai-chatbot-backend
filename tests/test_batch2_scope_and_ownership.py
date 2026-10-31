"""Batch 2: Cambodia support with per-site market scope, did-you-mean for names given as a location,
and no invented ownership/amenities on cards. Pure unit tests: no network, no Firestore."""
import app.services.firestore_service as fs
from app.core.tenant import set_request_site, reset_request_site, allowed_countries_for_site
from app.services.intent_router import fuzzy_resolve_location, LOCATION_ALIASES
from app.services.public_agent import ensure_structured_response, _out_of_scope_markets
from app.services.i18n_service import get_public_system_prompt, get_compact_system_prompt


def _doc(name, city, country="thailand", price=5_000_000, cur="THB", ptype="villa"):
    slug = name.lower().replace(" ", "-")
    return {"name": {"pl": name, "en": ""}, "cityId": city, "countryId": country, "propertyType": ptype,
            "priceFrom": price, "currency": cur, "isActive": True, "slug": {"pl": slug, "en": slug}}


def _cards():
    docs = [_doc("Phuket Villa One", "phuket"),
            _doc("Vue Aston", "phnom-penh", country="cambodia", price=110_000, cur="USD", ptype="condominium"),
            _doc("Coral Kandima", "maldives", country="maldives", price=300_000, cur="USD")]
    return [fs.normalize_firestore_project(f"id{i}", d) for i, d in enumerate(docs)]


def _search(monkeypatch, site, **kw):
    cards = _cards()
    monkeypatch.setattr(fs, "fetch_properties_from_firestore", lambda **k: list(cards))
    tok = set_request_site(site)
    try:
        return [c["name"] for c in fs.search_properties_firestore(target="user", limit=10, **kw)]
    finally:
        reset_request_site(tok)


def test_polish_cambodia_spellings_find_cambodian_listings(monkeypatch):
    for loc in ("Kambodża", "kambodzy", "Cambodia", "Phnom Penh"):
        assert _search(monkeypatch, "varsoviaestate.com", location=loc) == ["Vue Aston"], loc
    assert _search(monkeypatch, "varsoviaestate.com", location="Malediwy") == ["Coral Kandima"]


def test_thailand_only_sites_never_show_other_countries(monkeypatch):
    for site in ("tajlandia.com", "thai.estate"):
        for loc in ("Kambodża", "Cambodia", "Phnom Penh", "Maldives"):
            assert _search(monkeypatch, site, location=loc) == [], (site, loc)
        assert _search(monkeypatch, site, location="Phuket") == ["Phuket Villa One"]


def test_varsovia_keeps_every_market_and_admin_is_unrestricted():
    assert allowed_countries_for_site("varsoviaestate.com") is None
    assert allowed_countries_for_site("tajlandia.com") == ["thailand"]
    assert allowed_countries_for_site("https://www.thai.estate/en") == ["thailand"]
    assert allowed_countries_for_site() is None  # outside a public chat request (admin)


def test_router_recognises_cambodia_places():
    for text, canon in [("Pokaż apartamenty w Kambodży", "Cambodia"), ("condos in Phnom Penh", "Phnom Penh"),
                        ("coś w Sihanoukville", "Sihanoukville"), ("Malediwy", "Maldives")]:
        assert fuzzy_resolve_location(text) == canon, text
    assert fuzzy_resolve_location("villa in Phuket") == "Phuket"
    assert len(LOCATION_ALIASES) > 40


def test_out_of_scope_markets_only_for_thailand_sites():
    assert "cambodia" in _out_of_scope_markets("tajlandia.com")
    assert _out_of_scope_markets("varsoviaestate.com") == []


def test_prompts_carry_the_right_market_scope():
    assert "Cambodia" in get_public_system_prompt("en", "varsoviaestate.com")
    assert "Thailand listings only" in get_public_system_prompt("en", "tajlandia.com")
    assert "Thailand listings only" in get_compact_system_prompt("en", "thai.estate")
    for site in ("varsoviaestate.com", "tajlandia.com"):
        for lang in ("en", "pl", "th"):
            p = get_public_system_prompt(lang, site)
            assert "Registered Leasehold Villa]" not in p and "Rejestrowany Leasehold na willę]" not in p


def _table(name):
    return ("Here are options:\n\n| Property | Location | Price | Bedrooms |\n|---|---|---|---|\n"
            f"| {name} | Phuket | 5.0M THB | 3 |\n")


def test_cards_never_claim_freehold_or_invent_amenities():
    card = _cards()[0]
    for lang in ("en", "pl", "th"):
        out = ensure_structured_response(_table(card["name"]), cards=[card], user_message="show villas", language=lang)
        low = out.lower()
        assert "100% foreign freehold" not in low and "30-year registered" not in low
        assert "tropical gardens" not in low and "luxury amenities" not in low


def test_get_property_by_id_miss_gives_did_you_mean(monkeypatch):
    """The model sometimes looks a bare name up with get_property_by_id; a miss must still suggest near names."""
    import json
    from types import SimpleNamespace
    import app.services.property_name_index as pni
    from app.services.public_agent import public_agent
    monkeypatch.setattr(fs, "get_property_by_id_from_firestore", lambda pid, target="user": None)
    rows = [{"doc_id": "a1", "id": "a1", "name": {"pl": "Satoshi's Paradise", "en": ""}, "cityId": "sihanoukville"},
            {"doc_id": "a2", "id": "a2", "name": {"pl": "Anava Samui", "en": ""}, "cityId": "samui"}]
    idx = pni.get_property_name_index()
    monkeypatch.setattr(idx, "_loader", lambda: rows)
    idx.ensure_fresh(force=True)
    conv = SimpleNamespace(session_id="t", site_origin="varsoviaestate.com", preferences={}, id="1")
    try:
        res, cards_out, _ = public_agent.execute_tool(None, conv, "get_property_by_id", {"property_name": "Satoshi Residence"})
        assert res["found"] is False and cards_out == []
        assert res["name_suggestions"] == ["Satoshi's Paradise"]
        assert "Did you mean" in res["message"]
        json.dumps(res)  # must be serialisable: it is sent back to the model
    finally:
        idx.clear()  # do not leak the fake index into other tests


def test_valid_cards_survive_when_the_model_rewords_a_price():
    """If only the model's wording fails validation, the verified cards must still be shown (deterministic text)."""
    from app.services.public_agent import public_agent
    card = _cards()[0]  # Phuket Villa One, 5.0M THB
    bad_text = f"Here is **{card['name']}** in Phuket for only 1,234,567 THB!"
    text, out = public_agent._validate_or_fallback(
        bad_text, [card], language="en", site_origin="varsoviaestate.com",
        applied_filters={"location": "Phuket"}, criteria_summary="Phuket")
    assert out and out[0]["name"] == card["name"]
    assert "1,234,567" not in text
    assert "No verified listing matches" not in text


def test_project_names_that_contain_a_place_word_are_names_not_searches():
    from app.services.intent_router import looks_like_project_name, message_looks_like_filter_search
    for name in ["Paragon Villas Bophut", "Azure Sky Residences Phuket", "Villa Mirage Samui",
                 "Ile kosztuje Golden Lotus Penthouse Bangkok?", "Royal Orchid Beachfront Estate"]:
        assert looks_like_project_name(name), name
        assert not message_looks_like_filter_search(name), name


def test_real_searches_are_still_searches():
    from app.services.intent_router import looks_like_project_name, message_looks_like_filter_search
    for q in ["Show me villas in Pattaya", "Pokaż wille w Phuket do 15 mln THB", "Villas in Koh Samui under 10 million THB",
              "3 bedroom villas in Phuket under 25 million THB", "villas Pattaya", "villa in puket",
              "Pokaż nieruchomości w Kambodży", "Szukam czegoś z widokiem na morze w Phuket"]:
        assert not looks_like_project_name(q), q
    for q in ["Show me villas in Pattaya", "Pokaż wille w Phuket do 15 mln THB", "Villas in Koh Samui under 10 million THB"]:
        assert message_looks_like_filter_search(q), q


def test_yield_and_roi_percentages_from_marketing_text_are_removed():
    from app.services.listing_presentation import sanitize_marketing_claims as m
    for t in ["The area offers rental yields of 6-9% for investors.", "a total ROI projection of 9% over five years",
              "Expect 7.5% rental yield annually"]:
        assert "%" not in m(t), t
    assert "35,744 THB/sqm" in m("Competitive entry at 35,744 THB/sqm")
    assert "5% interest" in m("Loan at 5% interest, 30% deposit")


def test_unknown_place_is_not_silently_replaced_by_another_search(monkeypatch):
    import app.services.intent_router as ir
    monkeypatch.setattr(fs, "fetch_properties_from_firestore", lambda **k: _cards())
    monkeypatch.setitem(ir._PLACE_BLOB, "text", None)
    assert ir.extract_search_filters_deterministic("Show me a villa in Antarctica").get("foreign_location") == "Antarctica"
    for ok in ["Show me villas in Phuket", "Show me condos in Phnom Penh", "Show me villas in Thailand"]:
        assert not ir.extract_search_filters_deterministic(ok).get("foreign_location"), ok
    ir._PLACE_BLOB.update({"at": 0.0, "text": None})


def test_conversion_placeholders_are_removed_but_real_conversions_stay():
    from app.services.reply_validator import sanitize_listing_line_text as sl
    assert "confirm" not in sl("from 4.90M THB (~€confirm with advisor / ~PLN confirm with advisor)")
    assert "available" not in sl("from 4.90M THB (~€ available on request / ~PLN available on request)")
    assert "(~€128,947 / ~576,470 PLN)" in sl("from 4.90M THB (~€128,947 / ~576,470 PLN)")


def test_search_reply_is_deterministic_by_default():
    from app.core.config import settings
    assert settings.SEARCH_REPLY_USE_LLM is False
