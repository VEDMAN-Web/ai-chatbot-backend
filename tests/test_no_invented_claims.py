"""Step 2: prompts/templates must not invent yields or marketing filler."""
from app.services.approved_statements import APPROVED_STATEMENTS
from app.services.i18n_service import (
    build_rich_property_recommendation_text,
    get_off_topic_text,
    get_public_system_prompt,
)


BANNED = [
    "6%-10%",
    "6%–10%",
    "outstanding selection offering premium finishes",
    "I am delighted to present these verified luxury properties",
]


def test_approved_statements_empty_by_default():
    assert APPROVED_STATEMENTS == []


def test_public_system_prompt_has_no_invented_yields():
    for lang in ("en", "pl", "th"):
        prompt = get_public_system_prompt(lang, site_origin="tajlandia.com")
        for banned in ("6%-10%", "6%–10%"):
            assert banned not in prompt, f"{lang} prompt still contains {banned}"


def test_foreign_inquiry_reply_has_no_yield_numbers():
    text = get_off_topic_text(
        "en",
        site_origin="thai.estate",
        user_message="I want to buy property in Dubai",
    )
    assert "6%-10%" not in text
    assert "ROI" not in text or "do not" in text.lower() or "only cover" in text.lower()
    assert "advisor@thai.estate" in text
    assert "advisor@varsoviaestate.com" not in text


def test_listing_template_is_neutral_field_only():
    cards = [
        {
            "property_id": "p1",
            "name": "Test Villa",
            "location": "Phuket",
            "price": "from 12M THB",
            "bedrooms": 3,
            "has_pool": True,
        }
    ]
    text = build_rich_property_recommendation_text(
        cards=cards,
        language="en",
        is_sparse=False,
        searched_location="Phuket",
        searched_budget=20_000_000,
        searched_bedrooms=3,
        site_origin="tajlandia.com",
    )
    for banned in BANNED:
        assert banned not in text
    assert "Test Villa" in text
    assert "from 12M THB" in text
    assert "Starting from from" not in text
    assert "Starting from Price on request" not in text
