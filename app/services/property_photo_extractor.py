"""
app/services/property_photo_extractor.py

Extracts clean property photographs (no text, captions, logos, watermarks, plans or maps) from brochures.

Brochures are often flattened: every PDF page is one big raster with headlines, captions and logos baked into
the pixels next to (or on top of) the photos. Accepting or rejecting whole images therefore cannot give clean
photos, so each candidate goes through two independent Claude Vision passes:

  1. detect  - Claude lists every photograph / photorealistic render of the property on the candidate and returns
               a pixel bounding box that excludes all text, logos and page background.
  2. crop    - the box is cropped from the full-resolution raster and flat page-colour margins are trimmed.
  3. verify  - a separate Claude call looks only at the crop and must confirm it is a property photo with no
               visible text of any kind. If text remains, Claude may return a tighter text-free box; that crop is
               verified once more. Anything not confirmed clean is dropped.
  4. dedupe  - near-identical crops (same photo repeated across pages) are collapsed with a perceptual hash.

The extractor fails closed: with no API key, an API error or an unparseable answer, nothing is accepted.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import io
import json
import logging
import os
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageOps

from app.core.config import settings

logger = logging.getLogger(__name__)

# Long edge sent to Claude. Larger images are downscaled server-side anyway; sending at most this size keeps box
# coordinates in the same pixel space Claude sees.
VISION_MAX_EDGE = 1568
MIN_CANDIDATE_W, MIN_CANDIDATE_H = 400, 300      # embedded images smaller than this are icons / logos / slivers
MIN_PHOTO_W, MIN_PHOTO_H = 480, 320               # final crop must be at least this (full-resolution pixels)
MIN_ASPECT, MAX_ASPECT = 0.45, 2.8                # reject banner strips and thin columns
MAX_CANDIDATES = 40
DEFAULT_MAX_PHOTOS = 30
CORNER_FRACTION = 0.3                             # corner zooms sent to the verifier
CORNER_MAX_EDGE = 768
DUPLICATE_HAMMING = 6                             # dHash distance at or below this is the same photo

PHOTO_KINDS = ["exterior", "interior", "amenity", "aerial", "view", "landscape"]

# --------------------------------------------------------------------------------------------------------------
# Prompts and schemas
# --------------------------------------------------------------------------------------------------------------

DETECT_SYSTEM = """You locate real-estate photographs inside brochure pages and document images.

A PHOTO is a real photograph or a photorealistic 3D render of the property: building exterior, facade, aerial view,
interior room, bathroom, kitchen, pool, gym, lobby, garden, landscaping, terrace, or the view from the property.

NOT photos (never return these): floor plans, unit layouts, site or master plans, CAD or line drawings, elevations,
location or road maps, charts, tables, icons, logos, QR codes, headline or paragraph text blocks, colour panels,
amenity icon grids, people-only stock lifestyle pictures with no property visible, and decorative textures.

For every PHOTO on the image return one region with a tight bounding box in pixel coordinates of the image exactly
as you receive it (origin top-left, x to the right, y down). Rules for each box:
- It must contain only the photo: no page background, no borders, no neighbouring photo.
- It must contain NO text of any kind: no captions, labels, headlines, numbers, logos, watermarks, page numbers,
  stamps or brand marks. If text sits on or next to the photo, shrink the box to the largest rectangle of the photo
  that is completely free of text, provided that rectangle still shows the scene well (at least about 40% of the
  photo). If no such rectangle exists, do not return that photo.
- Collages: return each tile as its own region.
Return an empty list when the image has no qualifying photo."""

DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "page_kind": {
            "type": "string",
            "enum": ["photo_page", "mixed_page", "floor_plan", "map", "text_page", "cover_or_logo", "other"],
        },
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "x0": {"type": "integer"},
                    "y0": {"type": "integer"},
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "kind": {"type": "string", "enum": PHOTO_KINDS},
                    "description": {"type": "string"},
                },
                "required": ["x0", "y0", "x1", "y1", "kind", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["page_kind", "regions"],
    "additionalProperties": False,
}

VERIFY_SYSTEM = """You are a strict quality gate for a real-estate listing photo gallery.

