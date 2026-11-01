"""Unit tests for the text-free property photo extractor (Claude calls are mocked)."""
import io

import pytest
from PIL import Image, ImageDraw

from app.services import property_photo_extractor as ppe


def _photo(w=1200, h=800, seed=0):
    """A busy, non-flat synthetic 'photo'."""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for i in range(0, w, 40):
        d.rectangle([i, 0, i + 20, h], fill=((i * 7 + seed) % 255, (i * 3) % 255, (i * 11 + seed * 5) % 255))
    for j in range(0, h, 30):
        d.line([0, j, w, j + 50], fill=((j * 5) % 255, 120, (j + seed) % 255), width=6)
    return img


def _page_with_photo(margin=150):
    page = Image.new("RGB", (1200 + 2 * margin, 800 + 2 * margin), (250, 248, 244))
    page.paste(_photo(), (margin, margin))
    return page


def _ok_verdict(kind="exterior"):
    return {"is_property_photo": True, "contains_text": False, "text_found": "", "has_border_or_background": False,
            "is_collage": False, "quality_ok": True, "kind": kind, "description": "Tower facade",
            "alt_text": "Tower facade", "clean_box": None}


# ---- pixel helpers ---------------------------------------------------------------------------------------------

def test_trim_flat_margins_removes_page_background():
    page = _page_with_photo(margin=60)
    trimmed = ppe.trim_flat_margins(page)
    assert abs(trimmed.width - 1200) <= 4 and abs(trimmed.height - 800) <= 4


def test_trim_flat_margins_keeps_full_photo():
    img = _photo()
    assert ppe.trim_flat_margins(img).size == img.size


def test_is_flat_detects_blank_tiles():
    assert ppe._is_flat(Image.new("RGB", (800, 600), (200, 200, 200)))
    assert not ppe._is_flat(_photo())


def test_dhash_matches_rescaled_copy_and_differs_from_other_photo():
    a = _photo()
    b = a.resize((600, 400))
    c = _photo(seed=97).rotate(90, expand=True).resize((1200, 800))
    assert ppe._hamming(ppe.dhash(a), ppe.dhash(b)) <= ppe.DUPLICATE_HAMMING
    assert ppe._hamming(ppe.dhash(a), ppe.dhash(c)) > ppe.DUPLICATE_HAMMING


def test_box_to_full_scales_and_clamps():
    assert ppe._box_to_full({"x0": 10, "y0": 20, "x1": 110, "y1": 220}, 0.5, (1000, 1000)) == (20, 40, 220, 440)
    assert ppe._box_to_full({"x0": -5, "y0": 0, "x1": 9999, "y1": 300}, 1.0, (800, 600)) == (0, 0, 800, 300)
    assert ppe._box_to_full({"x0": 5, "y0": 5, "x1": 10, "y1": 10}, 1.0, (800, 600)) is None
    assert ppe._box_to_full({"x0": 5}, 1.0, (800, 600)) is None


def test_shape_rules_reject_strips_and_small_crops():
    assert ppe._shape_ok(1200, 800)
    assert not ppe._shape_ok(2000, 300)     # banner strip
    assert not ppe._shape_ok(300, 200)      # too small


# ---- gate ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("override", [
    {"contains_text": True}, {"is_property_photo": False}, {"is_collage": True},
    {"has_border_or_background": True}, {"quality_ok": False}, {"kind": "not_a_photo"},
])
def test_passes_requires_every_condition(override):
    assert ppe._passes(_ok_verdict())
    assert not ppe._passes({**_ok_verdict(), **override})


def test_fails_closed_without_api_key(monkeypatch):
    monkeypatch.setattr(ppe.settings, "ANTHROPIC_API_KEY", "")
    rep = ppe.extract_photos_from_candidates([ppe.Candidate(_photo(), "x")])
    assert rep.photos == [] and rep.errors


