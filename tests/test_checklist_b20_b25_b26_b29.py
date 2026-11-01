"""
Comprehensive tests for Go-Live Checklist items:
- B20: Price change preview row deduplication
- B25: Support "the listing ...", "the property ..." and ID prefixes in Chat
- B26: Bot context awareness after Undo
- B29: Generate slug property IDs instead of random prop-xxxx
"""

import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

from app.services.admin_change_diff import (
    build_change_diff_rows,
    format_single_diff_markdown,
)
from app.services.admin_property_resolver import (
    extract_property_name_phrase,
    resolve_admin_property_name,
)
from app.services.admin_turn_rules import resolve_write_target
from app.services.admin_reply_helpers import (
    remember_admin_session_fact,
    extract_session_state_from_conversation,
    build_admin_structured_state,
)
from app.services.action_service import generate_property_id


# ---------------------------------------------------------------------------
# Test B20: Change Preview Lists Price Twice
# ---------------------------------------------------------------------------
def test_b20_price_diff_row_deduplication():
    current = {
        "property_id": "test-villa-b20",
        "price_numeric_min": 12000000,
        "price_display": "12,000,000 THB",
        "_readable": {"price_numeric_min": True, "price_display": True},
    }
    # When proposed fields include both numeric and display price
    proposed = {
        "price_numeric_min": 18000000,
        "price_display": "18,000,000 THB",
    }

    rows = build_change_diff_rows(current, proposed)
    # Ensure redundant price_display is dropped in favor of price_numeric_min
    fields = [r["field"] for r in rows]
    assert "price_numeric_min" in fields
    assert "price_display" not in fields
    assert len(rows) == 1

    md = format_single_diff_markdown("test-villa-b20", rows, "Test Villa B20")
    # Verify the table contains exactly one Price row
    assert md.count("| Price |") == 1
    assert "12,000,000 THB" in md
    assert "18,000,000 THB" in md


# ---------------------------------------------------------------------------
# Test B25: Support "the listing ...", "the property ..." and ID prefix in Chat
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("input_phrase,expected_name", [
    ("Change the price of the listing Botanica Four Seasons to 20 million", "Botanica Four Seasons"),
    ("Change the price of the listing prop-1234 (Botanica Four Seasons) to 20 million", "Botanica Four Seasons"),
    ("Change the price of the property Villa Aruna to 15 million", "Villa Aruna"),
    ("Change the price of Botanica Four Seasons to 20 million", "Botanica Four Seasons"),
    ("Update status of the listing Grand Ocean Villa to Ready to move in", "Grand Ocean Villa"),
])
def test_b25_phrase_extraction_strips_listing_prefix(input_phrase, expected_name):
    extracted = extract_property_name_phrase(input_phrase)
    assert extracted == expected_name


def test_b25_resolve_write_target_handles_prefixes_and_id_parens():
    mock_db = MagicMock()
    # Mocking rows in properties_store
    row = SimpleNamespace(property_id="botanica-four-seasons", property_name="Botanica Four Seasons")

    from unittest.mock import patch
    with patch("app.repositories.properties_store.list_rows", return_value=[row]):
        # Test "the listing <Name>"
        target, problem = resolve_write_target(mock_db, "the listing Botanica Four Seasons")
        assert problem is None
        assert target.property_name == "Botanica Four Seasons"

        # Test "the property <Name>"
        target, problem = resolve_write_target(mock_db, "the property Botanica Four Seasons")
        assert problem is None
        assert target.property_name == "Botanica Four Seasons"

        # Test "prop-id (Name)"
        target, problem = resolve_write_target(mock_db, "botanica-four-seasons (Botanica Four Seasons)")
        assert problem is None
        assert target.property_name == "Botanica Four Seasons"


# ---------------------------------------------------------------------------
# Test B26: Bot Context Awareness After Undo
# ---------------------------------------------------------------------------
def test_b26_undo_context_recorded_in_session():
    conv = SimpleNamespace(preferences={})
    undo_payload = {
        "operation_id": "crud_undo_test_999",
        "property_id": "villa-sunset-view",
        "property_name": "Villa Sunset View",
        "action_type": "update",
    }

    # Record undo event in conversation preferences
    remember_admin_session_fact(conv, last_undo=undo_payload)

    # Extract session state
    session_state = extract_session_state_from_conversation(conv)
    assert session_state.get("last_undo") == undo_payload

    # Build structured state block injected into the LLM context
    state_block = build_admin_structured_state(last_undo=session_state["last_undo"])
    assert "recent_undo_event:" in state_block
    assert "crud_undo_test_999" in state_block
    assert "Villa Sunset View" in state_block
    assert "prior change was undone/rolled back" in state_block


# ---------------------------------------------------------------------------
# Test B29: Generate Slug Property IDs (ZZ Test Villa -> zz-test-villa)
# ---------------------------------------------------------------------------
def test_b29_slug_property_id_generation():
    # Test slug generation from listing name
    pid1 = generate_property_id(None, "ZZ Test Villa")
    assert pid1 == "zz-test-villa"

    pid2 = generate_property_id(None, "Ocean View Residence Phase 2")
    assert pid2 == "ocean-view-residence-phase-2"

