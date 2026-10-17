"""Display polish: capitalised place names, readable status, contextual quick replies."""
from app.services.i18n_service import build_rich_property_recommendation_text, get_quick_replies
from app.services.listing_facts import display_status, humanize_status
from app.services.listing_presentation import display_location, shorten_location
from app.services.public_agent import PublicAgentService
from app.services.shown_set import answer_shown_set, answer_shown_set_question, contextual_quick_replies


def test_display_location_capitalises_slugs_and_maps_awkward_names():
    cases = {
        "phuket": "Phuket", "pattaya": "Pattaya", "bangkok": "Bangkok", "samui": "Koh Samui",
        "koh samui": "Koh Samui", "hua hin": "Hua Hin", "hua-hin": "Hua Hin", "phnom penh": "Phnom Penh",
        "siem_reap": "Siem Reap", "sihanoukville": "Sihanoukville", "bang tao, phuket": "Bang Tao, Phuket",
    }
    for raw, want in cases.items():
        assert display_location(raw) == want, raw


def test_display_location_keeps_capitalised_thai_and_drops_empties():
    assert display_location("Koh Samui") == "Koh Samui"
    assert display_location("Bang Por, koh samui") == "Bang Por, Koh Samui"
    assert display_location("ภูเก็ต") == "ภูเก็ต"
    assert display_location("ภูเก็ต, phuket") == "ภูเก็ต, Phuket"
    assert display_location("samui, Koh Samui") == "Koh Samui"
    assert display_location(None) == "" and display_location("") == "" and display_location("None") == ""
    assert display_location("Phuket, none") == "Phuket"


def test_shorten_location_returns_display_form_and_filter_inputs_untouched():
    assert shorten_location(district="phuket", city="phuket", province="Thailand", location="phuket, Thailand") == "Phuket"
    assert shorten_location(district="samui", city="samui", province="Thailand", location="samui") == "Koh Samui"


def test_reply_text_shows_capitalised_city():
    cards = [{"name": "VIP Venus Karon", "location": "phuket", "price": "from 5.00M THB"}]
    txt = build_rich_property_recommendation_text(cards, language="en", location="phuket")
    assert "(Phuket)" in txt and "(phuket)" not in txt


def test_post_process_capitalises_card_locations_and_status_only_as_display():
    resp = {"type": "property_cards", "text": "x", "language": "en", "properties": [
        {"property_id": "p1", "location": "samui", "district": "samui", "province": "cambodia", "status": "under_construction"},
        {"property_id": "p2", "location": "ภูเก็ต", "status": "Available"},
    ]}
    out = PublicAgentService()._post_process_response(resp)
    a, b = out["properties"]
    assert a["location"] == "Koh Samui" and a["district"] == "Koh Samui" and a["province"] == "Cambodia"
    assert a["status"] == "Under construction" and a["property_id"] == "p1"
    assert b["location"] == "ภูเก็ต" and b["status"] == "Available"


def test_display_status_never_leaks_enum_values():
    assert display_status("UNDER_CONSTRUCTION") == "Under construction"
    assert display_status("under_construction", "pl") == "W budowie"
    assert display_status("off_plan") == "Off-plan"
    assert display_status("planned") == "Planned" and display_status("planned", "pl") == "Planowana"
    assert display_status("Sold Out") == "Sold Out" and display_status("Available") == "Available"
    assert display_status(None) == "" and display_status("None") == "" and display_status("null") == ""
    assert humanize_status("under_construction", "th") != "Under construction"


CARDS = [
    {"property_id": "a", "name": "Alpha", "price": "from 10.56M THB", "has_pool": True},
    {"property_id": "b", "name": "Beta", "price": "from 9.08M THB", "has_pool": True},
    {"property_id": "c", "name": "Gamma", "price": "Price on request", "has_pool": False},
]


def test_shown_set_answer_names_the_winner_and_text_api_unchanged():
    text, winner = answer_shown_set("Which of these is the cheapest?", CARDS, "en")
    assert winner == "Beta" and text == answer_shown_set_question("Which of these is the cheapest?", CARDS, "en")
    assert answer_shown_set("Which one is the most expensive?", CARDS, "en")[1] == "Alpha"
    assert answer_shown_set("Which one has a pool?", CARDS, "en")[1] == "Alpha"
    assert answer_shown_set("Hello there", CARDS, "en") is None


def test_contextual_quick_replies_en_pl_and_advisor_chip_matches_generic_one():
    en = contextual_quick_replies("en", "Beta")
    assert en == ["Tell me more about Beta", "Show me other options", "Connect me with an advisor"]
    assert en[-1] in get_quick_replies("en", True, "x")
    pl = contextual_quick_replies("pl", "Beta")
    assert pl[0] == "Powiedz więcej o Beta" and pl[-1] in get_quick_replies("pl", True, "x")
    assert contextual_quick_replies("en", None) == ["Show me other options", "Connect me with an advisor"]
    assert contextual_quick_replies("en", "Beta", advisor_first=True)[0] == "Connect me with an advisor"
