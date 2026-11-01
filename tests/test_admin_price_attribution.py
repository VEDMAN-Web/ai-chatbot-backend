"""
Regression: BUG 3 — "Wrong price assigned across properties".

The claim verifier used to check only that a price number appeared *somewhere*
in this turn's tool payload, so a reply that moved Elite Chalong's 19.98M onto
Ozone Campus ("Price on request") passed. Prices must now be bound to the
property they are stated next to.
"""

from unittest.mock import MagicMock

from app.services.admin_claim_verifier import (
    detect_price_misattribution,
    split_reply_by_property,
    build_property_price_index,
    verify_admin_claims,
    verify_and_correct_admin_reply,
)


def _prop(pid, name, price_display, price_min=None, price_max=None, **extra):
    d = {
        "property_id": pid,
        "property_name": name,
        "name": name,
        "status": "Available",
        "location": "Phuket",
        "price_display": price_display,
        "price": price_display,
        "price_numeric_min": price_min,
        "bedrooms": "2-3",
    }
    if price_max is not None:
        d["price_numeric_max"] = price_max
    d.update(extra)
    return d


def _search(*props):
    return [{"tool": "search_properties", "result": {"count": len(props), "properties": list(props)}}]


ELITE = _prop("PROP-PHU-101", "Elite Chalong", "19.98 million THB", 19_980_000)
OZONE = _prop("PROP-PHU-202", "Ozone Campus", "Price on request", None)
TOOLS = _search(ELITE, OZONE)


# ---------------------------------------------------------------------------
# The reported swap
# ---------------------------------------------------------------------------

def test_swapped_price_is_hard_mismatch_bullets():
    reply = (
        "I found 2 properties:\n"
        "1. **Elite Chalong** — Phuket\n"
        "   - Price: Price on request\n"
        "2. **Ozone Campus** — Phuket\n"
        "   - Price: 19.98M THB\n"
    )
    v = verify_admin_claims(reply, TOOLS)
    assert v.ok is False
    assert any(h.startswith("wrong_property_price") and "Ozone Campus" in h for h in v.hard_mismatches)


def test_swapped_price_is_hard_mismatch_inline():
    reply = "Ozone Campus is priced at 19.98 million THB, while Elite Chalong is price on request."
    v = verify_admin_claims(reply, TOOLS)
    assert v.ok is False
    assert any("wrong_property_price" in h for h in v.hard_mismatches)


def test_swap_between_two_priced_properties_flagged():
    a = _prop("PROP-PHU-1", "Elite Chalong", "19.98 million THB", 19_980_000)
    b = _prop("PROP-PHU-2", "Laguna Park", "8.5 million THB", 8_500_000)
    reply = "- **Elite Chalong**: 8.5M THB\n- **Laguna Park**: 19.98M THB"
    v = verify_admin_claims(reply, _search(a, b))
    assert v.ok is False
    assert len([h for h in v.hard_mismatches if h.startswith("wrong_property_price")]) == 2


def test_swap_routes_to_template_with_correct_prices():
    reply = "**Ozone Campus** — 19.98M THB\n**Elite Chalong** — price on request"
    res = verify_and_correct_admin_reply(reply, TOOLS, db=MagicMock(), regenerate_fn=None)
    assert res.intervention == "templated"
    lines = res.final_text.splitlines()
    elite_line = next(ln for ln in lines if "Elite Chalong" in ln)
    ozone_line = next(ln for ln in lines if "Ozone Campus" in ln)
    assert "19.98" in elite_line
    assert "19.98" not in ozone_line


def test_regenerated_correct_reply_is_accepted():
    bad = "**Ozone Campus** — 19.98M THB\n**Elite Chalong** — price on request"
    good = "**Elite Chalong** — 19.98M THB\n**Ozone Campus** — price on request"
    seen = {}

    def regen(instr):
        seen["instr"] = instr
        return good

    res = verify_and_correct_admin_reply(bad, TOOLS, db=MagicMock(), regenerate_fn=regen)
    assert res.intervention == "regenerated"
    assert res.final_text == good
    assert "property it belongs to" in seen["instr"]


# ---------------------------------------------------------------------------
# Correct attributions pass untouched
# ---------------------------------------------------------------------------

def test_correct_attribution_passes_untouched():
    reply = (
        "I found 2 properties:\n"
        "1. **Elite Chalong** — Phuket\n"
        "   - Price: 19.98M THB\n"
        "2. **Ozone Campus** — Phuket\n"
        "   - Price: Price on request\n"
    )
    v = verify_admin_claims(reply, TOOLS)
    assert v.ok is True
    assert not v.hard_mismatches
    res = verify_and_correct_admin_reply(reply, TOOLS, db=MagicMock())
    assert res.final_text == reply
    assert res.intervention in ("none", "soft_logged")


