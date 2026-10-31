"""Follow-ups on shown cards must resolve by name and ordinal, not data-unavailable."""
from types import SimpleNamespace

from app.services.intent_router import IntentType, detect_deterministic_intent
from app.services.property_context_service import (
    hydrate_names_from_history,
    load_property_context,
    resolve_name_against_context,
    resolve_property_reference,
    PropertyContext,
)


def _conversation_with_cards():
    cards = [
        {"property_id": "botanica-prestige", "name": "Botanica Prestige"},
        {"property_id": "fantasea-condo", "name": "Fantasea Condo"},
        {"property_id": "akara-pool-villas", "name": "Akara Pool Villas Phase 2"},
    ]
    return SimpleNamespace(
        id="conv-1",
        preferences={},
        message_history=[
            {"role": "user", "content": "Show new projects"},
            {
                "role": "assistant",
                "content": "Here are verified listings.",
                "structured_data": {"properties": cards, "property_ids": [c["property_id"] for c in cards]},
            },
        ],
    )


def test_hydrate_names_from_history_enables_fantasea_lookup():
    conv = _conversation_with_cards()
    ctx = hydrate_names_from_history(conv, PropertyContext())
    assert resolve_name_against_context("Fantasea Condo", ctx, conv) == "fantasea-condo"
    assert ctx.reference_map.get("first property") == "botanica-prestige"
    assert ctx.reference_map.get("second property") == "fantasea-condo"


def test_load_property_context_hydrates_even_when_active_already_set():
    conv = _conversation_with_cards()
    conv.preferences = {
        "last_result_ids": ["botanica-prestige", "fantasea-condo", "akara-pool-villas"],
        "property_context": {
            "active_property_id": "botanica-prestige",
            "recently_discussed_ids": ["botanica-prestige"],
            "reference_map": {"first property": "botanica-prestige"},
        },
    }
    ctx = load_property_context(conv)
    assert resolve_name_against_context("Fantasea Condo", ctx, conv) == "fantasea-condo"
    assert resolve_property_reference("tell me more about first properties", ctx).property_id == "botanica-prestige"
    assert resolve_property_reference("tell me more about Fantasea Condo", ctx).property_id == "fantasea-condo"


def test_intent_named_and_ordinal_followups():
    state = {
        "last_result_ids": ["botanica-prestige", "fantasea-condo", "akara-pool-villas"],
        "property_context": {
            "reference_map": {
                "first property": "botanica-prestige",
                "second property": "fantasea-condo",
                "Fantasea Condo": "fantasea-condo",
            }
        },
    }
    intent, args = detect_deterministic_intent("can you tell me more about Fantasea Condo ?", state)
    assert intent == IntentType.PROPERTY_DETAILS
    assert args.get("property_id") == "fantasea-condo"

    intent2, args2 = detect_deterministic_intent("tell me more about first properties", state)
    assert intent2 == IntentType.PROPERTY_DETAILS
    assert args2.get("property_id") == "botanica-prestige"

    intent3, args3 = detect_deterministic_intent("tell me more about first properties", {})
    assert intent3 == IntentType.PROPERTY_DETAILS
    assert args3.get("ordinal_index") == 0
