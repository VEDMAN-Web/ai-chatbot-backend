"""
TASK 4: Multi-turn drift — no assistant prose to the model; replies stay grounded.
Uses fake Firestore/CSV snapshot + a hallucinating fake model.
"""
from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.grounding import build_grounded_llm_messages, build_facts_block
from app.services.property_name_index import get_property_name_index, set_last_name_search_meta
from app.services.reply_validator import verify_claims

FIXTURE = Path(__file__).parent / "fixtures" / "listings_snapshot_2026-10-16.csv"


@pytest.fixture(scope="module")
def csv_rows():
    assert FIXTURE.is_file()
    with FIXTURE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def name_index(csv_rows):
    idx = get_property_name_index()
    idx.clear()
    idx.load_from_rows(csv_rows)
    set_last_name_search_meta(None, [])
    yield idx
    set_last_name_search_meta(None, [])


_HALLUCINATION_SNIPPETS = (
    "secret spa",
    "8% rental yield",
    "zorblax towers",
    "freehold for foreigners",
    "completion q2 2027",
    "assistant_prose",
)


def _hallucinating_reply(turn: int, cards: list) -> str:
    name = (cards[0].get("name") if cards else "Unknown") or "Unknown"
    return (
        f"{name} is great. Spa: Yes. It offers 8% rental yield. "
        f"Completion Q2 2027. Also see Zorblax Towers — freehold for foreigners. "
        f"ASSISTANT_PROSE_{turn}"
    )


