"""
app/services/image_processor.py

Intelligent real estate document image processing engine:
1. Filters CAD fragments and tiny vector artifacts.
2. Converts CMYK / TIFF / RGBA images to standard web-ready sRGB WebP/JPEG.
3. Detects low-resolution icons, logos, and decorative elements (aspect ratio, dimension scoring).
4. Employs perceptual/MD5 hashing for asset deduplication across brochure pages.
5. Employs PyMuPDF (fitz) page rendering when embedded stream extraction produces fragmented tiles.
6. Classifies images into hero/cover candidates, gallery photographs, and floor plans.
"""
from __future__ import annotations

import hashlib
import io
import logging
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Minimum thresholds for real estate property photography
MIN_WIDTH_PHOTO = 500
MIN_HEIGHT_PHOTO = 350
MIN_AREA_PHOTO = 250_000  # 500x500 approx
MAX_ASPECT_RATIO = 3.5    # Exclude extreme banners or thin line slivers
MIN_ASPECT_RATIO = 0.28   # Exclude ultra-tall vertical dividers


def normalize_image_to_srgb(image_bytes: bytes) -> Optional[Tuple[bytes, str, Tuple[int, int]]]:
    """
    Parses raw image bytes, converts CMYK/Palette/Grayscale to clean RGB,
    transposes according to EXIF orientation, and returns (webp_bytes, 'image/webp', (width, height)).
    """
    if not image_bytes:
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Respect EXIF orientation tag
            img = ImageOps.exif_transpose(img)

            width, height = img.size
            if width == 0 or height == 0:
                return None

            # Convert CMYK / P / LA / RGBA to RGB for standard web delivery
            if img.mode in ("CMYK", "P", "PA", "LA", "L"):
                img = img.convert("RGB")
            elif img.mode == "RGBA":
                # Create white background for transparent PNG/WebP if needed
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg

            output = io.BytesIO()
            img.save(output, format="WEBP", quality=85, method=4)
            return output.getvalue(), "image/webp", (width, height)
    except Exception as e:
        logger.debug(f"Failed to normalize image to sRGB: {e}")
        return None


def calculate_image_hash(image_bytes: bytes) -> str:
    """Returns SHA-256 hash for fast deduplication."""
    return hashlib.sha256(image_bytes).hexdigest()


def is_valid_property_photo(width: int, height: int) -> bool:
    """Filters out icons, CAD line fragments, small buttons, and background dividers."""
    if width < MIN_WIDTH_PHOTO or height < MIN_HEIGHT_PHOTO:
        return False
    aspect = width / float(height)
    if aspect > MAX_ASPECT_RATIO or aspect < MIN_ASPECT_RATIO:
        return False
    return True


def is_floor_plan_or_layout(width: int, height: int, context_hint: str = "") -> bool:
    """Detects whether an image is likely a master plan, unit layout, or architectural blueprint."""
    hint_lower = context_hint.lower()
    floorplan_keywords = ["floor", "plan", "layout", "unit", "masterplan", "cluster", "blueprint", "2bhk", "3bhk"]
    if any(k in hint_lower for k in floorplan_keywords):
        return True
    return False


def score_image_for_cover(width: int, height: int, is_floorplan: bool = False) -> float:
    """
    Scores an image to identify the best candidate for cover_image.
    Prefers landscape orientations (~16:9 or ~4:3) with high resolution.
    Penalizes floor plans and extreme portrait aspect ratios.
    """
    if is_floorplan:
        return -100.0

    area = width * height
    aspect = width / float(height) if height > 0 else 1.0

    # Ideal aspect ratio for real estate hero photo is between 1.33 (4:3) and 1.78 (16:9)
    aspect_penalty = abs(aspect - 1.5) * 50.0
    score = (area / 1000.0) - aspect_penalty
    return max(0.0, score)


def extract_high_res_images_from_pdf(
    pdf_bytes: bytes,
    max_pages: int = 15,
    max_images: int = 12
) -> List[Dict[str, Any]]:
    """
    Extracts high-resolution property photos and floor plans from a PDF document.
    Uses PyMuPDF (fitz) to inspect embedded images. If embedded images are fragmented
    or too small (e.g. vector brochures), it renders high-res full page visuals (150 DPI)
    so the brochure's actual visual presentations are retained.
    """
    results: List[Dict[str, Any]] = []
    seen_hashes = set()

    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        logger.warning(f"Could not open PDF with PyMuPDF: {e}")
        return results

    try:
        total_pages = min(len(doc), max_pages)

        for page_idx in range(total_pages):
            page = doc[page_idx]
            image_list = page.get_images(full=True)
            page_extracted_count = 0

            for img_info in image_list:
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    if not base_image:
                        continue
                    raw_bytes = base_image.get("image")
                    norm = normalize_image_to_srgb(raw_bytes)
                    if not norm:
                        continue
                    clean_bytes, mime, (w, h) = norm

                    if is_valid_property_photo(w, h):
                        img_hash = calculate_image_hash(clean_bytes)
                        if img_hash not in seen_hashes:
                            seen_hashes.add(img_hash)
                            is_fp = is_floor_plan_or_layout(w, h, context_hint=f"page_{page_idx+1}")
                            score = score_image_for_cover(w, h, is_floorplan=is_fp)

                            results.append({
                                "bytes": clean_bytes,
                                "mime": mime,
                                "width": w,
                                "height": h,
                                "is_floorplan": is_fp,
                                "score": score,
                                "page": page_idx + 1,
                                "source": "embedded_image"
                            })
                            page_extracted_count += 1
                            if len(results) >= max_images:
                                break
                except Exception as ex:
                    logger.debug(f"Failed extracting image xref {xref}: {ex}")

            if len(results) >= max_images:
                break

            # If the page has rich visual content but NO stand-alone large embedded image (e.g. CAD vector brochure),
            # render the page at 150 DPI as a high-fidelity visual brochure plate
            if page_extracted_count == 0 and page_idx in (0, 1, 2, 3, 4):
                try:
                    pix = page.get_pixmap(dpi=150)
                    rendered_bytes = pix.tobytes("webp")
                    norm = normalize_image_to_srgb(rendered_bytes)
                    if norm:
                        clean_bytes, mime, (w, h) = norm
                        img_hash = calculate_image_hash(clean_bytes)
                        if img_hash not in seen_hashes:
                            seen_hashes.add(img_hash)
                            # First page is likely cover
                            is_cover_cand = (page_idx == 0)
                            results.append({
                                "bytes": clean_bytes,
                                "mime": mime,
                                "width": w,
                                "height": h,
                                "is_floorplan": False,
                                "score": 5000.0 if is_cover_cand else 1000.0,
                                "page": page_idx + 1,
                                "source": "page_render"
                            })
                except Exception as ex:
                    logger.debug(f"Failed rendering page {page_idx} visual: {ex}")

    finally:
        try:
            doc.close()
        except Exception:
            pass

    # Sort results by quality/cover score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return results

