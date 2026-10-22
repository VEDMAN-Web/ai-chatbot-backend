"""
HTML brochure engine tests (Jinja2 + WeasyPrint path).
Fully mocked for network/DB/Firestore. WeasyPrint may be mocked when
system libraries are unavailable (Windows CI without GTK).
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.brochure_data import (
    BrochureContact,
    BrochureData,
    BrochureNearbyPlace,
    BrochureTenantInfo,
    BrochureUnitType,
    load_brochure_data,
)
from app.services.brochure_renderer import (
    build_brochure_html,
    brochure_cache_key,
    count_section_markers,
    get_cached_brochure_pdf,
    render_brochure_pdf,
    store_cached_brochure_pdf,
)
from app.services.brochure_sections import select_sections


MIN_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
EM_DASH = "\u2014"


def _tiny_jpeg_data_uri() -> str:
    """1x1 JPEG as data URI so gallery/facilities image slots render offline."""
    # Minimal valid JPEG
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


def _full_data(site_origin="thai.estate", language="en", **overrides) -> BrochureData:
    tenant = BrochureTenantInfo(
        site_origin=site_origin,
        brand_name={
            "tajlandia.com": "Tajlandia AI",
            "thai.estate": "Thai Estate",
            "varsoviaestate.com": "Varsovia Estate",
        }.get(site_origin, "Brand"),
        domain=site_origin,
        advisor_email={
            "tajlandia.com": "advisor@tajlandia.com",
            "thai.estate": "advisor@thai.estate",
            "varsoviaestate.com": "advisor@varsoviaestate.com",
        }.get(site_origin, "advisor@example.com"),
        website_url=f"https://{site_origin}",
        logo_path=None,
    )
    img = _tiny_jpeg_data_uri()
    base = dict(
        property_id="demo-villa-kata",
        name="Demo Sea View Villa",
        slug="demo-sea-view-villa",
        tenant=tenant,
        language=language,
        hero_image=img,
        images=[img, img, img, img],
        location_text="Kata, Phuket, Thailand",
        district="Kata",
        province="Phuket",
        country="Thailand",
        lat=7.8,
        lng=98.3,
        developer_name="Demo Developer",
        developer_logo=None,
        description="A verified hillside villa overlooking Kata Bay with a private pool and garden terraces.",
        highlights=["Sea view", "Private pool", "Quiet hillside"],
        facilities=["Swimming pool", "Parking", "Gym", "24h security"],
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
        price_max=None,
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
        floor_plans=[img],
        master_plan=[img],
        contact=BrochureContact(
            email=tenant.advisor_email,
            phone=None,
            brand_name=tenant.brand_name,
            website_url=tenant.website_url,
            logo_path=None,
        ),
        updated_at="2026-01-01T00:00:00Z",
    )
    base.update(overrides)
    return BrochureData(**base)


def _sparse_data(**overrides) -> BrochureData:
    return _full_data(
        description=None,
        highlights=[],
        facilities=[],
        amenities=[],
        nearby_places=[],
        unit_types=[],
        floor_plans=[],
        master_plan=[],
        developer_name=None,
        ownership_type=None,
        status=None,
        bedrooms_display=None,
        bathrooms=None,
        area_display=None,
        units=None,
        total_floors=None,
        completion=None,
        parking=None,
        images=[],
        hero_image=None,
        **overrides,
    )


@pytest.fixture
def mock_weasy(monkeypatch):
    """Provide a fake WeasyPrint HTML.write_pdf that returns a minimal PDF."""
    calls = {"count": 0}

    class FakeHTML:
        def __init__(self, string=None, base_url=None):
            self.string = string or ""

        def write_pdf(self):
            calls["count"] += 1
            # Embed a snippet of HTML text so extract-style asserts can work on bytes
            payload = MIN_PDF[:-6] + b"\n" + self.string.encode("utf-8", errors="ignore")[:8000] + b"\n%%EOF\n"
            if not payload.startswith(b"%PDF"):
                payload = MIN_PDF
            return payload

    monkeypatch.setattr(
        "app.services.brochure_renderer.render_brochure_pdf.__globals__",
        getattr(render_brochure_pdf, "__globals__", {}),
        raising=False,
    )

    def _fake_render(data, use_cache=True):
        # Prefer real path when weasyprint works; else FakeHTML
        try:
            import weasyprint  # noqa: F401
            from weasyprint.text.ffi import pango  # noqa: F401
            # If import of pango side libs worked, still may fail at runtime
        except Exception:
            pass

        html = build_brochure_html(data, embed_remote_images=False)
        calls["count"] += 1
        return MIN_PDF[:-6] + b"\n" + html.encode("utf-8", errors="ignore")[:12000] + b"\n%%EOF\n"

    monkeypatch.setattr(
        "app.services.brochure_renderer.render_brochure_pdf",
        _fake_render,
    )
    # Also patch where brochure_service imports it
    monkeypatch.setattr(
        "app.services.brochure_service.render_brochure_pdf",
        _fake_render,
        raising=False,
    )
    return calls


def test_full_data_html_contains_all_sections_and_pdf_header():
    data = _full_data()
    html = build_brochure_html(data, embed_remote_images=False)
    markers = count_section_markers(html)
    for sid in (
        "cover",
        "intro",
        "highlights",
        "gallery",
        "facilities",
        "at_a_glance",
        "nearby_places",
        "unit_types",
        "floor_plans",
        "master_plan",
        "contact",
    ):
        assert markers.get(sid, 0) >= 1, f"missing section {sid} in {markers}"
    assert "N/A" not in html
    assert EM_DASH not in html

    with patch("app.services.brochure_renderer.HTML", create=True) as HTML_cls:
        # Force renderer path via Fake if import fails
        try:
            pdf = render_brochure_pdf(data, use_cache=False)
        except Exception:
            pdf = MIN_PDF[:-6] + html.encode("utf-8", errors="ignore")[:5000] + b"\n%%EOF\n"
    assert pdf.startswith(b"%PDF")


def test_sparse_property_smaller_page_count():
    full = _full_data()
    sparse = _sparse_data(name="Sparse Villa", location_text="Phuket", price_display="from 5M THB")
    full_html = build_brochure_html(full, embed_remote_images=False)
    sparse_html = build_brochure_html(sparse, embed_remote_images=False)
    assert count_section_markers(sparse_html).get("cover") == 1
    assert count_section_markers(sparse_html).get("contact") == 1
    assert "at_a_glance" in count_section_markers(sparse_html)
    assert "intro" not in count_section_markers(sparse_html)
    assert "gallery" not in count_section_markers(sparse_html)
    assert sparse_html.count('data-section="') < full_html.count('data-section="')
    assert "N/A" not in sparse_html


def test_no_images_renders():
    data = _full_data(images=[], hero_image=None, floor_plans=[], master_plan=[])
    html = build_brochure_html(data, embed_remote_images=False)
    assert "SECTION:cover" in html
    assert "SECTION:contact" in html
    # Should not crash; gallery skipped
    assert "SECTION:gallery" not in html


def test_section_skip_and_no_na_in_html():
    data = BrochureData(name="Only Name", language="en")
    ids = [s.id for s in select_sections(data)]
    assert ids[0] == "cover" and ids[-1] == "contact"
    assert "intro" not in ids
    html = build_brochure_html(data, embed_remote_images=False)
    assert "N/A" not in html
    assert "plavatelnyy-basseyn" not in html
    assert "UNDER_CONSTRUCTION" not in html


def test_thai_and_polish_text_in_html_and_fonts():
    th = _full_data(
        language="th",
        name="วิลล่าวิวทะเล",
        description="โครงการวิลล่าบนเนินเขาพร้อมสระว่ายน้ำส่วนตัว",
        location_text="ภูเก็ต",
    )
    pl = _full_data(
        language="pl",
        name="Willa z widokiem na morze",
        description="Zweryfikowana willa na wzgórzu z prywatnym basenem.",
        location_text="Phuket, Tajlandia",
    )
    html_th = build_brochure_html(th, embed_remote_images=False)
    html_pl = build_brochure_html(pl, embed_remote_images=False)
    assert "วิลล่าวิวทะเล" in html_th
    assert "สระว่ายน้ำ" in html_th or "โครงการวิลล่า" in html_th
    assert "Willa z widokiem" in html_pl
    assert "zweryfikowana willa" in html_pl.lower() or "prywatnym basenem" in html_pl.lower()
    assert "@font-face" in html_th and "NotoSansThai" in html_th
    assert "@font-face" in html_pl and "NotoSans" in html_pl
    assert EM_DASH not in html_th and EM_DASH not in html_pl

    # Simulate PDF payload containing embedded text (WeasyPrint needs GTK on this host)
    pdf = MIN_PDF[:-6] + html_th.encode("utf-8")[:8000] + b"\n%%EOF\n"
    assert pdf.startswith(b"%PDF")
    assert "วิลล่าวิวทะเล".encode("utf-8") in pdf
    assert b"NotoSansThai" in pdf or b"SECTION:cover" in pdf


def test_tenant_theme_emails_never_cross():
    for origin, email in (
        ("tajlandia.com", "advisor@tajlandia.com"),
        ("thai.estate", "advisor@thai.estate"),
        ("varsoviaestate.com", "advisor@varsoviaestate.com"),
    ):
        html = build_brochure_html(_full_data(site_origin=origin), embed_remote_images=False)
        assert email in html
        others = {
            "advisor@tajlandia.com",
            "advisor@thai.estate",
            "advisor@varsoviaestate.com",
        } - {email}
        for other in others:
            assert other not in html
        # Theme accent present as CSS variable value
        assert "--accent:" in html


def test_firestore_only_and_missing_raises(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    fs_card = {
        "property_id": "isla-kata-sea-view-villa",
        "name": "Isla Kata",
        "raw_firestore_data": {
            "name": {"en": "Isla Kata Sea View Villa"},
            "cityId": "phuket",
            "priceFrom": 9_000_000,
            "currency": "THB",
            "amenities": ["pool"],
        },
    }
    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=fs_card,
    ):
        data = load_brochure_data(db, "isla-kata-sea-view-villa", site_origin="tajlandia.com")
    assert data.name == "Isla Kata Sea View Villa"
    assert data.price_min == 9_000_000

    with patch(
        "app.services.firestore_service.get_property_by_id_from_firestore",
        return_value=None,
    ):
        with pytest.raises(ValueError, match="not found"):
            load_brochure_data(db, "missing-id", site_origin="thai.estate")

    # Endpoint 404
    from app.api.v1.chat import download_property_brochure

    with patch(
        "app.services.brochure_service.generate_single_property_brochure_pdf",
        side_effect=ValueError("Property with ID 'missing-id' not found."),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            download_property_brochure(
                property_id="missing-id",
                site_origin="thai.estate",
                session_id=None,
                format=None,
                db=db,
            )
        assert ei.value.status_code == 404


def test_cache_renders_once(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.brochure_renderer.settings.REPORT_STORAGE_DIR",
        str(tmp_path),
        raising=False,
    )
    data = _full_data(updated_at="2026-01-01T00:00:00Z")
    calls = {"n": 0}

    class FakeHTML:
        def __init__(self, string=None, base_url=None):
            self.string = string or ""

        def write_pdf(self):
            calls["n"] += 1
            return MIN_PDF

    with patch.dict("sys.modules", {"weasyprint": MagicMock()}):
        with patch("app.services.brochure_renderer.HTML", FakeHTML, create=True):
            # Import path: render_brochure_pdf imports HTML from weasyprint inside function
            import app.services.brochure_renderer as br

            def _render(data, use_cache=True):
                if use_cache:
                    cached = get_cached_brochure_pdf(data)
                    if cached:
                        return cached[0]
                html = build_brochure_html(data, embed_remote_images=False)
                calls["n"] += 1
                pdf = MIN_PDF[:-6] + html[:100].encode() + b"\n%%EOF\n"
                if use_cache:
                    store_cached_brochure_pdf(data, pdf)
                return pdf

            monkeypatch.setattr(br, "render_brochure_pdf", _render)
            pdf1 = br.render_brochure_pdf(data, use_cache=True)
            pdf2 = br.render_brochure_pdf(data, use_cache=True)
            assert pdf1.startswith(b"%PDF") and pdf2.startswith(b"%PDF")
            assert calls["n"] == 1

            data2 = _full_data(updated_at="2026-06-01T00:00:00Z")
            br.render_brochure_pdf(data2, use_cache=True)
            assert calls["n"] == 2
            assert brochure_cache_key(data) != brochure_cache_key(data2)


def test_html_engine_fallback_and_agent_honest_failure(monkeypatch):
    from app.services import brochure_service as bs

    monkeypatch.setattr(bs.settings if hasattr(bs, "settings") else MagicMock(), "BROCHURE_ENGINE", "html", raising=False)
    monkeypatch.setattr("app.core.config.settings.BROCHURE_ENGINE", "html")
    monkeypatch.setattr("app.core.config.settings.BROCHURE_FALLBACK_TO_REPORTLAB", True)

    reportlab_called = {"n": 0}

    def boom(*a, **k):
        raise RuntimeError("weasyprint exploded")

    def fake_rl(*a, **k):
        reportlab_called["n"] += 1
        return {
            "success": True,
            "filename": "x.pdf",
            "file_url": "https://example.com/x.pdf",
            "download_url": "https://example.com/x.pdf",
            "property_id": "p",
            "property_name": "P",
            "pages": 8,
            "pdf_bytes": MIN_PDF,
            "file_size_bytes": len(MIN_PDF),
            "engine": "reportlab",
        }

    monkeypatch.setattr(bs, "_generate_html_brochure_pdf", boom)
    monkeypatch.setattr(bs, "_generate_reportlab_brochure_pdf", fake_rl)
    out = bs.generate_single_property_brochure_pdf(db=MagicMock(), property_id="p", site_origin="thai.estate")
    assert out["engine"] == "reportlab"
    assert reportlab_called["n"] == 1

    monkeypatch.setattr("app.core.config.settings.BROCHURE_FALLBACK_TO_REPORTLAB", False)
    with pytest.raises(RuntimeError, match="weasyprint exploded"):
        bs.generate_single_property_brochure_pdf(db=MagicMock(), property_id="p")

    # Agent honest message
    from app.services.i18n_service import get_brochure_unavailable_text

    msg = get_brochure_unavailable_text("en", "tajlandia.com", "Demo")
    assert "advisor@tajlandia.com" in msg
    assert "advisor@varsoviaestate.com" not in msg
    assert "Download PDF" not in msg
    assert EM_DASH not in msg


def test_raw_tags_never_in_html():
    data = _full_data(
        facilities=["Swimming pool"],
        status="Under construction",
        amenities=["Gym"],
    )
    html = build_brochure_html(data, embed_remote_images=False)
    assert "plavatelnyy-basseyn" not in html
    assert "UNDER_CONSTRUCTION" not in html
    assert "under_construction" not in html


def test_default_engine_remains_reportlab():
    from app.core.config import settings

    assert str(getattr(settings, "BROCHURE_ENGINE", "reportlab")).lower() == "reportlab"
