"""'Cheapest' / 'most expensive' must rank by real price across all matches (it used to return relevance order)."""
import pytest
from app.services.intent_router import detect_price_sort


@pytest.mark.parametrize("msg,expected", [
    ("Which one is the cheapest?", "asc"),
    ("Show me the cheapest villa in Phuket", "asc"),
    ("most affordable condo in Pattaya", "asc"),
    ("Najtańsza willa w Phuket", "asc"),
    ("most expensive villa", "desc"),
    ("Show me villas in Phuket", None),
    ("cheapest or most expensive", None),
])
def test_detect_price_sort(msg, expected):
    assert detect_price_sort(msg) == expected
