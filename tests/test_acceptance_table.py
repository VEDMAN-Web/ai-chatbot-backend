"""
Acceptance table checks (fake data / no live Firestore writes).
"""

from __future__ import annotations

import concurrent.futures

from app.api.v1.chat import PublicChatRequest
from app.core.tenant import resolve_tenant
from app.services.i18n_service import build_no_match_reply
from app.services.intent_router import IntentType, detect_deterministic_intent, extract_search_filters_deterministic
from app.services.listing_presentation import extract_ownership_from_property
from app.services.property_name_index import PropertyNameIndex, get_last_name_search_suggestions


def _idx() -> PropertyNameIndex:
    idx = PropertyNameIndex()
    idx.load_from_rows(
        [
            {
                "doc_id": "anava-samui",
                "name": {"en": "Anava Samui", "pl": "Anava Samui"},
                "slug": {"en": "anava-samui"},
                "cityId": "samui",
            },
            {
                "doc_id": "satoshi-paradise",
                "name": {"en": "Satoshi's Paradise"},
                "slug": {"en": "satoshis-paradise"},
            },
        ]
    )
    return idx


def test_acceptance_1_anava():
    mr = _idx().match("Anava")
    assert mr.confident and mr.confident.doc_id == "anava-samui"


def test_acceptance_2_satoshi_did_you_mean():
    mr = _idx().match("Satoshi Residence")
    assert mr.confident is None
    names = [c.display_name for c in mr.candidates]
    assert any("Paradise" in n or "Satoshi" in n for n in names)
    tips = get_last_name_search_suggestions()
    assert tips


def test_acceptance_3_polish_phuket_villas_budget():
    intent, args = detect_deterministic_intent("Pokaż wille w Phuket do 15 mln THB", {})
    assert intent == IntentType.PROPERTY_SEARCH
    assert args.get("location") == "Phuket"
    assert args.get("property_type") == "Villa"
    assert args.get("max_price") == 15_000_000


def test_acceptance_4_tajlandia_branding():
    req = PublicChatRequest(session_id="s", message="hi", site_origin="tajlandia.com")
    assert req.site_origin == "tajlandia.com"
    t = resolve_tenant(req.site_origin)
    assert "tajlandia.com" in t.advisor_email
    assert "varsoviaestate.com" not in t.advisor_email
    text = build_no_match_reply("pl", site_origin="tajlandia.com", criteria_summary="Phuket")
    assert "advisor@tajlandia.com" in text
    assert "advisor@varsoviaestate.com" not in text


def test_acceptance_5_impossible_filters():
    f = extract_search_filters_deterministic("50-bedroom villa in Phuket under 1M THB")
    assert f.get("bedrooms") == 50
    assert f.get("max_price") == 1_000_000
    text = build_no_match_reply(
        "en",
        site_origin="tajlandia.com",
        criteria_summary="Phuket, Villa, 50 bed, budget <= 1000000",
    )
    assert "No verified listing matches" in text
    assert "Foreign Freehold / Leasehold" not in text


def test_acceptance_6_no_invented_ownership():
    assert extract_ownership_from_property({"name": "X", "description": "Nice villa"}) is None
    assert extract_ownership_from_property(
        {"ownership": "Foreign Freehold / Leasehold available"}
    ) is None


def test_acceptance_7_parallel_suggestions_isolated():
    idx = _idx()
    idx.load_from_rows(
        [
            {"doc_id": "anava-samui", "name": {"en": "Anava Samui"}, "slug": {"en": "anava-samui"}},
            {"doc_id": "satoshi-paradise", "name": {"en": "Satoshi's Paradise"}, "slug": {"en": "satoshi"}},
            {"doc_id": "zorblax", "name": {"en": "Zorblax Towers"}, "slug": {"en": "zorblax"}},
        ]
    )

    def run(q: str, forbidden: str):
        idx.match(q)
        tips = get_last_name_search_suggestions()
        assert not any(forbidden.lower() in t.lower() for t in tips)
        return tips

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(run, "Satoshi Residence", "Zorblax")
        f2 = ex.submit(run, "Zorblax Unknown", "Satoshi")
        t1 = f1.result()
        t2 = f2.result()
    assert not any("Zorblax" in x for x in t1)
    assert not any("Satoshi" in x for x in t2)


def test_acceptance_8_mi_anava():
    mr = _idx().match("mi Anava")
    assert mr.confident and "anava" in mr.confident.doc_id
