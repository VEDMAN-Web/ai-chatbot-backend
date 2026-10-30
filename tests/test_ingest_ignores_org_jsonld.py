"""The site's own Organization JSON-LD must not become the listing's name or address."""

from __future__ import annotations

import json

from app.services.property_web_ingest import _json_blocks, _parse_html


def test_organization_block_is_not_treated_as_the_listing():
    ld = {"@graph": [
        {"@type": "Organization", "name": "Portal Realty Services Limited", "address": {"addressLocality": "Noida"}},
        {"@type": "BreadcrumbList", "name": "Crumbs"},
        {"@type": "Product", "name": "2 BHK Apartment in Vasna"},
    ]}
    html = f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'
    names = [b.get("name") for b in _json_blocks(_parse_html(html))]
    assert names == ["2 BHK Apartment in Vasna"]


def test_status_is_taken_from_explicit_page_wording():
    from app.services.property_web_ingest import _apply_guesses

    fields: dict = {}
    _apply_guesses(fields, "2 BHK Apartment. Furnished. Ready to Move. 8th floor")
    assert fields["status"]["value"] == "Ready to move in"
    fields = {}
    _apply_guesses(fields, "Nice flat with a balcony")
    assert "status" not in fields
