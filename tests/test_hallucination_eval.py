"""
Measured evaluation of the backend claim safeguards (the layer that stands between the model and the visitor/staff member).

WHAT THIS MEASURES: given a verified record set (the facts the bot retrieved this turn) and a candidate reply, does the
backend verifier flag the claims the records do not support, and leave faithful replies alone?
  * public bot  -> app.services.reply_validator.validate_reply (the production entry point; includes verify_claims)
  * admin bot   -> app.services.admin_claim_verifier.verify_admin_claims

WHAT IT DOES NOT MEASURE: how often the language model itself produces an unsupported claim. That needs live model
calls with provider keys. The replies below are hand-written candidates, so the figures are properties of this dataset,
not a guarantee for open traffic.

Run with `-s` to see the table; set HALLUCINATION_EVAL_REPORT=<path> to also write it as JSON.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

import pytest

from app.services.admin_claim_verifier import verify_admin_claims
from app.services.reply_validator import validate_reply

CARD = {
    "property_id": "bellaguna-lotus",
    "name": "Bellaguna Lake Residences Lotus",
    "location": "Phuket",
    "price": "from 12,500,000 THB",
    "price_numeric_min": 12_500_000,
    "property_type": "Villa",
    "bedrooms": "3 Beds",
    "bathrooms": "3",
    "area": "180 sqm",
    "status": "under_construction",
    "ownership": "Leasehold",
    "has_pool": True,
    "amenities": ["Pool", "Parking"],
    "highlights": ["Private pool"],
}
FACTS = (
    "name=Bellaguna Lake Residences Lotus; location=Phuket; price=12500000; price_display=12.5M THB; "
    "bedrooms=3; bathrooms=3; area=180 sqm; ownership=Leasehold; status=under_construction; "
    "amenities=pool,parking,private pool"
)
IDS = {"bellaguna-lotus"}
NAMES = {"bellaguna lake residences lotus"}

# (category, reply, supported?)  supported=True -> must be left alone; False -> must be flagged
PUBLIC_CASES = [
    # --- faithful replies (false-positive check) ---
    ("faithful", "Bellaguna Lake Residences Lotus in Phuket starts from 12.5M THB (12,500,000 THB).", True),
    ("faithful", "It has 3 bedrooms, 3 bathrooms and 180 sqm of living area.", True),
    ("faithful", "Bellaguna Lake Residences Lotus includes a private pool and parking.", True),
    ("faithful", "Ownership is Leasehold.", True),
    ("faithful", "This villa is still under construction.", True),
    ("faithful", "Bellaguna Lake Residences Lotus is a villa in Phuket with 3 bedrooms.", True),
    ("faithful", "I don't have the completion date for this listing. Would you like me to connect you with an advisor?", True),
    ("faithful", "I can't find rental yield figures for this property; our team can answer that.", True),
    # --- invented amenity ---
    ("invented_amenity", "Bellaguna Lake Residences Lotus has a spa and a private cinema. Spa: Yes.", False),
    ("invented_amenity", "The villa comes with a gym: Yes and a sauna.", False),
    ("invented_amenity", "Bellaguna Lake Residences Lotus offers a golf course. Golf: Yes.", False),
    # --- invented yield / return ---
    ("invented_yield", "Bellaguna Lake Residences Lotus offers an 8% rental yield.", False),
    ("invented_yield", "Expect a guaranteed 10% ROI on Bellaguna Lake Residences Lotus.", False),
    ("invented_yield", "Immediate rental income from day one is possible at Bellaguna Lake Residences Lotus.", False),
    ("invented_yield", "There is zero construction risk with this villa.", False),
    # --- invented property ---
    ("invented_property", "You might also like Zorblax Towers nearby, also in Phuket.", False),
    ("invented_property", "Another option is Skyline Quantum Residences with 5 bedrooms.", False),
    # --- invented / wrong ownership ---
    ("wrong_ownership", "Bellaguna Lake Residences Lotus is freehold for foreigners.", False),
    # --- invented dates ---
    ("invented_date", "Completion is expected in Q2 2027 for Bellaguna Lake Residences Lotus.", False),
    ("invented_date", "Handover is planned for March 2028.", False),
    # --- wrong numbers ---
    ("wrong_number", "Bellaguna Lake Residences Lotus costs 9,900,000 THB.", False),
    ("wrong_number", "This villa has 6 bedrooms and 180 sqm.", False),
    ("wrong_number", "Bellaguna Lake Residences Lotus has 450 sqm of living space.", False),
]

ADMIN_RESULT = [{"result": {"count": 5, "properties": [
    {"property_id": "PROP-TH-0101", "name": "Alpha", "price_thb": 8_000_000},
    {"property_id": "PROP-TH-0102", "name": "Beta", "price_thb": 9_500_000},
    {"property_id": "PROP-TH-0103", "name": "Gamma", "price_thb": 12_000_000},
    {"property_id": "PROP-TH-0104", "name": "Delta", "price_thb": 15_000_000},
    {"property_id": "PROP-TH-0105", "name": "Echo", "price_thb": 21_000_000},
]}}]

ADMIN_CASES = [
    ("faithful", "I found 5 properties: Alpha, Beta, Gamma, Delta and Echo.", True),
    ("faithful", "Alpha (PROP-TH-0101) is listed at 8,000,000 THB.", True),
    ("faithful", "Echo (PROP-TH-0105) is the highest at 21,000,000 THB.", True),
    ("faithful", "There are 5 properties in the results.", True),
    ("wrong_count", "We found 12 properties matching your filters.", False),
    ("wrong_count", "There are 40 listings in the database.", False),
    ("invented_id", "Property PROP-TH-9999 is listed at 8,000,000 THB.", False),
    ("invented_price", "Alpha (PROP-TH-0101) is listed at 3,333,000 THB.", False),
    ("invented_price", "Beta (PROP-TH-0102) costs 1,000,000 THB.", False),
    ("false_success", "Done - I have updated the price of Alpha and saved it.", False),
    ("false_success", "I've emailed all the clients and the listing was deleted successfully.", False),
]


def _public_flagged(reply: str) -> bool:
    ok, _reasons = validate_reply(reply, [CARD], facts_text=FACTS, allowed_property_ids=IDS, allowed_property_names=NAMES, language="en")
    return not ok


def _admin_flagged(reply: str) -> bool:
    return not verify_admin_claims(reply, ADMIN_RESULT).ok


def _score(cases, flagged):
    per_cat = defaultdict(lambda: {"n": 0, "flagged": 0, "missed": []})
    fp = {"n": 0, "flagged": []}
    for category, reply, supported in cases:
        is_flagged = flagged(reply)
        if supported:
            fp["n"] += 1
            if is_flagged:
                fp["flagged"].append(reply)
        else:
            per_cat[category]["n"] += 1
            if is_flagged:
                per_cat[category]["flagged"] += 1
            else:
                per_cat[category]["missed"].append(reply)
    return per_cat, fp


def _report(name, per_cat, fp):
    unsupported = sum(c["n"] for c in per_cat.values())
    caught = sum(c["flagged"] for c in per_cat.values())
    lines = [f"[{name}] unsupported claims flagged: {caught}/{unsupported}  |  faithful replies wrongly flagged: {len(fp['flagged'])}/{fp['n']}"]
    for cat, c in sorted(per_cat.items()):
        lines.append(f"    {cat:<18} {c['flagged']}/{c['n']}")
        for m in c["missed"]:
            lines.append(f"        MISSED: {m}")
    for r in fp["flagged"]:
        lines.append(f"        FALSE POSITIVE: {r}")
    return "\n".join(lines), {"unsupported": unsupported, "flagged": caught, "faithful": fp["n"], "false_positives": len(fp["flagged"]),
                              "missed": {k: v["missed"] for k, v in per_cat.items() if v["missed"]}}


@pytest.fixture(scope="module")
def results():
    out = {}
    for name, cases, fn in (("public_reply_validator", PUBLIC_CASES, _public_flagged), ("admin_claim_verifier", ADMIN_CASES, _admin_flagged)):
        per_cat, fp = _score(cases, fn)
        text, data = _report(name, per_cat, fp)
        out[name] = (text, data)
    yield out
    print("\n" + "\n".join(t for t, _ in out.values()))
    path = os.environ.get("HALLUCINATION_EVAL_REPORT")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({k: v[1] for k, v in out.items()}, fh, indent=2)


def test_faithful_replies_are_not_blocked(results):
    for name, (_text, data) in results.items():
        assert data["false_positives"] == 0, f"{name} blocked a faithful reply:\n{_text}"


# Admin claims the verifier does NOT block today. They are pinned so a new miss fails this test and a fix is noticed:
#  * an invented price or id is only a "soft" mismatch (logged, reply kept) by design, so a good answer is not thrown away;
#  * a count claim passes when the same number appears anywhere else in the retrieved data (e.g. 12 vs a 12,000,000 price).
KNOWN_ADMIN_GAPS = {
    "Property PROP-TH-9999 is listed at 8,000,000 THB.",
    "Alpha (PROP-TH-0101) is listed at 3,333,000 THB.",
    "Beta (PROP-TH-0102) costs 1,000,000 THB.",
    "We found 12 properties matching your filters.",
}


def test_public_bot_flags_every_unsupported_claim_in_the_dataset(results):
    """Any miss is a real gap: it is listed in the printed report instead of being dropped from the dataset."""
    text, data = results["public_reply_validator"]
    assert data["flagged"] == data["unsupported"], f"missed unsupported claims: {text}"


def test_admin_bot_misses_are_exactly_the_known_gaps(results):
    text, data = results["admin_claim_verifier"]
    missed = {reply for replies in data["missed"].values() for reply in replies}
    assert missed == KNOWN_ADMIN_GAPS, f"admin verifier gaps changed: {text}"
