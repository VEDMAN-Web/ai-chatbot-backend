import pytest
from app.services.admin_chat_crud import parse_new_listing, extract_price
from app.services.admin_chat_quality import format_saved_listing_reply

def test_test1_exact_sentence_parses_all_fields():
    sentence = "Add a new listing: ZZ AI Test Villa, a villa in Lamai, Koh Samui, price 9,500,000 THB, status Available"
    parsed = parse_new_listing(sentence)
    
    assert parsed.fields.get("property_name") == "ZZ AI Test Villa"
    assert parsed.fields.get("property_type") == "Villa"
    assert "Lamai" in (parsed.fields.get("province") or "")
    assert parsed.fields.get("price_numeric_min") == 9_500_000
    assert parsed.fields.get("price_display") == "9,500,000 THB"
    assert parsed.fields.get("status") == "Available"
    assert parsed.property_id == "zz-ai-test-villa"
    assert len(parsed.errors) == 0
    assert len(parsed.missing) == 0

def test_single_help_text_on_missing_fields():
    incomplete = "Add a new listing: ZZ Incomplete Villa"
    parsed = parse_new_listing(incomplete)
    assert len(parsed.missing) > 0
    # There should only be one help text string in errors
    help_errors = [e for e in parsed.errors if "For example:" in e]
    assert len(help_errors) <= 1

def test_create_confirmation_message_states_created():
    reply = format_saved_listing_reply(
        title="ZZ AI Test Villa",
        new_value="9,500,000 THB",
        old_value="—",
        store_line="verified in database (Cloud Firestore)",
        undo=True,
        mode="create",
    )
    assert "New listing ZZ AI Test Villa created." in reply
    assert "(was —)" not in reply
    assert "(was -)" not in reply

def test_foreign_currency_price_extraction():
    reading_usd = extract_price("price $250,000")
    assert reading_usd is not None
    assert reading_usd.value == 250_000
    assert reading_usd.currency == "USD"

    reading_eur = extract_price("price 500.000 €")
    assert reading_eur is not None
    assert reading_eur.value == 500_000
    assert reading_eur.currency == "EUR"

    reading_sgd = extract_price("price 160,000 SGD")
    assert reading_sgd is not None
    assert reading_sgd.value == 160_000
    assert reading_sgd.currency == "SGD"