def test_pipeline_crops_verifies_and_dedupes(monkeypatch):
    monkeypatch.setattr(ppe.settings, "ANTHROPIC_API_KEY", "test-key")
    page = _page_with_photo()
    # The same photo appears on two pages; one page also has a text-only region the verifier rejects.
    monkeypatch.setattr(ppe, "detect_regions", lambda img, name="": ("mixed_page", [
        {"x0": 150, "y0": 150, "x1": 1350, "y1": 950, "kind": "exterior", "description": "facade"},
    ], 1.0))
    monkeypatch.setattr(ppe, "verify_photo", lambda img, name="": (_ok_verdict(), 1.0))
    rep = ppe.extract_photos_from_candidates([ppe.Candidate(page, "p1"), ppe.Candidate(page.copy(), "p2")])
    assert len(rep.photos) == 1
    p = rep.photos[0]
    assert abs(p.width - 1200) <= 4 and abs(p.height - 800) <= 4
    assert Image.open(io.BytesIO(p.bytes)).format == "JPEG"
    assert any(r["reason"] == "duplicate of a kept photo" for r in rep.rejected)


def test_text_in_crop_is_refined_once_then_accepted(monkeypatch):
    monkeypatch.setattr(ppe.settings, "ANTHROPIC_API_KEY", "test-key")
    calls = []

    def verify(img, name=""):
        calls.append(img.size)
        if len(calls) == 1:   # first look: caption along the bottom, suggest cropping it off
            return ({**_ok_verdict(), "contains_text": True, "text_found": "LOBBY",
                     "clean_box": {"x0": 0, "y0": 0, "x1": img.width, "y1": int(img.height * 0.8)}}, 1.0)
        return _ok_verdict(), 1.0

    monkeypatch.setattr(ppe, "detect_regions", lambda img, name="": ("photo_page", [
        {"x0": 0, "y0": 0, "x1": 1200, "y1": 800, "kind": "interior", "description": "lobby"}], 1.0))
    monkeypatch.setattr(ppe, "verify_photo", verify)
    rep = ppe.extract_photos_from_candidates([ppe.Candidate(_photo(), "p1")])
    assert len(calls) == 2 and len(rep.photos) == 1
    assert rep.photos[0].height == 640


def test_text_that_cannot_be_cropped_away_is_rejected(monkeypatch):
    monkeypatch.setattr(ppe.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ppe, "detect_regions", lambda img, name="": ("photo_page", [
        {"x0": 0, "y0": 0, "x1": 1200, "y1": 800, "kind": "exterior", "description": "x"}], 1.0))
    monkeypatch.setattr(ppe, "verify_photo", lambda img, name="": (
        {**_ok_verdict(), "contains_text": True, "text_found": "BRAND LOGO", "clean_box": None}, 1.0))
    rep = ppe.extract_photos_from_candidates([ppe.Candidate(_photo(), "p1")])
    assert rep.photos == []
    assert rep.rejected and rep.rejected[0]["text_found"] == "BRAND LOGO"


def test_api_errors_reject_instead_of_accepting(monkeypatch):
    monkeypatch.setattr(ppe.settings, "ANTHROPIC_API_KEY", "test-key")

    def boom(img, name=""):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(ppe, "detect_regions", boom)
    rep = ppe.extract_photos_from_candidates([ppe.Candidate(_photo(), "p1")])
    assert rep.photos == [] and "detect failed" in rep.errors[0]


def test_floor_plan_pages_yield_nothing(monkeypatch):
    monkeypatch.setattr(ppe.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ppe, "detect_regions", lambda img, name="": ("floor_plan", [], 1.0))
    rep = ppe.extract_photos_from_candidates([ppe.Candidate(_photo(), "p1")])
    assert rep.photos == [] and "floor_plan" in rep.rejected[0]["reason"]


# ---- candidates ------------------------------------------------------------------------------------------------

