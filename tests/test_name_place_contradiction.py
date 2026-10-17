from app.services.property_name_index import PropertyNameIndex


def _index() -> PropertyNameIndex:
    idx = PropertyNameIndex()
    idx.load_from_rows([
        {"doc_id": "mirage-sukhumvit-27", "name": {"en": "", "pl": "Mirage Sukhumvit 27"}, "cityId": "bangkok",
         "slug": {"en": "mirage-sukhumvit-27"}},
        {"doc_id": "anava-condominium", "name": {"en": "Anava Condominium"}, "cityId": "samui",
         "slug": {"en": "anava-condominium"}},
    ])
    idx._loaded_at = 10**12  # never refresh in tests
    return idx


def test_brand_word_with_wrong_place_is_not_confident():
    r = _index().match("Villa Mirage Samui")
    assert r.confident is None
    assert [c.display_name for c in r.candidates] == ["Mirage Sukhumvit 27"]


def test_brand_word_with_right_place_stays_confident():
    assert _index().match("Mirage Bangkok").confident.doc_id == "mirage-sukhumvit-27"
    assert _index().match("Anava Samui").confident.doc_id == "anava-condominium"


def test_plain_name_stays_confident():
    assert _index().match("Mirage").confident.doc_id == "mirage-sukhumvit-27"