def test_eight_turn_script_no_prose_to_model_and_claims_stripped(name_index, monkeypatch):
    """
    Scripted conversation; fake model hallucinates on turns 3–8.
    Assert: model inputs never contain earlier assistant prose;
    final replies never contain hallucination snippets.
    """
    catalogue = [
        {
            "property_id": "phuket-v1",
            "name": "Phuket Test Villa One",
            "location": "Phuket",
            "price": "from 15,000,000 THB",
            "price_numeric_min": 15_000_000,
            "property_type": "Villa",
            "bedrooms": "3 Beds",
            "has_pool": True,
            "amenities": ["Pool", "Parking"],
            "ownership": "Leasehold",
            "status": "under_construction",
        },
        {
            "property_id": "phuket-v2",
            "name": "Phuket Test Villa Two",
            "location": "Phuket",
            "price": "from 18,000,000 THB",
            "price_numeric_min": 18_000_000,
            "property_type": "Villa",
            "bedrooms": "4 Beds",
            "has_pool": True,
            "amenities": ["Pool"],
            "ownership": "Leasehold",
        },
        {
            "property_id": "samui-c1",
            "name": "Samui Test Condo One",
            "location": "Koh Samui",
            "price": "from 8,000,000 THB",
            "price_numeric_min": 8_000_000,
            "property_type": "Condominium",
            "bedrooms": "2 Beds",
            "amenities": ["Gym"],
            "ownership": "Foreign Freehold",
        },
        {
            "property_id": "samui-c2",
            "name": "Samui Test Condo Two",
            "location": "Koh Samui",
            "price": "from 9,500,000 THB",
            "price_numeric_min": 9_500_000,
            "property_type": "Condominium",
            "bedrooms": "2 Beds",
            "amenities": ["Pool"],
            "ownership": "Foreign Freehold",
        },
    ]

    turns = [
        "Show me villas in Phuket",
        "Tell me about the first one",
        "Does it have a pool?",
        "Where is it located?",
        "Show me condos in Samui",
        "Tell me about the second result",
        "What is the rental yield?",
        "Is it freehold?",
    ]

    conv = SimpleNamespace(
        id="mt-1",
        session_id="mt-sess",
        site_origin="tajlandia.com",
        preferences={"language": "en"},
        message_history=[],
    )

    model_inputs: list = []

    # Drive via structured state + claim verifier on hallucinated text
    from app.services.intent_router import detect_deterministic_intent, IntentType

    for i, msg in enumerate(turns):
        # Record what would be sent to the model
        msgs = build_grounded_llm_messages(
            system_prompt="SYS",
            conversation=conv,
            user_message=msg,
            language="en",
            facts_block=build_facts_block(
                [
                    c for c in catalogue
                    if str(c["property_id"]) in set(conv.preferences.get("last_result_ids") or [])
                ] or catalogue[:2],
                language="en",
            ),
        )
        model_inputs.append(msgs)
        blob = "\n".join(m["content"] for m in msgs)
        for snip in _HALLUCINATION_SNIPPETS:
            assert snip not in blob.lower(), f"turn {i+1}: assistant prose leaked: {snip}"

        intent, args = detect_deterministic_intent(msg, conv.preferences)
        cards = []
        if intent == IntentType.PROPERTY_SEARCH or "show me" in msg.lower():
            # Simulate search results
            if "samui" in msg.lower():
                cards = [catalogue[2], catalogue[3]]
            else:
                cards = [catalogue[0], catalogue[1]]
            conv.preferences["last_result_ids"] = [c["property_id"] for c in cards]
            conv.preferences["seen_property_ids"] = list(
                set(conv.preferences.get("seen_property_ids") or []) | {c["property_id"] for c in cards}
            )
            conv.preferences["active_filters"] = {
                "location": "Koh Samui" if "samui" in msg.lower() else "Phuket",
                "property_type": "Condominium" if "condo" in msg.lower() else "Villa",
            }
            from app.services.property_context_service import (
                load_property_context, set_active_property, touch_discussed, save_property_context,
            )
            pctx = load_property_context(conv)
            names = {c["property_id"]: c["name"] for c in cards}
            pctx = touch_discussed(pctx, [c["property_id"] for c in cards], names, refresh_ordinals=True)
            pctx = set_active_property(pctx, cards[0]["property_id"], reference=cards[0]["name"])
            conv.preferences.update(save_property_context(conv, pctx))
            # Deterministic grounded reply (search path)
            reply = f"Here are verified options including {cards[0]['name']}."
        else:
            # Details / follow-up — hallucinating model output, then verify
            active_id = (conv.preferences.get("property_context") or {}).get("active_property_id")
            if "second" in msg.lower():
                ids = conv.preferences.get("last_result_ids") or []
                active_id = ids[1] if len(ids) > 1 else active_id
            card = next((c for c in catalogue if c["property_id"] == active_id), catalogue[0])
            cards = [card]
            if i + 1 >= 3:
                raw = _hallucinating_reply(i + 1, cards)
            else:
                raw = f"{card['name']} in {card['location']}. Price {card['price']}."
            facts = build_facts_block([card], active_card=card, language="en")
            ok, reasons = verify_claims(
                reply_text=raw,
                cards=cards,
                facts_text=facts,
                allowed_property_ids={card["property_id"]},
                allowed_property_names={card["name"].lower()},
            )
            if not ok:
                # Deterministic fallback from FACTS
                reply = (
                    f"{card['name']} — {card['location']}. {card['price']}. "
                    f"Bedrooms: {card['bedrooms']}. "
                    f"Ownership: {card.get('ownership') or 'not in our verified data'}."
                )
                if "yield" in msg.lower():
                    reply = (
                        f"Rental yield for **{card['name']}** is not in our verified data. "
                        f"Our advisory team can confirm developer figures."
                    )
                if "freehold" in msg.lower():
                    own = card.get("ownership") or ""
                    if "freehold" in own.lower():
                        reply = f"**{card['name']}** ownership on record: {own}."
                    else:
                        reply = (
                            f"Foreign freehold is not confirmed in verified data for **{card['name']}** "
                            f"(recorded: {own or 'not documented'})."
                        )
            else:
                reply = raw

            # Keep active
            from app.services.property_context_service import (
                load_property_context, set_active_property, save_property_context,
            )
            pctx = load_property_context(conv)
            pctx = set_active_property(pctx, card["property_id"], reference=card["name"])
            conv.preferences.update(save_property_context(conv, pctx))

        low = reply.lower()
        for snip in ("zorblax", "8% rental", "spa: yes", "completion q2 2027", "assistant_prose"):
            assert snip not in low, f"turn {i+1} reply still has {snip}: {reply}"

        # Append turn to history (as production would)
        conv.message_history = list(conv.message_history) + [
            {"role": "user", "content": msg},
            {"role": "assistant", "content": reply},
        ]

    assert len(model_inputs) == 8
    # After 6+ turns still only user history in grounded messages
    last_blob = "\n".join(m["content"] for m in model_inputs[-1])
    assert "ASSISTANT_PROSE" not in last_blob
    user_roles = [m for m in model_inputs[-1] if m["role"] == "user"]
    assert len(user_roles) <= 4
