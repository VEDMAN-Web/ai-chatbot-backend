"""Bot-made listings match the website's Project shape (varsovia-estate src/types/index.ts)."""
from __future__ import annotations

import pytest

from app.services import firestore_service as fs
from app.services import listing_shape as ls
from tests.fake_firestore import FakeFirestore, patch_firestore

WEBSITE_FIELDS = {
    "name": dict, "slug": dict, "subtitle": dict, "description": dict, "shortDescription": dict,
    "address": dict, "seoTitle": dict, "seoDescription": dict, "countryId": str, "cityId": str,
    "developerId": str, "priceFrom": float, "priceTo": float, "pricePerSqm": float, "currency": str,
    "propertyType": str, "completionDate": str, "completionStatus": str, "totalUnits": float,
    "availableUnits": float, "areaFrom": float, "areaTo": float, "layouts": list, "images": list,
    "coverImage": str, "videoUrl": str, "coordinates": dict, "amenities": list, "expectedROI": float,
    "rentalYield": float, "paymentPlan": str, "tags": list, "isActive": bool, "isFeatured": bool,
    "isTopList": bool,
}
WEBSITE_TYPES = {"condo", "villa", "apartment", "townhouse", "penthouse", "hotel_unit", "land"}


@pytest.fixture()
def store(monkeypatch):
    fake = patch_firestore(monkeypatch, FakeFirestore())
    ls._CACHE.clear()
    # Same ids/names as the website seed data (scripts/seed-data.ts)
    fake.collection("cities").document("samui").set({"name": {"pl": "Koh Samui", "en": "Koh Samui"}, "slug": {"pl": "samui", "en": "samui"},
                                                     "countryId": "thailand", "coordinates": {"lat": 9.512, "lng": 100.0136}, "isActive": True})
    fake.collection("cities").document("phuket").set({"name": {"pl": "Phuket", "en": "Phuket"}, "countryId": "thailand",
                                                      "coordinates": {"lat": 7.8804, "lng": 98.3923}, "isActive": True})
    fake.collection("cities").document("phnom-penh").set({"name": {"en": "Phnom Penh"}, "countryId": "cambodia", "isActive": True})
    fake.collection("countries").document("thailand").set({"name": {"en": "Thailand", "pl": "Tajlandia"}})
    fake.collection("developers").document("dev-abc").set({"name": "ABC Developments"})
    return fake


def _doc(store, pid):
    return store.collection("projects").document(pid).get().to_dict()


@pytest.mark.parametrize("fields,city", [
    ({"province": "Surat Thani", "district": "Lamai", "area": "Samui"}, "samui"),
    ({"province": "Ko Samui"}, "samui"),
    ({"province": "Koh Samui"}, "samui"),
    ({"province": "Phuket", "district": "Patong"}, "phuket"),
    ({"province": "Thailand", "address": "12 Moo 4, Bophut, Koh Samui"}, "samui"),
    ({"province": "Phnom Penh", "district": "BKK1"}, "phnom-penh"),
    ({"province": "Lamai Beach"}, "samui"),
    ({"province": "Koh Samui (Bophut)"}, "samui"),
    ({"province": "Samui Island"}, "samui"),
    ({"province": "Rawai, Phuket"}, "phuket"),
    ({"location": "Bang Tao Beach, Phuket"}, "phuket"),
    ({"province": "Kathu - Patong"}, "phuket"),
    ({"province": "Puket"}, "phuket"),
    ({"province": "ภูเก็ต"}, "phuket"),
    ({"province": "เกาะสมุย"}, "samui"),
    ({"province": "Surat Thani", "district": "Bophut"}, "samui"),
])
def test_real_inputs_find_the_website_city(store, fields, city):
    hit = ls.find_city(store, fields)
    assert hit and hit[0] == city


def test_address_only_update_never_moves_the_listing(store):
    store.collection("projects").document("pat-1").set({"name": {"en": "P"}, "cityId": "phuket", "countryId": "thailand"})
    fs.save_or_update_property_firestore("pat-1", {"address": "Patong Road, Bangkok side street"}, target="admin")
    assert _doc(store, "pat-1")["cityId"] == "phuket"
    fs.save_or_update_property_firestore("pat-1", {"province": "Koh Samui"}, target="admin")
    assert _doc(store, "pat-1")["cityId"] == "samui"


def test_unknown_city_is_reported_but_lookup_problems_never_block(store, monkeypatch):
    msg = ls.city_problem(store, {"province": "Krabi"})
    assert msg and "Krabi" in msg and "Koh Samui" in msg
    assert ls.city_problem(store, {"province": "Lamai"}) is None
    ls._CACHE.clear()
    monkeypatch.setattr(store, "collection", lambda name: (_ for _ in ()).throw(RuntimeError("down")))
    assert ls.city_problem(store, {"province": "Krabi"}) is None


def test_create_validation_asks_for_a_known_city(store):
    from app.services.action_service import validate_listing_values
    base = {"property_name": "ZZ V", "property_type": "Villa", "price_numeric_min": 5_000_000, "status": "Available"}
    errs = validate_listing_values({**base, "province": "Krabi"}, is_create=True)
    assert "location" in errs and "Krabi" in errs["location"]
    assert "location" not in validate_listing_values({**base, "province": "Surat Thani", "district": "Lamai"}, is_create=True)
    # an update that moves the listing is checked too; other updates are not
    assert "Krabi" in validate_listing_values({"province": "Krabi"}, is_create=False)["location"]
    assert "location" not in validate_listing_values({"province": "Patong"}, is_create=False)
    assert "location" not in validate_listing_values({"price_numeric_min": 5}, is_create=False)