Accept an image only if ALL of the following hold:
1. It is a real photograph or photorealistic render of a property (exterior, interior, amenity, aerial, view or
   landscaping). Floor plans, layouts, maps, drawings, charts, logos, icons, text panels, and blank or near-blank
   images are rejected.
2. It contains NO visible text at all: no captions, labels, headlines, numbers, logos, watermarks, brand marks,
   page numbers, QR codes, stamps or signage overlays added in post-production. Inspect every edge and corner -
   small labels in corners are the most common failure. Only text physically part of the scene that is tiny and
   unreadable (e.g. a distant shop sign) may be tolerated; if in doubt, it is text.
3. It is a single photo (not a collage with gutters) without page background bands or borders along any edge.
4. It is sharp and large enough to show the scene.

If the only problem is text, a logo, a border or a background band along part of the image, give clean_box: the
largest rectangle (pixel coordinates of the image exactly as you receive it, origin top-left) that would pass all
rules and still shows the scene well. Otherwise clean_box is null."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_property_photo": {"type": "boolean"},
        "contains_text": {"type": "boolean"},
        "text_found": {"type": "string"},
        "has_border_or_background": {"type": "boolean"},
        "is_collage": {"type": "boolean"},
        "quality_ok": {"type": "boolean"},
        "kind": {"type": "string", "enum": PHOTO_KINDS + ["not_a_photo"]},
        "description": {"type": "string"},
        "alt_text": {"type": "string"},
        "clean_box": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "x0": {"type": "integer"},
                        "y0": {"type": "integer"},
                        "x1": {"type": "integer"},
                        "y1": {"type": "integer"},
                    },
                    "required": ["x0", "y0", "x1", "y1"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
    },
    "required": ["is_property_photo", "contains_text", "text_found", "has_border_or_background", "is_collage",
                 "quality_ok", "kind", "description", "alt_text", "clean_box"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Candidate:
    image: Image.Image                     # RGB, full resolution
    source: str                            # e.g. "pdf:page3:xref44", "docx:word/media/image2.jpeg"


@dataclass
class PropertyPhoto:
    bytes: bytes                           # JPEG
    mime: str
    width: int
    height: int
    kind: str
    description: str
    alt_text: str
    source: str
    box: Tuple[int, int, int, int]         # crop box in the candidate's full-resolution pixels
    dhash: int = 0
    checks: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"bytes": self.bytes, "mime": self.mime, "width": self.width, "height": self.height,
                "image_type": self.kind, "category": "PHOTOGRAPHY", "description": self.description,
                "alt_text": self.alt_text, "source": self.source, "box": list(self.box)}


@dataclass
class ExtractionReport:
    photos: List[PropertyPhoto]
    candidates: int
    rejected: List[Dict[str, Any]]
    errors: List[str]


# --------------------------------------------------------------------------------------------------------------
# Candidate extraction
# --------------------------------------------------------------------------------------------------------------

def _to_rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _big_enough(w: int, h: int) -> bool:
    return w >= MIN_CANDIDATE_W and h >= MIN_CANDIDATE_H


def _pdf_candidates(data: bytes) -> List[Candidate]:
    import fitz  # PyMuPDF

    out: List[Candidate] = []
    seen_xref, seen_hash = set(), set()
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for page_no, page in enumerate(doc, start=1):
            for info in page.get_images(full=True):
                xref, smask, w, h = info[0], info[1], info[2], info[3]
                if xref in seen_xref or not _big_enough(w, h):
                    continue
                seen_xref.add(xref)
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if smask:
                        # Apply the soft mask so transparent regions become white instead of noise.
                        pix = fitz.Pixmap(pix, fitz.Pixmap(doc, smask))
                    if pix.colorspace and pix.colorspace.n not in (1, 3):   # CMYK etc.
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    img = _to_rgb(Image.open(io.BytesIO(pix.tobytes("png"))))
                except Exception as exc:  # unusual colour spaces / filters
                    logger.debug("pdf image xref %s unreadable: %s", xref, exc)
                    continue
                if _is_flat(img):     # blank tiles, solid fills, stray masks
                    continue
                digest = hashlib.sha1(img.resize((32, 32)).tobytes()).hexdigest()
                if digest in seen_hash:
                    continue
                seen_hash.add(digest)
                out.append(Candidate(img, f"pdf:page{page_no}:xref{xref}"))
                if len(out) >= MAX_CANDIDATES:
                    return out
    finally:
        doc.close()
    return out


