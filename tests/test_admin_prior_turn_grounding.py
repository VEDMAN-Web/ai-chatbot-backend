"""
Regression: BUG 1 — counts grounded by an EARLIER turn's tool result must not be
flagged as invented when the reply restates them in a later turn.
"""

from unittest.mock import MagicMock

from app.services.admin_claim_verifier import (
    verify_admin_claims,
    verify_and_correct_admin_reply,
)

TURN1_INVENTORY = [{
    "tool": "get_database_inventory_stats",
    "result": {
        "authoritative_total_properties": 1046,
        "by_status": {"available": 812, "sold_out": 234},
        "properties": [{"property_id": f"PROP-PHU-{i}", "name": f"Listing {i}"} for i in range(6)],
    },
}]

HISTORY = [
    {"role": "user", "content": "How many properties do we have?"},
    {"role": "assistant", "content": "We have 1,046 properties.", "tool_results": TURN1_INVENTORY},
    {"role": "user", "content": "Show villas in Rawai"},
]

TURN2_TOOLS = [{
    "tool": "search_properties",
    "result": {"count": 3, "properties": [
        {"property_id": "PROP-PHU-900", "name": "Rawai Villa A", "price_display": "9 million THB"},
        {"property_id": "PROP-PHU-901", "name": "Rawai Villa B", "price_display": "11 million THB"},
        {"property_id": "PROP-PHU-902", "name": "Rawai Villa C", "price_display": "Price on request"},
    ]},
}]


def test_restated_prior_count_is_ok_with_history():
    reply = "Out of 1,046 properties in the database, I found 3 villas in Rawai."
    res = verify_and_correct_admin_reply(
        reply, TURN2_TOOLS, db=MagicMock(), conversation_history=HISTORY
    )
    assert res.ok is True
    assert res.final_text == reply
    assert not any(h.startswith("wrong_count") for h in res.hard_mismatches)


def test_restated_prior_count_ok_with_explicit_prior_tool_results():
    reply = "We have 1046 properties in total; 3 match Rawai."
    v = verify_admin_claims(reply, TURN2_TOOLS, prior_tool_results=TURN1_INVENTORY)
    assert v.ok is True


def test_without_history_count_is_still_hard():
    reply = "Out of 1,046 properties in the database, I found 3 villas in Rawai."
    v = verify_admin_claims(reply, TURN2_TOOLS)
    assert v.ok is False
    assert any(h.startswith("wrong_count") for h in v.hard_mismatches)


def test_count_matching_neither_turn_is_still_hard():
    reply = "Out of 1,200 properties in the database, I found 3 villas in Rawai."
    res = verify_and_correct_admin_reply(
        reply, TURN2_TOOLS, db=MagicMock(), conversation_history=HISTORY, regenerate_fn=None
    )
    assert res.ok is False
    assert any(h.startswith("wrong_count") for h in res.hard_mismatches)
    assert res.intervention == "templated"


def test_truncated_prior_list_length_is_not_a_count():
    # Stored history lists are truncated to 6 items — "6 properties" is not grounded by that.
    tools = [{"tool": "search_properties", "result": {"count": 2, "properties": [
        {"property_id": "PROP-X-1", "name": "Alpha Rawai"},
        {"property_id": "PROP-X-2", "name": "Beta Rawai"},
    ]}}]
    reply = "I found 6 properties in Rawai."
    assert verify_admin_claims(reply, tools).ok is False  # sanity: not grounded this turn
    v = verify_admin_claims(reply, tools, prior_tool_results=TURN1_INVENTORY)
    assert v.ok is False


def test_prior_turn_does_not_excuse_wrong_change_price():
    tools = [{
        "tool": "propose_update",
        "result": {
            "pending_action_id": "pa-1",
            "property_id": "PROP-PHU-900",
            "requires_confirmation": True,
            "new_price": "9500000",
        },
    }]
    prior = [{"tool": "get_property", "result": {"property_id": "PROP-PHU-900", "price": "12 million THB"}}]
    reply = "I prepared the change for PROP-PHU-900: new price 12 million THB."
    v = verify_admin_claims(reply, tools, prior_tool_results=prior)
    assert v.ok is False
    assert any(h.startswith("wrong_change_price") for h in v.hard_mismatches)


def test_regen_instruction_does_not_disown_earlier_figures():
    captured = {}

    def regen(instr):
        captured["instr"] = instr
        return None

    verify_and_correct_admin_reply(
        "I found 1,200 properties.", TURN2_TOOLS, db=MagicMock(), regenerate_fn=regen
    )
    instr = captured["instr"]
    assert "earlier" in instr.lower()
    assert "do not disown" in instr.lower()
    assert "Do not invent counts" not in instr
