import json
import pytest
from app.services.property_web_ingest import extract_draft, FetchedPage, _json_blocks, _parse_html, _clean_listing_name

FAZWAZ_URL = "https://www.fazwaz.com/property-sales/1-bedroom-condo-for-sale-at-the-line-phahol-pradipat-in-phaya-thai-bangkok-u6003334"

FAZWAZ_HTML_WITH_AGENT_LD = """<!DOCTYPE html>
<html>
<head>
    <title>1 Bedroom Condo for Sale at The Line Phahol - Pradipat in Phaya Thai, Bangkok | FazWaz.com</title>
    <meta property="og:title" content="1 Bedroom Condo for Sale at The Line Phahol - Pradipat in Phaya Thai, Bangkok" />
    <script type="application/ld+json">
    {
        "@context": "https://schema.org",
        "@type": "RealEstateAgent",
        "name": "FazWaz",
        "url": "https://www.fazwaz.com",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "Asoke Towers, Sukhumvit 21",
            "addressLocality": "Khlong Toei Nuea",
            "addressRegion": "Bangkok"
        }
    }
    </script>
</head>
<body>
    <h1>1 Bedroom Condo for Sale at The Line Phahol - Pradipat in Phaya Thai, Bangkok</h1>
    <table>
        <tr><th>Property Type</th><td>Condo</td></tr>
        <tr><th>Bedrooms</th><td>1</td></tr>
        <tr><th>Bathrooms</th><td>1</td></tr>
        <tr><th>Price</th><td>3,790,000 THB</td></tr>
        <tr><th>Status</th><td>Available</td></tr>
    </table>
</body>
</html>
"""

def test_fazwaz_agent_jsonld_not_used_for_listing_name_or_address():
    parser = _parse_html(FAZWAZ_HTML_WITH_AGENT_LD)
    blocks = _json_blocks(parser)
    # The RealEstateAgent block MUST NOT be in blocks
    assert len(blocks) == 0, f"Expected 0 listing blocks, got: {blocks}"

def test_fazwaz_name_extracted_from_title_not_site_name():
    page = FetchedPage(
        url=FAZWAZ_URL,
        final_url=FAZWAZ_URL,
        content_type="text/html",
        text=FAZWAZ_HTML_WITH_AGENT_LD,
        body=FAZWAZ_HTML_WITH_AGENT_LD.encode("utf-8"),
    )
    draft = extract_draft(page)
    name = (draft["provenance"].get("property_name") or {}).get("value")
    # Must NOT be FazWaz
    assert name != "FazWaz"
    assert "The Line Phahol" in name

def test_fazwaz_address_not_fazwaz_office():
    page = FetchedPage(
        url=FAZWAZ_URL,
        final_url=FAZWAZ_URL,
        content_type="text/html",
        text=FAZWAZ_HTML_WITH_AGENT_LD,
        body=FAZWAZ_HTML_WITH_AGENT_LD.encode("utf-8"),
    )
    draft = extract_draft(page)
    address = (draft["provenance"].get("address") or {}).get("value")
    assert address != "Asoke Towers, Sukhumvit 21"
    # Province should be inferred as Bangkok from title/URL
    province = (draft["provenance"].get("province") or {}).get("value")
    assert province == "Bangkok"
    district = (draft["provenance"].get("district") or {}).get("value")
    assert district == "Phaya Thai"

def test_fazwaz_sgd_price_triggers_decision():
    sgd_html = FAZWAZ_HTML_WITH_AGENT_LD.replace("3,790,000 THB", "160,000 SGD")
    page = FetchedPage(
        url=FAZWAZ_URL,
        final_url=FAZWAZ_URL,
        content_type="text/html",
        text=sgd_html,
        body=sgd_html.encode("utf-8"),
    )
    draft = extract_draft(page)
    p_disp = draft["provenance"].get("price_display") or {}
    assert p_disp.get("needs_decision") is True
    assert "SGD" in p_disp.get("reason", "")
    assert "price_numeric_min" not in draft["provenance"]

def test_duplicate_does_not_switch_mode_to_update():
    from app.services.property_web_ingest import draft_from_url

    class DummyStaff:
        email = "admin@example.com"
        role = "super_admin"
        allowed_websites = ["tajlandia.com"]
        selected_websites = ["tajlandia.com"]

    # Even if find_duplicate returns a match, draft_from_url MUST remain mode="create"
    import app.services.property_web_ingest as pwi
    orig_find = pwi.find_duplicate
    orig_fetch = pwi.fetch_public_page
    try:
        pwi.fetch_public_page = lambda u, **k: FetchedPage(
            url=FAZWAZ_URL,
            final_url=FAZWAZ_URL,
            content_type="text/html",
            text=FAZWAZ_HTML_WITH_AGENT_LD,
            body=FAZWAZ_HTML_WITH_AGENT_LD.encode("utf-8"),
        )
        pwi.find_duplicate = lambda db, url, name, allowed_websites=None: {
            "property_id": "fazwaz",
            "name": "FazWaz",
            "store": "Firestore",
        }
        draft = draft_from_url(None, DummyStaff(), FAZWAZ_URL)
        assert draft["mode"] == "create"
        assert draft["property_id"] != "fazwaz"
        assert draft.get("duplicate") is not None
    finally:
        pwi.find_duplicate = orig_find
        pwi.fetch_public_page = orig_fetch

