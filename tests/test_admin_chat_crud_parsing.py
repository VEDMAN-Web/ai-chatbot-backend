"""
What the admin chat understands from a command: the price, the status, the listing name, and what is being asked
(create, delete, undo). Cases come from the QA report on the Admin AI Assistant (35 chat cases, 19 failed).
"""
import pytest

from app.services.admin_chat_crud import (
    detect_crud_intent, extract_price, extract_status, operation_id_in, parse_new_listing, wants_permanent_delete,
)
from app.services.admin_property_resolver import extract_property_name_phrase


# ---- price: a number in the name is never the price ----------------------------------------------------------------

@pytest.mark.parametrize("message,expected", [
    ("Change the price of 3 Villas - Lamai to 5,000,000 THB", 5_000_000),       # the QA report: used to show 3 THB
    ("Update price of 3 Villas - Lamai to 5M", 5_000_000),
    ("Set the price of Villa 12 to 4.5 million", 4_500_000),
    ("change price of Rawai 88 Residence to 12000000", 12_000_000),
    ("update 2 Bedroom Condo price to 3,200,000", 3_200_000),
    ("set price of Unit 5B to 750k", 750_000),
    ("price of Pirak Villa to ฿9,900,000", 9_900_000),
    ("Update price of Sunset Villa to 500 THB", 500),                           # a short value with a currency is kept
    ("change the price of 3 Villas - Lamai to 5 mln", 5_000_000),
    ("Set price of Alpha at 8000000 baht", 8_000_000),
])
def test_the_price_is_the_amount_asked_for_not_a_number_in_the_name(message, expected):
    reading = extract_price(message)
    assert reading.error is None and reading.value == expected


@pytest.mark.parametrize("message", [
    "Update price of 3 Villas - Lamai to -500", "change price of Alpha to -5,000,000 THB", "set price of Alpha to minus 500",
    "update price of Alpha to negative 1000", "set price of Alpha to 0 THB", "price of Alpha to −300 THB",
])
def test_a_negative_or_zero_price_is_an_error_not_a_positive_number(message):
    reading = extract_price(message)
    assert reading.present and reading.value is None and "greater than 0" in reading.error


@pytest.mark.parametrize("message", [
    "Change the status of 3 Villas - Lamai to ready", "Show 3 Villas - Lamai", "update price of Villa 12",
    "increase the price by 80%", "what is the price of 2 Bedroom Villa",
])
def test_no_price_is_invented_when_none_is_given(message):
    assert extract_price(message).value is None


# ---- status ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,expected", [
    ("Change the status of 3 Villas - Lamai to ready", "Ready to move in"),
    ("change status of Alpha to Ready to move in", "Ready to move in"),
    ("Update the status of Alpha to under construction", "Under construction"),
    ("set status of Alpha to sold", "Sold Out"),
    ("Change status of Alpha to Sold Out", "Sold Out"),
    ("change status of Alpha to reserved", "Reserved"),
    ("update status of Alpha to available", "Available"),
    ("Update status of Alpha to for sale and price to 5M", "For sale"),
    ("mark Alpha as sold out", "Sold Out"),
    ("set Alpha as planned", "Planned"),
])
def test_status_is_mapped_to_an_allowed_value(message, expected):
    reading = extract_status(message)
    assert reading.error is None and reading.value == expected


@pytest.mark.parametrize("message,bad", [
    ("Change the status of 3 Villas - Lamai to banana", "banana"), ("update status of Alpha to coming soon", "coming soon"),
])
def test_an_unknown_status_is_refused_and_lists_the_valid_ones(message, bad):
    reading = extract_status(message)
    assert reading.present and reading.value is None and bad in reading.error and "Available" in reading.error


def test_no_status_is_invented_when_none_is_asked():
    assert extract_status("update the price of Alpha to 5M").present is False


# ---- the listing name keeps its digits ----------------------------------------------------------------------------

@pytest.mark.parametrize("message,name", [
    ("Change the price of 3 Villas - Lamai to 5,000,000 THB", "3 Villas - Lamai"),
    ("Update the status of 3 Villas - Lamai to ready", "3 Villas - Lamai"),
    ("Delete 3 Villas - Lamai", "3 Villas - Lamai"),
    ("Change the price of Rawai 88 Residence to 12000000", "Rawai 88 Residence"),
    ("update price of Pirak Villa 12 to 4.5 million", "Pirak Villa 12"),
    ("Change the price of Pirak Villa to 5 million", "Pirak Villa"),
])
def test_the_name_phrase_keeps_digits_that_belong_to_the_name(message, name):
    assert extract_property_name_phrase(message) == name


