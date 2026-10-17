"""Follow-up answers come straight from the verified record, or say honestly that the data does not hold it."""
import pytest
from app.services.listing_facts import answer_listing_fact as ans

RAW = {
    "completionDate": "2027-03", "completionStatus": "under_construction", "currency": "THB",
    "priceFrom": "2990000", "priceTo": "11736000", "pricePerSqm": "101770",
    "availableUnits": "57", "totalUnits": "564", "paymentPlan": "", "developerId": "",
    "address": {"en": "Bophut, Ko Samui", "pl": ""},
    "amenities": ["pool", "gym", "security", "parking", "balcony"],
    "layouts": [
        {"unitType": "studio", "priceFrom": "2990000", "priceTo": "7042000", "currency": "THB", "area": 29.9, "availableUnits": "54"},
        {"unitType": "condo", "bedrooms": "1", "priceFrom": "5784174", "priceTo": "6620000", "currency": "THB", "area": "49", "availableUnits": "2"},
    ],
}
CARD = {"name": "Anava Condominium", "status": "under_construction", "currency": "THB",
        "price_numeric_min": 2990000, "raw_firestore_data": RAW, "amenities": RAW["amenities"]}
ADV = "advisor@varsoviaestate.com"


def a(q, lang="en", owner=None):
    return ans(q, CARD, language=lang, advisor_email=ADV, ownership=owner)


def test_completion_uses_the_record():
    r = a("When will it be finished?")
    assert "March 2027" in r and "under construction" in r


def test_units_and_layouts():
    assert "57 units available out of 564" in a("How many are still available?")
    r = a("What is the size of the studio?")
    assert "29.9" in r and "54 available" in r
    assert "49" in a("What is the area of the one-bedroom?")
    assert "Studio, 1-bedroom" in a("How many bedrooms are available?")


def test_currency_is_arithmetic_and_labelled_indicative():
    for q, cur in (("Give me the price in euro", "EUR"), ("And in zloty", "PLN"), ("And in US dollars", "USD")):
        r = a(q)
        assert cur in r and "indicative" in r and "2.99M THB" in r


@pytest.mark.parametrize("q", [
    "What is the payment plan?", "Who is the developer?", "How far is it from the airport?",
    "Does it allow pets?", "Is wifi included?", "What taxes will I pay?", "What floor is the 2-bedroom on?",
])
def test_missing_data_is_said_plainly(q):
    r = a(q)
    assert "isn't in our verified data" in r and ADV in r


@pytest.mark.parametrize("q", [
    "What is the rental yield?", "Can you guarantee 8% return?", "Is it a good investment?", "Will the price go up?",
])
def test_no_returns_or_forecasts(q):
    r = a(q)
    assert "can't be guaranteed" in r
    assert "%" not in r.replace("8%", "")  # nothing quoted back as a figure


def test_ownership_states_record_value_and_defers_foreign_rules():
    assert "leasehold" in a("Can a foreigner buy it?", owner="leasehold")
    assert "isn't in our verified data" in a("Is it freehold?", owner=None)


def test_amenity_present_or_absent():
    assert "yes" in a("Is there a balcony?")
    assert "isn't in our verified data" in a("Is there a sauna?")


def test_unhandled_and_other_languages_fall_through():
    assert a("Tell me about it") is None
    assert a("How much does it cost?") is None
    assert a("เมื่อไหร่จะเสร็จ", lang="th") is None


def test_polish():
    assert "marzec 2027" in a("Kiedy będzie gotowe? ukończenie", lang="pl")