def test_pdf_candidates_skip_small_and_blank_images():
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)

    def png(img):
        b = io.BytesIO()
        img.save(b, format="PNG")
        return b.getvalue()

    page.insert_image(fitz.Rect(0, 0, 600, 400), stream=png(_photo()))
    page.insert_image(fitz.Rect(600, 0, 700, 100), stream=png(_photo(120, 80)))                     # icon
    page.insert_image(fitz.Rect(0, 400, 600, 595), stream=png(Image.new("RGB", (900, 600), "white")))  # blank
    cands = ppe.candidates_from_document(doc.tobytes(), "b.pdf")
    assert len(cands) == 1 and cands[0].image.size == (1200, 800)


# ---- drive draft integration -----------------------------------------------------------------------------------

def test_attach_clean_photos_sets_images_on_drafts(monkeypatch):
    from app.services import drive_folder_ingest as ing
    from app.services import cloud_media_storage as cms

    photo = ppe.PropertyPhoto(bytes=b"jpg", mime="image/jpeg", width=1200, height=800, kind="exterior",
                              description="Tower", alt_text="Tower", source="pdf:page2:xref4", box=(0, 0, 1, 1))
    monkeypatch.setattr(ppe, "extract_property_photos",
                        lambda *a, **k: ppe.ExtractionReport([photo], 3, [{"reason": "x"}], []))
    monkeypatch.setattr(cms.cloud_storage, "upload_property_image", lambda *a, **k: "https://cdn/x.jpg")
    draft = {"property_name": "Ayodhya Puram", "provenance": {}}
    already = {"property_name": "Other", "provenance": {"images": {"value": ["https://keep/me.jpg"]}}}
    n = ing.attach_clean_photos([draft, already], b"%PDF-", {"drive_file_id": "f1", "name": "b.pdf"})
    assert n == 1
    assert draft["provenance"]["images"]["value"] == ["https://cdn/x.jpg"]
    assert draft["photos"][0]["kind"] == "exterior"
    assert draft["photo_extraction"]["accepted"] == 1
    assert already["provenance"]["images"]["value"] == ["https://keep/me.jpg"]


def test_order_keeps_document_flow_with_exterior_cover_and_caps(monkeypatch):
    monkeypatch.setattr(ppe.settings, "ANTHROPIC_API_KEY", "test-key")
    kinds = {"p1": "interior", "p2": "amenity", "p3": "exterior", "p4": "interior"}
    monkeypatch.setattr(ppe, "detect_regions", lambda img, name="": ("photo_page", [
        {"x0": 0, "y0": 0, "x1": 1200, "y1": 800, "kind": "interior", "description": "x"}], 1.0))
    monkeypatch.setattr(ppe, "verify_photo", lambda img, name="": (_ok_verdict(kinds[img.info["src"]]), 1.0))
    real_crop = Image.Image.crop

    def crop(self, box=None):
        out = real_crop(self, box)
        out.info["src"] = self.info.get("src")
        return out

    monkeypatch.setattr(Image.Image, "crop", crop)
    cands = []
    for i, src in enumerate(kinds, 1):
        img = _photo(seed=i * 41).rotate(i * 30).resize((1200, 800))
        img.info["src"] = src
        cands.append(ppe.Candidate(img, src))
    rep = ppe.extract_photos_from_candidates(cands, max_photos=3)
    assert [p.source for p in rep.photos] == ["p3", "p1", "p2"]
    assert any("limit" in r["reason"] for r in rep.rejected)


def test_corner_zooms_are_native_resolution_corners():
    img = _photo(2000, 1000)
    zooms = ppe.corner_zooms(img)
    assert [z[0].split()[1] for z in zooms] == ["top-left", "top-right", "bottom-left", "bottom-right"]
    assert all(max(z[1].size) <= ppe.CORNER_MAX_EDGE for z in zooms)
    assert zooms[0][1].getpixel((0, 0)) == img.getpixel((0, 0))
