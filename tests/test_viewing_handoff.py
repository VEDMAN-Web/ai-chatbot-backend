from app.services.viewing_handoff import build_viewing_reply, is_viewing_request

CONTACTS = [{"label": "Thailand", "phone": "+66 81 679 4414"}, {"label": "Cambodia", "phone": "+855 96 301 5807"}]


def test_detects_viewing_requests():
    for q in ("Can I visit it next week?", "Schedule viewing for abc123", "I am interested in X. Can you provide details and arrange a viewing?",
              "I want to visit this villa", "Chciałbym obejrzeć ten apartament", "Umów wizytę", "Can I see it in person?"):
        assert is_viewing_request(q), q


def test_ignores_normal_questions():
    for q in ("How much is it?", "Can foreigners visit Thailand without a visa?", "Show me villas in Phuket", "What is the view like?", "Does it have a sea view?"):
        assert not is_viewing_request(q), q


def test_reply_hands_over_and_never_invents_dates():
    r = build_viewing_reply("en", "Anava Condominium", CONTACTS, "advisor@varsoviaestate.com")
    assert "Anava Condominium" in r and "advisor" in r.lower() and "+66 81 679 4414" in r
    assert not any(w in r.lower() for w in ("monday", "tomorrow at", "confirmed for"))
    assert "Porozmawiaj z doradcą" in build_viewing_reply("pl", None, CONTACTS, "a@b.c")


def test_name_for_id_uses_card_display_name():
    from app.services.viewing_handoff import name_for_id
    hist = [{"role": "assistant", "content": "x", "structured_data": {"properties": [{"property_id": "p1", "name": "9D Cozy Villas"}]}}]
    assert name_for_id(hist, "p1") == "9D Cozy Villas" and name_for_id(hist, "zz") is None and name_for_id(hist, None) is None
