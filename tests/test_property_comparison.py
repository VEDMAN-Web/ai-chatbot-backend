"""Side-by-side comparison of 2-3 listings: built from stored fields, no invented claims, honest when data is missing."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.public_agent import PublicAgentService


def _prop(pid, name, **kw):
    base = dict(property_id=pid, property_name=name, district="Layan", province="Phuket", price_display="from 8.5 million THB",
                property_type="Villa", bedrooms_display="2-3", area_display="120-180 sqm", has_pool=True,
                status="Available", ownership_type="Freehold")
    base.update(kw)
    return SimpleNamespace(**base)


A = _prop("PHK-A", "Alpha Villa")
B = _prop("PHK-B", "Beta Residence", district="Bang Tao", price_display=None, bedrooms_display=None, area_display=None,
          has_pool=False, ownership_type=None)
C = _prop("PHK-C", "Gamma House", district="Karon", price_display="12 million THB", ownership_type="Leasehold")


def _compare(props, language="en", ids=None):
    agent = PublicAgentService.__new__(PublicAgentService)
    agent.model = "test"
    agent.client = None  # no model: the deterministic table is used
    conversation = SimpleNamespace(id="c1", session_id="s1", site_origin="tajlandia.com", preferences={}, message_history=[])
    with patch("app.services.public_agent.get_properties_by_ids", return_value=props), \
         patch("app.services.public_agent.format_property_card", side_effect=lambda p: {"property_id": p.property_id}), \
         patch("app.services.public_agent.to_compact_projection", side_effect=lambda p, intent=None: {}), \
         patch("app.services.public_agent.append_conversation_turn"), \
         patch("app.services.public_agent.token_tracker"):
        return agent._handle_property_comparison(
            db=MagicMock(), conversation=conversation, args={"property_ids": ids or [p.property_id for p in props]},
            user_message="compare", start_time=0.0, language=language)


def test_two_listings_are_compared_side_by_side():
    res = _compare([A, B])
    text = res["text"]
    assert res["type"] == "property_cards" and [c["property_id"] for c in res["properties"]] == ["PHK-A", "PHK-B"]
    assert "`PHK-A`" in text and "`PHK-B`" in text and "Layan" in text and "Bang Tao" in text
    assert "from 8.5 million THB" in text


def test_three_listings_are_all_in_the_table():
    res = _compare([A, B, C])
    header = next(line for line in res["text"].splitlines() if line.startswith("| Feature"))
    assert header.count("|") == 5 and "Alpha Villa" in header and "Beta Residence" in header and "Gamma House" in header
    for pid in ("PHK-A", "PHK-B", "PHK-C"):
        assert f"`{pid}`" in res["text"]
    assert "Leasehold" in res["text"] and "12 million THB" in res["text"]
    assert len(res["properties"]) == 3


def test_more_than_three_is_limited_to_three():
    res = _compare([A, B, C, _prop("PHK-D", "Delta Place")])
    assert len(res["properties"]) == 3 and "`PHK-D`" not in res["text"]


def test_missing_values_are_shown_as_missing_not_invented():
    text = _compare([A, B])["text"]
    row = {line.split("|")[1].strip(" *"): line for line in text.splitlines() if line.startswith("| **")}
    assert "On Request" in row["Starting Price"]
    assert "N/A" in row["Bedrooms"] and "N/A" in row["Built Area"]
    assert "Confirm with advisor" in row["Ownership"]


def test_no_unsupported_marketing_claims_are_added():
    for language in ("en", "pl", "th"):
        text = _compare([A, B, C], language)["text"].lower()
        for phrase in ("construction quality", "high-demand", "investment locations", "prestiżowych", "standardy budowlane", "มาตรฐานคุณภาพ"):
            assert phrase not in text, (language, phrase)


@pytest.mark.parametrize("language,feature", [("en", "Feature"), ("pl", "Parametr"), ("th", "หัวข้อ")])
def test_each_language_has_a_complete_table(language, feature):
    lines = _compare([A, B, C], language)["text"].splitlines()
    table = [line for line in lines if line.startswith("|")]
    assert table[0].startswith(f"| {feature} |") and len(table) == 2 + 9
    assert all(line.count("|") == 5 for line in table)


def test_a_single_listing_asks_for_a_second_one():
    res = _compare([A])
    assert res["type"] == "text" and res["properties"] == [] and "at least two" in res["text"]
