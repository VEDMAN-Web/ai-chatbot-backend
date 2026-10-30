"""
User-side brochure LLM copy (Claude Sonnet) — fake Anthropic client, no network.
Does not change OPENAI_MODEL / public chat behaviour.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core import config as config_mod
from app.services.brochure_copywriter import (
    BrochureCopy,
    ai_footer_note,
    build_copy_input,
    clear_copy_cache,
    collect_allowed_tokens,
    write_brochure_copy,
)
from app.services.brochure_data import BrochureData, BrochureNearbyPlace, BrochureUnitType
from app.services.brochure_renderer import build_brochure_html


def _sample_data(**overrides) -> BrochureData:
    base = dict(
        property_id="demo-villa-kata",
        name="Demo Sea View Villa",
        language="en",
        location_text="Kata, Phuket, Thailand",
        district="Kata",
        province="Phuket",
        country="Thailand",
        property_type="Villa",
        status="Under construction",
        ownership_type="Leasehold",
        price_display="from 12,500,000 THB",
        price_min=12_500_000,
        currency="THB",
        bedrooms_display="3",
        bathrooms=3,
        area_display="280 sqm",
        completion="Q4 2027",
        description="A hillside villa overlooking Kata Bay with a private pool.",
        amenities=["Sea view", "Garden"],
        facilities=["Swimming pool", "Parking"],
        highlights=["Sea view", "Private pool"],
        nearby_places=[
            BrochureNearbyPlace(category="Beaches", name="Kata Beach", distance_text="5 min"),
        ],
        unit_types=[
            BrochureUnitType(name="Type A", size_text="280 sqm", beds=3, baths=3, units=6),
        ],
        investment_info=None,
        developer_name="Demo Developer",
    )
    base.update(overrides)
    return BrochureData(**base)


def _valid_json(lang: str = "en") -> str:
    if lang == "pl":
        return json.dumps({
            "headline": "Willa z widokiem na Kata",
            "intro": "Elegancka willa w Kata na Phuket z prywatnym basenem i widokiem na morze.",
            "highlights": ["Widok na morze", "Prywatny basen", "Lokalizacja Kata"],
            "location_blurb": "Położona w Kata, Phuket, blisko plaży Kata Beach.",
            "lifestyle_blurb": "Spokojne wzgórze z ogrodem i parkingiem.",
            "investment_blurb": "",
            "closing_line": "Zapraszamy do kontaktu z doradcą.",
        }, ensure_ascii=False)
    if lang == "th":
        return json.dumps({
            "headline": "วิลล่าวิวทะเลคาตา",
            "intro": "วิลล่าบนเนินเขาที่คาตา ภูเก็ต พร้อมสระว่ายน้ำส่วนตัว",
            "highlights": ["วิวทะเล", "สระว่ายน้ำส่วนตัว", "ทำเลคาตา"],
            "location_blurb": "ตั้งอยู่ที่คาตา ภูเก็ต ใกล้หาดคาตา",
            "lifestyle_blurb": "บรรยากาศเงียบสงบพร้อมสวนและที่จอดรถ",
            "investment_blurb": "",
            "closing_line": "ติดต่อทีมที่ปรึกษาของเรา",
        }, ensure_ascii=False)
    return json.dumps({
        "headline": "Hillside villa above Kata Bay",
        "intro": "A hillside villa in Kata, Phuket with a private pool and sea view.",
        "highlights": ["Sea view", "Private pool", "Kata location"],
        "location_blurb": "Set in Kata, Phuket near Kata Beach.",
        "lifestyle_blurb": "Quiet hillside living with garden and parking.",
        "investment_blurb": "",
        "closing_line": "Speak with our advisory team to learn more.",
    })


class _FakeMsg:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(type="text", text=text)]


class _FakeAnthropic:
    def __init__(self, text: str, *, fail_first: bool = False):
        self._text = text
        self.calls = 0
        self.models: list = []
        self._fail_first = fail_first
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        self.models.append(kwargs.get("model"))
        if self._fail_first and self.calls == 1:
            raise ConnectionError("connection reset")
        return _FakeMsg(self._text)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_copy_cache()
    yield
    clear_copy_cache()


def test_build_copy_input_whitelist_no_secrets():
    data = _sample_data()
    # Attach contact-like fields that must never appear
    data.contact = None
    payload = build_copy_input(data)
    blob = json.dumps(payload).lower()
    assert "email" not in blob
    assert "phone" not in blob
    assert "advisor@" not in blob
    assert "internal" not in blob
    assert "raw_metadata" not in blob
    assert payload.get("name") == "Demo Sea View Villa"
    assert payload.get("price_display")
    assert "property_id" not in payload  # raw ids excluded


def test_happy_path_en_pl_th_used_in_html(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(config_mod.settings, "OPENAI_MODEL", "gpt-5.5")

    for lang in ("en", "pl", "th"):
        clear_copy_cache()
        fake = _FakeAnthropic(_valid_json(lang))
        with patch("anthropic.Anthropic", return_value=fake):
            data = _sample_data(language=lang)
            copy = write_brochure_copy(data, lang)
        assert copy is not None
        assert copy.headline
        assert copy.intro
        html = build_brochure_html(data, embed_remote_images=False, copy=copy, ai_footer=ai_footer_note(lang))
        assert copy.headline in html or copy.intro in html
        assert "AI" in html or "AI" in ai_footer_note(lang) or "zweryfikowanych" in html or "ยืนยัน" in html
        assert fake.models[0] == "claude-sonnet-5-5"
    # Public chat model setting unchanged
    assert config_mod.settings.OPENAI_MODEL == "gpt-5.5"


def test_hallucinated_number_dropped(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    bad = json.dumps({
        "headline": "8% rental yield villa",
        "intro": "This villa offers 8% ROI and costs 99,000,000 THB.",
        "highlights": ["8% yield", "Private pool", "Sea view"],
        "location_blurb": "In Kata, Phuket.",
        "lifestyle_blurb": "Quiet hillside.",
        "investment_blurb": "Guaranteed 8% return.",
        "closing_line": "Call now.",
    })
    fake = _FakeAnthropic(bad)
    with patch("anthropic.Anthropic", return_value=fake):
        copy = write_brochure_copy(_sample_data(), "en")
    # Unsupported numbers / banned → copy dropped or cleaned without inventing
    if copy is None:
        assert True
    else:
        blob = json.dumps(copy.to_dict()).lower()
        assert "8%" not in blob
        assert "99000000" not in blob.replace(",", "")
        assert "guaranteed" not in blob


def test_banned_phrase_and_other_property_removed(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    bad = json.dumps({
        "headline": "Best price limited time offer",
        "intro": "Risk-free purchase near Zorblax Towers with tax-free ownership.",
        "highlights": ["Best price", "Private pool", "Sea view"],
        "location_blurb": "Kata, Phuket.",
        "lifestyle_blurb": "Garden living.",
        "investment_blurb": "",
        "closing_line": "Only 2 left.",
    })
    fake = _FakeAnthropic(bad)
    with patch("anthropic.Anthropic", return_value=fake):
        with patch(
            "app.services.brochure_copywriter._mentions_other_property",
            side_effect=lambda text, own: "zorblax" in (text or "").lower(),
        ):
            copy = write_brochure_copy(_sample_data(), "en")
    if copy is None:
        return
    blob = json.dumps(copy.to_dict()).lower()
    assert "guaranteed" not in blob
    assert "best price" not in blob
    assert "limited time" not in blob
    assert "tax-free" not in blob and "tax free" not in blob
    assert "zorblax" not in blob
    assert "only 2 left" not in blob


def test_fallback_when_disabled_or_invalid(monkeypatch):
    data = _sample_data()
    # Pin theme so random visual selection does not break HTML equality
    det_html = build_brochure_html(data, embed_remote_images=False, theme_id=1)

    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", False)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    assert write_brochure_copy(data, "en") is None

    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "")
    assert write_brochure_copy(data, "en") is None

    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    fake = _FakeAnthropic("not-json{{{")
    with patch("anthropic.Anthropic", return_value=fake):
        assert write_brochure_copy(data, "en") is None

    # Deterministic HTML unchanged when no copy (same theme)
    assert build_brochure_html(data, embed_remote_images=False, theme_id=1) == det_html


def test_cache_hit_and_price_change_busts(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_CACHE_TTL_HOURS", 24)
    fake = _FakeAnthropic(_valid_json("en"))
    data = _sample_data()
    with patch("anthropic.Anthropic", return_value=fake):
        c1 = write_brochure_copy(data, "en")
        c2 = write_brochure_copy(data, "en")
    assert c1 is not None and c2 is not None
    assert fake.calls == 1

    data2 = _sample_data(price_display="from 15,000,000 THB", price_min=15_000_000)
    fake2 = _FakeAnthropic(_valid_json("en"))
    with patch("anthropic.Anthropic", return_value=fake2):
        c3 = write_brochure_copy(data2, "en")
    assert c3 is not None
    assert fake2.calls == 1


def test_collect_allowed_tokens_includes_millions():
    tokens = collect_allowed_tokens({"price_display": "from 12,500,000 THB", "price_min": 12_500_000})
    assert "12500000" in tokens or any("12500000" in t for t in tokens)
    assert any(t.endswith("m") for t in tokens)


def test_model_id_is_brochure_setting(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(config_mod.settings, "OPENAI_MODEL", "gpt-5.5-keep")
    fake = _FakeAnthropic(_valid_json("en"))
    with patch("anthropic.Anthropic", return_value=fake):
        write_brochure_copy(_sample_data(), "en")
    assert fake.models == ["claude-sonnet-5-5"]
    assert config_mod.settings.OPENAI_MODEL == "gpt-5.5-keep"


def test_html_pipeline_uses_copy_and_fallback_pdf(monkeypatch):
    from app.services import brochure_service as bs

    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_ENABLED", True)
    monkeypatch.setattr(config_mod.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config_mod.settings, "BROCHURE_ENGINE", "html")
    monkeypatch.setattr(config_mod.settings, "BROCHURE_LLM_FOOTER_NOTE", True)

    data = _sample_data()
    copy = BrochureCopy(
        headline="Hillside villa above Kata Bay",
        intro="A hillside villa in Kata, Phuket with a private pool.",
        highlights=["Sea view", "Private pool", "Kata location"],
        location_blurb="Set in Kata near Kata Beach.",
        lifestyle_blurb="Quiet hillside living.",
        investment_blurb="",
        closing_line="Speak with our advisory team.",
    )

    monkeypatch.setattr(bs, "_write_brochure_copy_timed", lambda d, lang: (copy, "sonnet", 12))
    monkeypatch.setattr(
        "app.services.brochure_data.load_brochure_data",
        lambda **k: data,
    )
    monkeypatch.setattr(
        "app.services.brochure_renderer.render_brochure_pdf",
        lambda d, **k: b"%PDF-1.4 fake",
    )
    monkeypatch.setattr(
        bs.default_storage_provider,
        "save_file",
        lambda *a, **k: "https://example.com/brochure.pdf",
    )

    res = bs._generate_html_brochure_pdf(db=MagicMock(), property_id="demo-villa-kata", language="en")
    assert res["success"] is True
    assert res["brochure_copy_source"] == "sonnet"
    assert res["pdf_bytes"].startswith(b"%PDF")


def test_config_status_endpoint_labels_only():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.auth import AuthenticatedStaff, require_admin_or_super_admin

    def _fake_staff():
        return AuthenticatedStaff(
            user_id="u1",
            email="admin@test.com",
            role="super_admin",
            allowed_websites=["tajlandia.com"],
        )

    app.dependency_overrides[require_admin_or_super_admin] = _fake_staff
    try:
        client = TestClient(app)
        with patch.object(config_mod.settings, "BROCHURE_LLM_ENABLED", True), \
             patch.object(config_mod.settings, "ANTHROPIC_API_KEY", "secret-should-not-leak"), \
             patch.object(config_mod.settings, "BROCHURE_LLM_MODEL", "claude-sonnet-5-5"):
            r = client.get("/api/v1/admin/system/config-status")
        assert r.status_code == 200
        body = r.json()
        assert body["brochure_llm"] in ("on", "off")
        assert body["brochure_llm_model"] == "claude-sonnet-5-5"
        blob = json.dumps(body)
        assert "secret-should-not-leak" not in blob
        assert "ANTHROPIC_API_KEY" not in blob
    finally:
        app.dependency_overrides.pop(require_admin_or_super_admin, None)