# ---- what is being asked -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,intent", [
    ("Add a new listing: Sunset Pearl, villa, Phuket, 12,000,000 THB, available", "create_listing"),
    ("create property Sunset Pearl", "create_listing"),
    ("Please add a new villa called Sunset Pearl", "create_listing"),
    ("Add a new buyer lead: QA Tester, qa.tester@example.com", "create_lead"),
    ("create lead John", "create_lead"),
    ("Delete 3 Villas - Lamai", "delete"), ("remove the listing Pirak Villa", "delete"), ("archive Pirak Villa", "delete"),
    ("permanently delete 3 Villas - Lamai", "delete"), ("please hard delete Alpha", "delete"),
    ("Undo the last change", "undo"), ("undo that", "undo"), ("revert batch_0123456789ab", "undo"), ("Rollback the last update", "undo"),
    ("Show 3 Villas - Lamai", None), ("Specs for 3 Villas - Lamai", None), ("Export Koh Samui listings to Excel", None),
    ("how many properties do we have", None), ("what did we add last week", None), ("do not delete anything", None),
    ("list the leads", None), ("Update price of Alpha to 5M", None),
])
def test_the_command_is_recognised(message, intent):
    assert detect_crud_intent(message) == intent


def test_permanent_delete_is_recognised_so_it_can_be_refused():
    assert wants_permanent_delete("permanently delete Alpha") and wants_permanent_delete("hard delete Alpha")
    assert not wants_permanent_delete("delete Alpha")


def test_an_operation_id_is_found_in_the_message():
    assert operation_id_in("undo batch_0123456789ab please") == "batch_0123456789ab"
    assert operation_id_in("undo crud_aabbccddeeff") == "crud_aabbccddeeff"
    assert operation_id_in("undo the last change") is None


# ---- a new listing --------------------------------------------------------------------------------------------------

def test_a_complete_comma_separated_listing_is_understood():
    new = parse_new_listing("Add a new listing: Sunset Pearl, villa, Phuket, 12,000,000 THB, available")
    assert new.missing == [] and new.errors == []
    assert new.fields == {"property_name": "Sunset Pearl", "property_type": "Villa", "province": "Phuket",
                          "price_numeric_min": 12_000_000, "price_display": "12,000,000 THB", "status": "Available"}
    assert new.property_id == "sunset-pearl"


def test_a_keyed_listing_is_understood_and_keeps_digits_in_the_name():
    new = parse_new_listing("Create property: name: 3 Villas - Lamai, type: condo, location: Koh Samui, price: 5M, status: ready, beds: 3")
    assert new.errors == [] and new.missing == []
    assert new.fields["property_name"] == "3 Villas - Lamai" and new.fields["property_type"] == "Condominium"
    assert new.fields["price_numeric_min"] == 5_000_000 and new.fields["status"] == "Ready to move in"
    assert new.fields["bedrooms_min"] == 3 and new.fields["province"] == "Koh Samui"


def test_missing_parts_are_listed_not_invented():
    new = parse_new_listing("Add a new listing: Sunset Pearl, villa")
    assert [m.split(" ")[1] for m in new.missing] == ["location", "price", "status"]
    assert "property_name" in new.fields and "province" not in new.fields and "price_numeric_min" not in new.fields


def test_a_bad_status_or_negative_price_in_a_new_listing_is_an_error():
    stray = parse_new_listing("Add a new listing: A Villa, villa, Phuket, 5M, banana")
    assert any("banana" in e for e in stray.errors) and any("status" in m for m in stray.missing)   # not silently dropped
    bad = parse_new_listing("Add a new listing: name: A Villa, type: villa, location: Phuket, price: -5, status: banana")
    assert len(bad.errors) == 2


# ---- reading one listing by name -------------------------------------------------------------------------------------

from app.services.admin_chat_crud import extract_listing_name_for_read


@pytest.mark.parametrize("message,name", [
    ("Show 3 Villas - Lamai", "3 Villas - Lamai"),
    ("Specs for 3 Villas - Lamai", "3 Villas - Lamai"),
    ("specifications of Pirak Villa", "Pirak Villa"),
    ("Tell me about Rawai 88 Residence", "Rawai 88 Residence"),
    ("What is the price of Pirak Villa?", "Pirak Villa"),
    ("details for Sunset Pearl", "Sunset Pearl"),
    ("show the listing Pirak Villa", "Pirak Villa"),
])
def test_a_named_listing_is_picked_out_of_a_read_command(message, name):
    assert extract_listing_name_for_read(message) == name


@pytest.mark.parametrize("message", [
    "Show villas in Phuket", "show me properties under 5M", "show all listings", "list the leads", "show villas with a pool",
    "Export Koh Samui listings to Excel", "how many properties do we have", "show more", "show it", "find condos near Rawai",
])
def test_a_search_is_not_taken_for_a_listing_name(message):
    assert extract_listing_name_for_read(message) is None