def test_same_price_on_two_properties_is_fine():
    a = _prop("PROP-PHU-1", "Elite Chalong", "12.5 million THB", 12_500_000)
    b = _prop("PROP-PHU-2", "Laguna Park", "12.5 million THB", 12_500_000)
    reply = "- **Elite Chalong**: 12.5M THB\n- **Laguna Park**: 12.5M THB"
    assert verify_admin_claims(reply, _search(a, b)).ok is True


def test_price_range_property():
    a = _prop("PROP-PHU-1", "Skyline Rawai", "5.2 - 12 million THB", 5_200_000, 12_000_000)
    b = _prop("PROP-PHU-2", "Laguna Park", "25 million THB", 25_000_000)
    tools = _search(a, b)
    ok_reply = "- **Skyline Rawai**: from 5.2M to 12M THB\n- **Laguna Park**: 25M THB"
    assert verify_admin_claims(ok_reply, tools).ok is True
    ok_dash = "**Skyline Rawai** — 5.2–12 mln THB"
    assert verify_admin_claims(ok_dash, tools).ok is True
    # Laguna's price moved onto the range property → swap
    bad = "- **Skyline Rawai**: 25M THB\n- **Laguna Park**: 5.2M THB"
    v = verify_admin_claims(bad, tools)
    assert v.ok is False


def test_formats_m_million_mln_comma_full_thb():
    replies = [
        "**Elite Chalong** — 19.98M",
        "**Elite Chalong** — 19.98 million THB",
        "**Elite Chalong** — 19,98 mln THB",
        "**Elite Chalong** — ฿19,980,000",
        "**Elite Chalong** — 19,980,000 THB",
        "**Elite Chalong** — about 20M THB",
    ]
    for r in replies:
        assert verify_admin_claims(r, TOOLS).ok is True, r
    swapped = [
        "**Ozone Campus** — 19.98M",
        "**Ozone Campus** — 19.98 million THB",
        "**Ozone Campus** — 19,98 mln THB",
        "**Ozone Campus** — ฿19,980,000",
        "**Ozone Campus** — 19,980,000 THB",
    ]
    for r in swapped:
        assert verify_admin_claims(r, TOOLS).ok is False, r


def test_polish_reply_swap_flagged():
    reply = "**Ozone Campus** — cena: 19,98 mln THB\n**Elite Chalong** — cena na zapytanie"
    v = verify_admin_claims(reply, TOOLS)
    assert v.ok is False


# ---------------------------------------------------------------------------
# False-positive guards
# ---------------------------------------------------------------------------

def test_unbound_segment_keeps_global_check():
    # No property named on the price line → old global behaviour (number is in payload).
    reply = "Prices in this search go up to 19.98M THB.\n\nOzone Campus: price on request."
    assert verify_admin_claims(reply, TOOLS).ok is True


def test_comparison_sentence_not_flagged():
    reply = "Ozone Campus has no published price, unlike Elite Chalong at 19.98M THB."
    assert verify_admin_claims(reply, TOOLS).ok is True
    reply2 = "Laguna Park is 11.48M cheaper than Elite Chalong."
    b = _prop("PROP-PHU-2", "Laguna Park", "8.5 million THB", 8_500_000)
    assert verify_admin_claims(reply2, _search(ELITE, b)).ok is True


def test_area_and_distance_not_treated_as_price():
    o = _prop("PROP-PHU-202", "Ozone Campus", "Price on request", None, area_display="120 sqm")
    reply = "**Ozone Campus** — 120 sqm, 50 m from the beach, price on request"
    assert verify_admin_claims(reply, _search(ELITE, o)).ok is True


def test_price_from_own_description_allowed_for_price_on_request():
    o = _prop(
        "PROP-PHU-202", "Ozone Campus", "Price on request", None,
        highlights="Studios expected from 4.5M THB at launch",
    )
    reply = "**Ozone Campus** — price on request (studios expected from 4.5M THB)"
    assert verify_admin_claims(reply, _search(ELITE, o)).ok is True


def test_pending_price_change_on_property_not_flagged():
    tools = TOOLS + [{
        "tool": "propose_update",
        "result": {
            "pending_action_id": "pa-1",
            "property_id": "PROP-PHU-202",
            "requires_confirmation": True,
            "change_diff": {"property_id": "PROP-PHU-202", "before": "Price on request", "after": "15000000"},
        },
    }]
    reply = "I prepared a change: **Ozone Campus** price → 15M THB. Please confirm."
    v = verify_admin_claims(reply, tools)
    assert not any(h.startswith("wrong_property_price") for h in v.hard_mismatches)


def test_multi_property_heading_does_not_bind_following_bullets():
    idx = build_property_price_index(TOOLS)
    segs = split_reply_by_property("## Elite Chalong vs Ozone Campus\n- Elite: 19.98M", idx)
    assert segs[-1][1] is None
    assert detect_price_misattribution("## Elite Chalong vs Ozone Campus\n- Elite: 19.98M", TOOLS) == []


def test_property_id_mention_binds():
    reply = "`PROP-PHU-202` — 19.98M THB"
    v = verify_admin_claims(reply, TOOLS)
    assert v.ok is False