def _zip_candidates(data: bytes, prefix: str, label: str) -> List[Candidate]:
    out: List[Candidate] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in sorted(zf.namelist()):
            if not name.startswith(prefix) or not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp",
                                                                          ".tif", ".tiff")):
                continue
            try:
                img = _to_rgb(Image.open(io.BytesIO(zf.read(name))))
            except Exception:
                continue
            if _big_enough(*img.size) and not _is_flat(img):
                out.append(Candidate(img, f"{label}:{name}"))
            if len(out) >= MAX_CANDIDATES:
                break
    return out


def candidates_from_document(data: bytes, filename: str) -> List[Candidate]:
    """Raster candidates from a PDF, DOCX, PPTX or standalone image file."""
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if ext == ".pdf" or data[:5] == b"%PDF-":
            return _pdf_candidates(data)
        if ext == ".docx":
            return _zip_candidates(data, "word/media/", "docx")
        if ext == ".pptx":
            return _zip_candidates(data, "ppt/media/", "pptx")
        img = _to_rgb(Image.open(io.BytesIO(data)))
        return [Candidate(img, f"image:{filename}")] if _big_enough(*img.size) else []
    except Exception as exc:
        logger.warning("photo candidate extraction failed for %s: %s", filename, exc)
        return []


def candidates_from_images(items: Sequence[Dict[str, Any]]) -> List[Candidate]:
    """Candidates from already-downloaded images: [{"bytes": ..., "source": ...}]."""
    out: List[Candidate] = []
    for i, it in enumerate(items):
        try:
            img = _to_rgb(Image.open(io.BytesIO(it["bytes"])))
        except Exception:
            continue
        if _big_enough(*img.size) and not _is_flat(img):
            out.append(Candidate(img, str(it.get("source") or f"image{i + 1}")))
    return out[:MAX_CANDIDATES]


# --------------------------------------------------------------------------------------------------------------
# Pixel helpers
# --------------------------------------------------------------------------------------------------------------

def _is_flat(img: Image.Image) -> bool:
    """True for blank / single-colour / mask-like images."""
    small = img.convert("L").resize((64, 64))
    lo, hi = small.getextrema()
    if hi - lo < 18:
        return True
    hist = small.histogram()
    return max(hist) / float(64 * 64) > 0.92


