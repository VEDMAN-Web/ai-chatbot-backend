"""'100 tys. THB' means a hundred thousand, not a hundred million (it used to return 80-97M THB villas)."""
import pytest
from app.services.intent_router import extract_search_filters_deterministic as ex


@pytest.mark.parametrize("msg,expected", [
    ("Pokaż wille w Phuket do 100 tys. THB", 100_000),
    ("villas in Phuket under 500 thousand THB", 500_000),
    ("Pokaż wille do 300 tysięcy THB", 300_000),
    ("Show me villas in Phuket under 20 million THB", 20_000_000),
    ("Pokaż wille w Phuket do 20 mln THB", 20_000_000),
    ("condo up to 5M THB", 5_000_000),
])
def test_budget_units(msg, expected):
    assert ex(msg).get("max_price") == expected
