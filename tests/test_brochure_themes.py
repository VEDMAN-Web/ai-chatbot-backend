"""
Tests for brochure visual themes (pick_theme, HTML render, fallback, filenames).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.brochure_data import (
    BrochureContact,
    BrochureData,
    BrochureNearbyPlace,
    BrochureTenantInfo,
    BrochureUnitType,
)
from app.services.brochure_renderer import build_brochure_html, count_section_markers
from app.services.brochure_themes import (
    THEME_IDS,
    THEMES,
    clear_theme_memory,
    pick_theme,
)


MIN_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _tiny_jpeg_data_uri() -> str:
    raw = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x7f\xff\xd9"
    )
    import base64

    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def _full_property(**overrides) -> BrochureData:
    img = _tiny_jpeg_data_uri()
    tenant = BrochureTenantInfo(
        site_origin="thai.estate",
        brand_name="Thai Estate",
        domain="thai.estate",
        advisor_email="advisor@thai.estate",
        website_url="https://thai.estate",
    )
    base = dict(
        property_id="theme-demo-villa",
        name="Theme Demo Villa",
        tenant=tenant,
        language="en",
        hero_image=img,
        images=[img, img, img, img],
        location_text="Kata, Phuket",
        description="A verified hillside villa overlooking Kata Bay.",
        highlights=["Sea view", "Private pool", "Quiet hillside"],
        facilities=["Swimming pool", "Parking"],
        amenities=["Gym", "Garden"],
        nearby_places=[
            BrochureNearbyPlace(category="Beaches", name="Kata Beach", distance_text="5 min"),
        ],
        status="Under construction",
        ownership_type="Leasehold",
        price_display="from 12,500,000 THB",
        area_display="280 sqm",
        bedrooms_display="3",
        unit_types=[BrochureUnitType(name="Type A", size_text="280 sqm", beds=3, baths=3)],
        floor_plans=[img],
        investment_info="Guaranteed rental programme documented in listing.",
        contact=BrochureContact(
            email="advisor@thai.estate",
            brand_name="Thai Estate",
            website_url="https://thai.estate",
        ),
    )
    base.update(overrides)
    return BrochureData(**base)


def _minimal_property(**overrides) -> BrochureData:
    return _full_property(
        description=None,
        highlights=[],
        facilities=[],
        amenities=[],
        nearby_places=[],
        unit_types=[],
        floor_plans=[],
        images=[],
        hero_image=None,
        investment_info=None,
        ownership_type=None,
        status=None,
        area_display=None,
        bedrooms_display=None,
        price_display="from 5,000,000 THB",
        location_text="Phuket",
        **overrides,
    )


@pytest.fixture(autouse=True)
def _clear_themes():
    clear_theme_memory()
    yield
    clear_theme_memory()


def test_pick_theme_range_explicit_and_avoid_repeat():
    assert pick_theme(requested=3) == 3
    assert pick_theme(requested=99) in THEME_IDS  # invalid -> random
    clear_theme_memory()
    first = pick_theme(session_key="sess-a")
    second = pick_theme(session_key="sess-a")
    # Soft guarantee: when possible, second differs from first
    assert first in THEME_IDS and second in THEME_IDS
    # Over many picks with same key, avoid immediate repeat often
    picks = [pick_theme(session_key="sess-b") for _ in range(20)]
    assert all(p in THEME_IDS for p in picks)
    # Immediate repeats should be rare with exclude-last behaviour
    immediate = sum(1 for i in range(1, len(picks)) if picks[i] == picks[i - 1])
    assert immediate <= 2


def test_pick_theme_uses_all_seven_over_200_calls():
    clear_theme_memory()
    seen = {pick_theme(session_key=f"k{i}") for i in range(200)}
    assert seen == set(THEME_IDS)


def test_each_theme_renders_html_full_minimal_thai():
    full = _full_property()
    minimal = _minimal_property()
    thai = _full_property(language="th", name="วิลล่าทดสอบ", description="คำอธิบายโครงการที่ตรวจสอบแล้ว")

    for theme_id in THEME_IDS:
        for data in (full, minimal, thai):
            html = build_brochure_html(data, embed_remote_images=False, theme_id=theme_id)
            assert "SECTION:cover" in html
            assert "SECTION:contact" in html
            assert f"theme-{theme_id}" in html
            # Strip embedded images so base64 cannot false-positive on 'None'/'NaN'
            visible = re.sub(r"data:[^\"']+", "", html)
            # No wireframe placeholders like [PRICE] / [AREA]
            assert not re.search(r"\[[A-Z][A-Z_ ]+\]", visible)
            for tok in ("None", "N/A", "NaN"):
                assert tok not in visible
            assert not re.search(r"\bnan\b", visible, flags=re.I)
            # Page numbering present
            assert re.search(r"\d{2} / \d+", html)
            markers = count_section_markers(html)
            page_count = sum(markers.values())
            assert 2 <= page_count <= 8


def test_skipped_investment_changes_page_total():
    with_inv = _full_property()
    without = _full_property(investment_info=None)
    html_a = build_brochure_html(with_inv, embed_remote_images=False, theme_id=1)
    html_b = build_brochure_html(without, embed_remote_images=False, theme_id=1)
    assert "SECTION:investment" in html_a
    assert "SECTION:investment" not in html_b
    # Totals differ in footer labels
    tot_a = max(int(m) for m in re.findall(r"\d{2} / (\d+)", html_a))
    tot_b = max(int(m) for m in re.findall(r"\d{2} / (\d+)", html_b))
    assert tot_a == tot_b + 1


def test_theme_failure_falls_back(monkeypatch):
    from app.services import brochure_renderer as br

    data = _full_property()
    calls = {"themes": []}

    real_build = br.build_brochure_html

    def flaky_build(data, **kwargs):
        tid = kwargs.get("theme_id")
        calls["themes"].append(tid)
        if tid == 1 and not kwargs.get("use_legacy_base"):
            raise RuntimeError("theme 1 template boom")
        return real_build(data, **kwargs)

    monkeypatch.setattr(br, "build_brochure_html", flaky_build)

    class FakeHTML:
        def __init__(self, string=None, base_url=None):
            self.string = string or ""

        def write_pdf(self):
            return MIN_PDF[:-6] + self.string.encode("utf-8", errors="ignore")[:2000] + b"\n%%EOF\n"

    with patch.dict("sys.modules", {"weasyprint": MagicMock()}):
        with patch.object(br, "_weasyprint_html_to_pdf", side_effect=lambda html: FakeHTML(html).write_pdf()):
            # Force start at theme 1 so first attempt fails
            pdf = br.render_brochure_pdf(data, use_cache=False, theme_id=1)
    assert pdf.startswith(b"%PDF")
    assert 1 in calls["themes"]
    assert any(t != 1 for t in calls["themes"])


def test_consecutive_html_requests_different_filenames(monkeypatch):
    from app.services import brochure_service as bs

    monkeypatch.setattr("app.core.config.settings.BROCHURE_ENGINE", "html")
    monkeypatch.setattr("app.core.config.settings.BROCHURE_FALLBACK_TO_REPORTLAB", False)

    data = _full_property()
    monkeypatch.setattr(
        "app.services.brochure_data.load_brochure_data",
        lambda **k: data,
    )
    monkeypatch.setattr(
        bs,
        "_write_brochure_copy_timed",
        lambda *a, **k: (None, "disabled", 0),
    )
    monkeypatch.setattr(
        "app.services.brochure_renderer.render_brochure_pdf",
        lambda *a, **k: MIN_PDF,
    )
    saved = []

    def fake_save(filename, content, content_type=None):
        saved.append(filename)
        return f"https://example.com/{filename}"

    monkeypatch.setattr(bs.default_storage_provider, "save_file", fake_save)

    out1 = bs._generate_html_brochure_pdf(
        db=MagicMock(), property_id="theme-demo-villa", template=2, session_key="s1"
    )
    out2 = bs._generate_html_brochure_pdf(
        db=MagicMock(), property_id="theme-demo-villa", template=2, session_key="s1"
    )
    assert out1["filename"] != out2["filename"]
    assert "_t2_" in out1["filename"] and out1["filename"].startswith("brochure_")
    assert out1["theme_id"] == 2