def _row_is_flat(px, y: int, x0: int, x1: int, ref: Tuple[int, int, int], tol: int) -> bool:
    step = max(1, (x1 - x0) // 120)
    for x in range(x0, x1, step):
        p = px[x, y]
        if abs(p[0] - ref[0]) > tol or abs(p[1] - ref[1]) > tol or abs(p[2] - ref[2]) > tol:
            return False
    return True


def _col_is_flat(px, x: int, y0: int, y1: int, ref: Tuple[int, int, int], tol: int) -> bool:
    step = max(1, (y1 - y0) // 120)
    for y in range(y0, y1, step):
        p = px[x, y]
        if abs(p[0] - ref[0]) > tol or abs(p[1] - ref[1]) > tol or abs(p[2] - ref[2]) > tol:
            return False
    return True


def trim_flat_margins(img: Image.Image, tol: int = 14, max_frac: float = 0.25) -> Image.Image:
    """Remove uniform page-colour bands from each edge (Claude's boxes often include a sliver of background)."""
    w, h = img.size
    if w < 8 or h < 8:
        return img
    px = img.load()
    x0, y0, x1, y1 = 0, 0, w, h
    lim_x, lim_y = int(w * max_frac), int(h * max_frac)
    ref = px[w // 2, 0]
    while y0 < lim_y and _row_is_flat(px, y0, x0, x1, px[x1 // 2, y0], tol) and _row_is_flat(px, y0, x0, x1, ref, 60):
        y0 += 1
    ref = px[w // 2, h - 1]
    while h - y1 < lim_y and _row_is_flat(px, y1 - 1, x0, x1, px[x1 // 2, y1 - 1], tol) \
            and _row_is_flat(px, y1 - 1, x0, x1, ref, 60):
        y1 -= 1
    ref = px[0, h // 2]
    while x0 < lim_x and _col_is_flat(px, x0, y0, y1, px[x0, (y0 + y1) // 2], tol) \
            and _col_is_flat(px, x0, y0, y1, ref, 60):
        x0 += 1
    ref = px[w - 1, h // 2]
    while w - x1 < lim_x and _col_is_flat(px, x1 - 1, y0, y1, px[x1 - 1, (y0 + y1) // 2], tol) \
            and _col_is_flat(px, x1 - 1, y0, y1, ref, 60):
        x1 -= 1
    if (x0, y0, x1, y1) == (0, 0, w, h):
        return img
    return img.crop((x0, y0, x1, y1))


def dhash(img: Image.Image, size: int = 8) -> int:
    g = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _shape_ok(w: int, h: int) -> bool:
    return w >= MIN_PHOTO_W and h >= MIN_PHOTO_H and MIN_ASPECT <= (w / float(h)) <= MAX_ASPECT


def _scaled_for_vision(img: Image.Image) -> Tuple[Image.Image, float]:
    """Downscaled copy for Claude and the factor to map its coordinates back to full resolution."""
    w, h = img.size
    scale = min(1.0, VISION_MAX_EDGE / float(max(w, h)))
    if scale >= 1.0:
        return img, 1.0
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS), scale


def _jpeg_b64(img: Image.Image, quality: int = 88) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _box_to_full(box: Dict[str, Any], scale: float, size: Tuple[int, int],
                 offset: Tuple[int, int] = (0, 0)) -> Optional[Tuple[int, int, int, int]]:
    try:
        x0, y0, x1, y1 = (int(box["x0"]), int(box["y0"]), int(box["x1"]), int(box["y1"]))
    except (KeyError, TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    W, H = size
    fx0 = max(0, min(W, int(round(x0 / scale)) + offset[0]))
    fy0 = max(0, min(H, int(round(y0 / scale)) + offset[1]))
    fx1 = max(0, min(W, int(round(x1 / scale)) + offset[0]))
    fy1 = max(0, min(H, int(round(y1 / scale)) + offset[1]))
    if fx1 - fx0 < 16 or fy1 - fy0 < 16:
        return None
    return fx0, fy0, fx1, fy1


# --------------------------------------------------------------------------------------------------------------
# Claude calls
# --------------------------------------------------------------------------------------------------------------

def _model() -> str:
    return getattr(settings, "PHOTO_EXTRACT_MODEL", "") or settings.ANTHROPIC_MODEL or "claude-opus-5-5"


_client_singleton = None


def _client():
    global _client_singleton
    if _client_singleton is None:
        import anthropic
        _client_singleton = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=120.0, max_retries=3)
    return _client_singleton


def _image_block(img: Image.Image) -> Dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(img)}}


def _ask_json(system: str, schema: Dict[str, Any], img: Image.Image, text: str, effort: str,
              extra_images: Sequence[Tuple[str, Image.Image]] = ()) -> Dict[str, Any]:
    """One vision request with a JSON-schema constrained answer. Raises on API errors or refusals."""
    content: List[Dict[str, Any]] = [_image_block(img)]
    for label, extra in extra_images:
        content += [{"type": "text", "text": label}, _image_block(extra)]
    content.append({"type": "text", "text": text})
    resp = _client().messages.create(
        model=_model(),
        max_tokens=8000,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("vision request refused")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("vision answer truncated")
    raw = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    return json.loads(raw)


def detect_regions(img: Image.Image, property_name: str = "") -> Tuple[str, List[Dict[str, Any]], float]:
    small, scale = _scaled_for_vision(img)
    text = (f"Image size: {small.width} x {small.height} pixels. Property: {property_name or 'unknown'}.\n"
            "List every qualifying property photo with a tight, text-free bounding box.")
    data = _ask_json(DETECT_SYSTEM, DETECT_SCHEMA, small, text, effort="high")
    return str(data.get("page_kind") or "other"), list(data.get("regions") or []), scale


def corner_zooms(img: Image.Image) -> List[Tuple[str, Image.Image]]:
    """Native-resolution views of the four corners, where small labels and watermarks usually sit."""
    w, h = img.size
    cw, ch = int(w * CORNER_FRACTION), int(h * CORNER_FRACTION)
    out = []
    for name, box in (("top-left", (0, 0, cw, ch)), ("top-right", (w - cw, 0, w, ch)),
                      ("bottom-left", (0, h - ch, cw, h)), ("bottom-right", (w - cw, h - ch, w, h))):
        c = img.crop(box)
        c.thumbnail((CORNER_MAX_EDGE, CORNER_MAX_EDGE), Image.Resampling.LANCZOS)
        out.append((f"Zoomed {name} corner of the same image (for spotting small text):", c))
    return out


def verify_photo(img: Image.Image, property_name: str = "") -> Tuple[Dict[str, Any], float]:
    small, scale = _scaled_for_vision(img)
    text = (f"The first image is the candidate, {small.width} x {small.height} pixels; the zoomed corners are only "
            f"for inspection and clean_box must use the first image's pixels. Property: {property_name or 'unknown'}.\n"
            "Decide whether this image may be published as a clean listing photo.")
    return _ask_json(VERIFY_SYSTEM, VERIFY_SCHEMA, small, text, effort="high",
                     extra_images=corner_zooms(img)), scale


def _passes(v: Dict[str, Any]) -> bool:
    return bool(v.get("is_property_photo")) and not v.get("contains_text") and not v.get("is_collage") \
        and not v.get("has_border_or_background") and bool(v.get("quality_ok")) and v.get("kind") in PHOTO_KINDS


# --------------------------------------------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------------------------------------------

def _process_region(cand: Candidate, region: Dict[str, Any], scale: float, property_name: str,
                    rejected: List[Dict[str, Any]]) -> Optional[PropertyPhoto]:
    box = _box_to_full(region, scale, cand.image.size)
    label = f"{cand.source} [{region.get('kind')}] {region.get('description', '')[:60]}"
    if not box:
        rejected.append({"source": cand.source, "reason": "invalid box", "region": region})
        return None
    crop = trim_flat_margins(cand.image.crop(box))
    if not _shape_ok(*crop.size):
        rejected.append({"source": label, "reason": f"too small / bad aspect {crop.size}"})
        return None
    if _is_flat(crop):
        rejected.append({"source": label, "reason": "blank crop"})
        return None

    for attempt in range(2):
        v, vscale = verify_photo(crop, property_name)
        if _passes(v):
            return PropertyPhoto(
                bytes=_encode_jpeg(crop), mime="image/jpeg", width=crop.width, height=crop.height,
                kind=v.get("kind") or region.get("kind") or "exterior",
                description=v.get("description") or region.get("description") or "",
                alt_text=(v.get("alt_text") or region.get("description") or property_name or "Property photo")[:200],
                source=cand.source, box=box, dhash=dhash(crop), checks={"verify_attempts": attempt + 1})
        clean = v.get("clean_box") if v.get("is_property_photo") else None
        if attempt == 0 and clean:
            sub = _box_to_full(clean, vscale, crop.size)
            if sub:
                crop = trim_flat_margins(crop.crop(sub))
                if _shape_ok(*crop.size) and not _is_flat(crop):
                    continue
        rejected.append({"source": label, "reason": "failed verification",
                         "text_found": v.get("text_found"), "verdict": {k: v.get(k) for k in (
                             "is_property_photo", "contains_text", "is_collage", "has_border_or_background",
                             "quality_ok", "kind")}})
        return None
    return None


def _encode_jpeg(img: Image.Image) -> bytes:
    # Cap the stored size: listing galleries do not need 3600 px originals.
    if max(img.size) > 2400:
        img = img.copy()
        img.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True, progressive=True)
    return buf.getvalue()


def _process_candidate(cand: Candidate, property_name: str, deadline: Optional[float] = None
                       ) -> Tuple[List[PropertyPhoto], List[Dict[str, Any]], List[str]]:
    rejected: List[Dict[str, Any]] = []
    errors: List[str] = []
    photos: List[PropertyPhoto] = []
    if deadline is not None and time.monotonic() > deadline:
        errors.append(f"{cand.source}: skipped, photo time budget used up")
        return photos, rejected, errors
    try:
        page_kind, regions, scale = detect_regions(cand.image, property_name)
    except Exception as exc:
        errors.append(f"{cand.source}: detect failed ({type(exc).__name__}: {exc})")
        return photos, rejected, errors
    if not regions:
        rejected.append({"source": cand.source, "reason": f"no photo regions ({page_kind})"})
    for region in regions[:8]:
        try:
            p = _process_region(cand, region, scale, property_name, rejected)
            if p:
                photos.append(p)
        except Exception as exc:
            errors.append(f"{cand.source}: verify failed ({type(exc).__name__}: {exc})")
    return photos, rejected, errors


def _dedupe(photos: List[PropertyPhoto]) -> List[PropertyPhoto]:
    kept: List[PropertyPhoto] = []
    for p in sorted(photos, key=lambda x: x.width * x.height, reverse=True):
        if any(_hamming(p.dhash, k.dhash) <= DUPLICATE_HAMMING for k in kept):
            continue
        kept.append(p)
    return kept


_COVER_KINDS = ("exterior", "aerial")


def extract_photos_from_candidates(candidates: Sequence[Candidate], property_name: str = "",
                                   max_photos: int = DEFAULT_MAX_PHOTOS, workers: int = 6,
                                   deadline_sec: Optional[float] = None) -> ExtractionReport:
    if not candidates:
        return ExtractionReport([], 0, [], [])
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        # Fail closed: without Claude nothing can be confirmed text-free.
        return ExtractionReport([], len(candidates), [], ["ANTHROPIC_API_KEY is not set; no photos were accepted"])
    photos: List[PropertyPhoto] = []
    rejected: List[Dict[str, Any]] = []
    errors: List[str] = []
    position = {c.source: i for i, c in enumerate(candidates)}
    deadline = time.monotonic() + deadline_sec if deadline_sec else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for p, r, e in pool.map(lambda c: _process_candidate(c, property_name, deadline), candidates):
            photos.extend(p)
            rejected.extend(r)
            errors.extend(e)
    unique = _dedupe(photos)
    kept_ids = {id(p) for p in unique}
    rejected.extend({"source": p.source, "reason": "duplicate of a kept photo"}
                    for p in photos if id(p) not in kept_ids)
    # Keep the document's own order (it mixes exteriors, amenities and interiors), with the largest exterior or
    # aerial view first so it becomes the cover image.
    unique.sort(key=lambda p: (position.get(p.source, 0), p.box[1], p.box[0]))
    covers = [p for p in unique if p.kind in _COVER_KINDS]
    if covers:
        cover = max(covers, key=lambda p: p.width * p.height)
        unique.remove(cover)
        unique.insert(0, cover)
    if len(unique) > max_photos:
        rejected.extend({"source": p.source, "reason": f"over the {max_photos}-photo limit"}
                        for p in unique[max_photos:])
    return ExtractionReport(unique[:max_photos], len(candidates), rejected, errors)


def extract_property_photos(data: bytes, filename: str, property_name: str = "",
                            max_photos: int = DEFAULT_MAX_PHOTOS, deadline_sec: Optional[float] = None,
                            workers: int = 6) -> ExtractionReport:
    """Clean, text-free property photos from a brochure / document / image file."""
    cands = candidates_from_document(data, filename)
    report = extract_photos_from_candidates(cands, property_name, max_photos=max_photos, workers=workers,
                                            deadline_sec=deadline_sec)
    logger.info("photo extraction %s: %d candidates -> %d clean photos (%d rejected, %d errors)",
                filename, report.candidates, len(report.photos), len(report.rejected), len(report.errors))
    return report
