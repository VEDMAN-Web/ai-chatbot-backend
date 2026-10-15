import pytest
from app.services.i18n_service import (
    build_consultative_followup_questions,
    build_encouraging_alternative_intro,
    build_rich_property_recommendation_text,
    get_zero_match_text
)

def test_build_consultative_followup_questions_max_limit():
    # Test that questions returned are between 1 and 3 questions (never > 3)
    questions_en = build_consultative_followup_questions(language="en", location=None, budget=None, bedrooms=None)
    assert len(questions_en) <= 3
    assert len(questions_en) >= 1
    # Check that it asks about location, budget, bedrooms/lifestyle
    combined_en = " ".join(questions_en).lower()
    assert "budget" in combined_en or "island" in combined_en or "bedroom" in combined_en

    questions_pl = build_consultative_followup_questions(language="pl", location="Phuket", budget=None, bedrooms=2)
    assert len(questions_pl) <= 3
    assert len(questions_pl) >= 1
    combined_pl = " ".join(questions_pl).lower()
    assert "budżet" in combined_pl or "inwestycyjny" in combined_pl or "lokalizacj" in combined_pl

    questions_th = build_consultative_followup_questions(language="th", location=None, budget=10000000, bedrooms=None)
    assert len(questions_th) <= 3
    assert len(questions_th) >= 1


def test_build_encouraging_alternative_intro():
    intro_en = build_encouraging_alternative_intro("en", searched_location="Phuket", searched_bedrooms=10)
    assert "phuket" in intro_en.lower() or "10" in intro_en.lower()
    # Check encouraging/motivating tone
    assert any(w in intro_en.lower() for w in ["delighted", "curated", "exceptional", "verified", "pleasure", "proud"])

    intro_pl = build_encouraging_alternative_intro("pl", searched_location="Samui", searched_bedrooms=6)
    assert "samui" in intro_pl.lower() or "6" in intro_pl.lower()
    assert any(w in intro_pl.lower() for w in ["przyjemnością", "wyselekcjonowaliśmy", "wyjątkow", "atrakcyjn"])


def test_build_rich_property_recommendation_text():
    sample_cards = [
        {
            "property_id": "prop-1",
            "name": "Ocean Vista Luxury Villa",
            "location": "Phuket, Choeng Thale",
            "price": "THB 28,500,000",
            "bedrooms": 4,
            "property_type": "Villa",
            "has_pool": True,
            "beach_access": True,
            "ownership_structure": "30-Year Registered Leasehold Villa"
        },
        {
            "property_id": "prop-2",
            "name": "Azure Horizon Beachfront Condo",
            "location": "Bangkok, Sukhumvit",
            "price": "THB 12,000,000",
            "bedrooms": 2,
            "property_type": "Condominium",
            "has_pool": True,
            "beach_access": False,
            "ownership_structure": "Foreign Freehold Condominium"
        }
    ]

    # Test rich text recommendation with sparse query
    rich_text = build_rich_property_recommendation_text(
        cards=sample_cards,
        language="en",
        is_alternative=False,
        is_sparse=True,
        searched_location="Phuket"
    )

    # 1. Contains rich intro and details
    assert "Ocean Vista Luxury Villa" in rich_text
    assert "Azure Horizon Beachfront Condo" in rich_text
    assert "Choeng Thale" in rich_text
    assert "28,500,000" in rich_text
    
    # 2. Contains consultative follow-up questions (at most 2-3)
    # Check that questions are present
    assert "?" in rich_text
    question_lines = [line for line in rich_text.split("\n") if "?" in line]
    # Follow-up questions should not exceed 3
    assert len(question_lines) <= 4  # allow max 3 bullet questions + potential prompt

    # 3. Test alternative recommendation with encouraging tone
    rich_alt_text = build_rich_property_recommendation_text(
        cards=sample_cards,
        language="en",
        is_alternative=True,
        is_sparse=True,
        searched_location="Koh Samui",
        searched_bedrooms=8
    )
    assert any(w in rich_alt_text.lower() for w in ["delighted", "recommend", "handpicked", "exceptional", "pleasure"])