def test_new_listing_has_every_website_field_in_website_shape(store):
    assert fs.save_or_update_property_firestore(
        "zz-full", {"property_name": "ZZ Full", "province": "Surat Thani", "district": "Lamai", "property_type": "Villa",
                    "price_numeric_min": 12_000_000, "status": "Ready to move in", "developer_name": "abc developments"},
        target="admin")
    doc = _doc(store, "zz-full")
    for key, kind in WEBSITE_FIELDS.items():
        assert isinstance(doc.get(key), kind), (key, doc.get(key))
    for key in ("name", "slug", "description", "address"):
        assert {"pl", "en"} <= set(doc[key]), key
    assert doc["name"]["en"] == doc["name"]["pl"] == "ZZ Full"
    assert doc["slug"]["en"] == "zz-full"  # the backend builds website links from the id
    assert (doc["cityId"], doc["countryId"], doc["developerId"]) == ("samui", "thailand", "dev-abc")
    assert doc["coordinates"] == {"lat": 9.512, "lng": 100.0136}
    assert doc["propertyType"] == "villa" and doc["propertyTypeLabel"] == "Villa"
    assert doc["priceFrom"] == 12_000_000 and doc["currency"] == "THB" and doc["completionStatus"] == "ready"
    assert doc["isActive"] is True


def test_staff_currency_kept_and_city_without_coordinates_gets_country_centre(store):
    fs.save_or_update_property_firestore("zz-pp", {"property_name": "ZZ PP", "province": "Phnom Penh",
                                                   "property_type": "Condominium", "currency": "usd",
                                                   "price_numeric_min": 90_000}, target="admin")
    doc = _doc(store, "zz-pp")
    assert doc["currency"] == "USD" and doc["countryId"] == "cambodia"
    assert doc["coordinates"]["lat"] != 0 and doc["coordinates"]["lng"] != 0
    assert doc["propertyType"] in WEBSITE_TYPES


def test_new_listing_needs_a_price_number(store):
    from app.services.action_service import validate_listing_values
    base = {"property_name": "ZZ V", "property_type": "Villa", "province": "Phuket", "status": "Available"}
    assert "price" in validate_listing_values({**base, "price_display": "Price on request"}, is_create=True)
    assert "price" not in validate_listing_values({**base, "price_numeric_min": 5_000_000}, is_create=True)
    assert "price" not in validate_listing_values({**base, "price_display": "12,000,000 THB"}, is_create=True)
    fs.save_or_update_property_firestore("zz-pt", {"property_name": "ZZ PT", "province": "Phuket",
                                                   "price_display": "12M THB"}, target="admin")
    assert _doc(store, "zz-pt")["priceFrom"] == 12_000_000
    # if a write still has no price, the document stays well-formed
    fs.save_or_update_property_firestore("zz-np", {"property_name": "ZZ NP", "province": "Phuket"}, target="admin")
    doc = _doc(store, "zz-np")
    assert doc["priceFrom"] == 0.0 and doc["priceOnRequest"] is True


def test_update_keeps_website_shapes_and_maps_type(store):
    store.collection("projects").document("web-1").set({
        "name": {"pl": "Dom", "en": "House"}, "address": {"pl": "Ulica", "en": "Street"},
        "description": {"pl": "Opis", "en": "Desc"}, "propertyType": "villa", "cityId": "phuket"})
    fs.save_or_update_property_firestore("web-1", {"address": "New Street", "description": "New text",
                                                   "property_type": "Condominium"}, target="admin")
    doc = _doc(store, "web-1")
    assert doc["address"] == {"pl": "New Street", "en": "New Street"}
    assert doc["description"] == {"pl": "New text", "en": "New text"}
    assert doc["propertyType"] == "condo" and doc["propertyTypeLabel"] == "Condominium"
    assert doc["name"] == {"pl": "Dom", "en": "House"}
    # a province-only update no longer wipes a localized address
    fs.save_or_update_property_firestore("web-1", {"province": "Phuket"}, target="admin")
    assert _doc(store, "web-1")["address"] == {"pl": "New Street", "en": "New Street"}


def test_cards_show_one_language_readable_type_and_no_ocean_pin(store):
    card = fs.normalize_firestore_project("x", {
        "name": {"en": "X"}, "address": {"pl": "Lamai", "en": "Lamai"}, "propertyType": "condo",
        "coordinates": {"lat": 0, "lng": 0}, "priceFrom": 0})
    assert card["property_type"] == "Condominium"
    assert "Lamai, Lamai" not in str(card.get("location") or "") + str(card.get("address") or "")
    assert not card.get("latitude") and not card.get("longitude")


@pytest.mark.parametrize("raw,want", [("Villa", "villa"), ("Condominium", "condo"), ("House", "villa"),
                                      ("Land", "land"), ("Penthouse", "penthouse"), ("condo", "condo")])
def test_type_mapping(raw, want):
    assert ls.website_type(raw) == want


def test_slug_keeps_polish_letters_readable():
    assert ls.slugify("Złota Wieża Łódź") == "zlota-wieza-lodz"
