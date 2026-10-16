"""Listing honesty: no invented bedrooms, THB-normalised budgets, Cambodian places are regions.
Pure unit tests: no network, no Firestore."""
import app.services.firestore_service as fs


def _doc(name, city, country="thailand", price=5_000_000, cur="THB", layouts=None):
    d = {"name": {"pl": name, "en": ""}, "cityId": city, "countryId": country, "propertyType": "villa",
         "priceFrom": price, "currency": cur, "isActive": True,
         "slug": {"pl": name.lower().replace(" ", "-"), "en": name.lower().replace(" ", "-")}}
    if layouts is not None:
        d["layouts"] = layouts
    return d


def _cards(*docs):
    return [fs.normalize_firestore_project(f"id{i}", d) for i, d in enumerate(docs)]


def test_no_invented_bedrooms_when_layouts_missing():
    c = _cards(_doc("Alpha Villas", "phuket"))[0]
    assert c["bedrooms"] == ""
    assert "Beds" not in c["description"]


def test_bedrooms_taken_from_layouts_when_present():
    c = _cards(_doc("Beta Villas", "phuket", layouts=[{"bedrooms": 2}, {"bedrooms": 4}]))[0]
    assert c["bedrooms"] == "2-4 Beds"


def test_usd_price_converted_to_thb():
    c = _cards(_doc("Gamma", "phuket", price=100_000, cur="USD"))[0]
    assert c["price_thb_min"] == 100_000 * fs.settings.FX_USD_THB
    assert fs.to_thb(5_000_000, "THB") == 5_000_000
    assert fs.to_thb(1000, "XYZ") is None  # unknown currency is never guessed


def _search(monkeypatch, cards, **kw):
    monkeypatch.setattr(fs, "fetch_properties_from_firestore", lambda **k: list(cards))
    return fs.search_properties_firestore(target="user", limit=10, **kw)


def test_budget_search_does_not_treat_usd_as_thb(monkeypatch):
    cards = _cards(_doc("Cheap THB", "phuket", price=900_000),
                   _doc("Sunny USD", "phuket", price=678_710, cur="USD"))
    names = [c["name"] for c in _search(monkeypatch, cards, location="Phuket", max_price=1_000_000)]
    assert "Cheap THB" in names and "Sunny USD" not in names


def test_bedroom_search_only_returns_listings_with_known_bedrooms(monkeypatch):
    cards = _cards(_doc("Known 3bed", "phuket", layouts=[{"bedrooms": 3}]), _doc("Unknown beds", "phuket"))
    names = [c["name"] for c in _search(monkeypatch, cards, location="Phuket", bedrooms=3)]
    assert names == ["Known 3bed"]


def test_phnom_penh_is_a_region_not_a_property_name(monkeypatch):
    cards = _cards(_doc("The Olympia Condominium", "phuket"),
                   _doc("Vue Aston", "phnom-penh", country="cambodia", price=110_000, cur="USD"))
    names = [c["name"] for c in _search(monkeypatch, cards, location="Phnom Penh")]
    assert names == ["Vue Aston"]


def test_cambodia_country_search(monkeypatch):
    cards = _cards(_doc("The Olympia Condominium", "phuket"),
                   _doc("Vue Aston", "phnom-penh", country="cambodia", price=110_000, cur="USD"))
    names = [c["name"] for c in _search(monkeypatch, cards, location="Cambodia")]
    assert names == ["Vue Aston"]
