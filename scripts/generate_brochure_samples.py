"""
Generate sample brochures for the three tenants into data/brochure_samples/.
Uses the HTML template pipeline. Prefers WeasyPrint; falls back to Chrome
headless print-to-PDF when WeasyPrint system libs are missing (Windows).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.brochure_data import (  # noqa: E402
    BrochureContact,
    BrochureData,
    BrochureNearbyPlace,
    BrochureTenantInfo,
    BrochureUnitType,
)
from app.services.brochure_renderer import build_brochure_html, render_brochure_pdf  # noqa: E402
from app.services.offer_dossier_service import resolve_logo_path  # noqa: E402

OUT = ROOT / "data" / "brochure_samples"
OUT.mkdir(parents=True, exist_ok=True)

TENANTS = [
    ("tajlandia.com", "Tajlandia AI", "advisor@tajlandia.com", "https://tajlandia.com"),
    ("thai.estate", "Thai Estate", "advisor@thai.estate", "https://thai.estate"),
    ("varsoviaestate.com", "Varsovia Estate", "advisor@varsoviaestate.com", "https://varsoviaestate.com/en"),
]


def _sample(site_origin: str, brand: str, email: str, website: str) -> BrochureData:
    logo = resolve_logo_path(brand)
    tenant = BrochureTenantInfo(
        site_origin=site_origin,
        brand_name=brand,
        domain=site_origin,
        advisor_email=email,
        website_url=website,
        logo_path=logo,
    )
    return BrochureData(
        property_id="sample-kata-sea-view",
        name="Sample Kata Sea View Villa",
        tenant=tenant,
        language="en",
        location_text="Kata, Phuket, Thailand",
        district="Kata",
        province="Phuket",
        country="Thailand",
        developer_name="Sample Developer Co.",
        description=(
            "A hillside villa overlooking Kata Bay with a private pool, "
            "garden terraces and open-plan living. All facts shown here are "
            "sample placeholders for layout review only."
        ),
        highlights=["Sea view", "Private pool", "Quiet hillside setting"],
        facilities=["Swimming pool", "Parking", "Gym", "24h security", "Garden"],
        amenities=["Sea view", "Garden"],
        nearby_places=[
            BrochureNearbyPlace(category="Beaches", name="Kata Beach", distance_text="5 min"),
            BrochureNearbyPlace(category="Airport", name="Phuket Airport", distance_text="45 min"),
        ],
        property_type="Villa",
        status="Under construction",
        ownership_type="Leasehold",
        price_display="from 12,500,000 THB",
        price_min=12_500_000,
        currency="THB",
        bedrooms_display="3",
        bathrooms=3,
        area_display="280 sqm",
        units=12,
        total_floors=2,
        completion="Q4 2027",
        parking="2 cars",
        unit_types=[
            BrochureUnitType(name="Type A", size_text="280 sqm", beds=3, baths=3, units=6),
            BrochureUnitType(name="Type B", size_text="320 sqm", beds=4, baths=4, units=6),
        ],
        contact=BrochureContact(
            email=email,
            brand_name=brand,
            website_url=website,
            logo_path=logo,
        ),
        updated_at="2026-10-16T00:00:00Z",
        images=[],
        hero_image=None,
        floor_plans=[],
        master_plan=[],
    )


def _chrome_print(html_path: Path, pdf_path: Path) -> bool:
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file():
        chrome = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not chrome.is_file():
        return False
    # Chrome needs absolute file URL
    uri = html_path.resolve().as_uri()
    cmd = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        f"--print-to-pdf={pdf_path.resolve()}",
        "--no-pdf-header-footer",
        uri,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=90, capture_output=True)
        return pdf_path.is_file() and pdf_path.stat().st_size > 500
    except Exception as e:
        print(f"Chrome print failed: {e}")
        return False


def main() -> None:
    for site, brand, email, website in TENANTS:
        data = _sample(site, brand, email, website)
        slug = site.replace(".", "_")
        html = build_brochure_html(data, embed_remote_images=False)
        html_path = OUT / f"brochure_{slug}.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"Wrote {html_path}")

        pdf_path = OUT / f"brochure_{slug}.pdf"
        ok = False
        try:
            pdf_bytes = render_brochure_pdf(data, use_cache=False)
            pdf_path.write_bytes(pdf_bytes)
            ok = True
            print(f"Wrote {pdf_path} via WeasyPrint ({len(pdf_bytes)} bytes)")
        except Exception as e:
            print(f"WeasyPrint unavailable ({e}); trying Chrome headless...")
            # Wrap pages for screen print: Chrome ignores @page size sometimes;
            # still useful for a viewable PDF.
            if _chrome_print(html_path, pdf_path):
                ok = True
                print(f"Wrote {pdf_path} via Chrome headless")
        if not ok:
            print(f"WARNING: could not write PDF for {site}; open the HTML file instead.")


if __name__ == "__main__":
    main()
