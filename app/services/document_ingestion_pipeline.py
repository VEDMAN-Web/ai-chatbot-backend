"""
app/services/document_ingestion_pipeline.py

Enterprise Production Real Estate Document Ingestion Pipeline.
Supports:
- Heterogeneous Document Inputs: PDF, DOCX, XLSX, CSV, PPTX, TXT, HTML, Standalone Images, and Web URLs.
- Multi-Stage AI Text Extraction with Claude Opus & Claude Vision.
- Claude Vision Image Quality & Authenticity Verification.
- Automated CAD Line/Fragment Filtering and Page Visual Rendering.
- Permanent Public Firebase/Google Cloud Storage Bucket Management.
- Synchronized Database Persistence (PostgreSQL `properties`, `property_specs`, `property_images` + Firestore `projects`/`properties`).
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import urllib.parse
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from app.core.config import settings
from app.db.models import Property, PropertyImage, PropertySpec
from app.db.session import SessionLocal
from app.services.claude_property_extractor import (
    extract_property_details_from_image_with_claude,
    extract_property_details_with_claude,
)
from app.services.claude_vision_verifier import verify_image_with_claude_vision
from app.services.cloud_media_storage import cloud_storage
from app.services.image_processor import (
    extract_high_res_images_from_pdf,
    normalize_image_to_srgb,
)
from app.services.property_normalizer import (
    normalize_property_type,
    normalize_status,
    parse_and_normalize_area,
    parse_and_normalize_price,
    parse_bedrooms_and_bathrooms,
    parse_floors_and_units,
)

logger = logging.getLogger(__name__)


def extract_raw_text(content_bytes: bytes, filename: str) -> str:
    """Extracts text from PDF, DOCX, XLSX, CSV, PPTX, TXT, HTML."""
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(stream=content_bytes, filetype="pdf")
            parts = [page.get_text("text") or "" for page in doc]
            doc.close()
            return "\n".join(parts).strip()
        except Exception as e:
            logger.warning(f"fitz PDF text extraction note: {e}")

    elif ext in (".docx", ".doc"):
        try:
            import docx
            doc = docx.Document(io.BytesIO(content_bytes))
            lines = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    lines.append(" | ".join(c.text.strip() for c in row.cells if c.text.strip()))
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"docx extraction failed: {e}")

    elif ext in (".xlsx", ".xls", ".csv"):
        try:
            import pandas as pd
            if ext == ".csv":
                df = pd.read_csv(io.BytesIO(content_bytes))
            else:
                df = pd.read_excel(io.BytesIO(content_bytes))
            return df.to_string()
        except Exception as e:
            logger.warning(f"spreadsheet extraction failed: {e}")

    elif ext in (".pptx", ".ppt"):
        try:
            from pptx import Presentation
            prs = Presentation(io.BytesIO(content_bytes))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        parts.append(shape.text.strip())
            return "\n".join(parts)
        except Exception as e:
            logger.warning(f"pptx extraction failed: {e}")

    elif ext in (".html", ".htm"):
        try:
            soup = BeautifulSoup(content_bytes, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except Exception as e:
            logger.warning(f"html text extraction failed: {e}")

    # Fallback UTF-8 / ASCII decode
    try:
        return content_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def extract_candidate_images(content_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """Extracts candidate images from PDF, DOCX, PPTX, or standalone image formats."""
    ext = os.path.splitext(filename)[1].lower()
    candidates: List[Dict[str, Any]] = []

    if ext == ".pdf":
        return _extract_candidate_pdf_images(content_bytes)

    elif ext in (".docx", ".doc"):
        return _extract_zip_media_images(content_bytes, "word/media/")

    elif ext in (".pptx", ".ppt"):
        return _extract_zip_media_images(content_bytes, "ppt/media/")

    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"):
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext.lstrip('.')}"
        candidates.append({
            "bytes": content_bytes,
            "mime": mime,
            "source": filename
        })

    return candidates


def _extract_zip_media_images(zip_bytes: bytes, prefix: str) -> List[Dict[str, Any]]:
    """Extracts media images embedded inside DOCX or PPTX archives."""
    images = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for fname in z.namelist():
                if fname.startswith(prefix) and fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    img_data = z.read(fname)
                    # Skip icons and decorative bullets under 12KB
                    if len(img_data) >= 12000:
                        mime = "image/jpeg" if fname.lower().endswith((".jpg", ".jpeg")) else "image/png"
                        images.append({
                            "bytes": img_data,
                            "mime": mime,
                            "source": fname
                        })
    except Exception as e:
        logger.warning(f"Zip media extraction error: {e}")
    return images


def _extract_candidate_pdf_images(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """Extracts embedded photos and renders full visual page plates (150 DPI)."""
    candidates: List[Dict[str, Any]] = []
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        # 1. Render presentation pages (up to 8 pages)
        for page_idx in range(min(len(doc), 8)):
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("jpeg")
            candidates.append({
                "bytes": img_bytes,
                "mime": "image/jpeg",
                "source": f"page_{page_idx+1}"
            })

        # 2. Inspect high-res embedded photos (filter out tiny slivers, CAD fills and extreme aspect ratios)
        for page_idx in range(len(doc)):
            for img_info in doc[page_idx].get_images():
                xref = img_info[0]
                base_img = doc.extract_image(xref)
                if base_img:
                    w = base_img.get("width", 0)
                    h = base_img.get("height", 0)
                    if w >= 800 and h >= 600 and 0.4 <= (w / h) <= 2.5:
                        candidates.append({
                            "bytes": base_img["image"],
                            "mime": "image/jpeg" if base_img.get("ext") == "jpeg" else "image/png",
                            "source": f"embedded_xref_{xref}"
                        })

        doc.close()
    except Exception as e:
        logger.warning(f"PDF image extraction helper error: {e}")

    return candidates[:10]


def process_and_ingest_property_document(
    content_bytes: bytes,
    filename: str,
    property_id_override: Optional[str] = None,
    save_to_database: bool = True
) -> Dict[str, Any]:
    """
    Master Ingestion Workflow:
    1. Extracts full document text (or analyzes image with Claude Vision for image files).
    2. Runs Claude Opus to extract and validate all 35+ fields against database schema.
    3. Renders and extracts candidate images/floor plans.
    4. Runs Claude Vision on each image to verify authenticity and weed out blank tiles/logos.
    5. Uploads verified assets to Firebase/GCS bucket with public read access.
    6. Stores all records in PostgreSQL and synchronizes with Firestore.
    """
    ext = os.path.splitext(filename)[1].lower()
    raw_text = extract_raw_text(content_bytes, filename)
    prop_id = property_id_override or f"prop-{uuid.uuid4().hex[:12]}"

    # --- Step 1: AI Extraction with Claude Opus ---
    ai_data: Optional[Dict[str, Any]] = None
    if raw_text and len(raw_text.strip()) > 30:
        ai_data = extract_property_details_with_claude(raw_text, filename)

    # For standalone image files where raw_text is empty or sparse, use Claude Vision for schema extraction
    if not ai_data and ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"):
        ai_data = extract_property_details_from_image_with_claude(content_bytes, filename)

    if not ai_data:
        # Fallback to deterministic regex extractor
        ai_data = _deterministic_fallback(raw_text, filename)

    prop_name = ai_data.get("property_name") or os.path.splitext(filename)[0].replace("_", " ").title()

    # --- Step 2: Image Extraction & Claude Vision Verification ---
    candidate_images = extract_candidate_images(content_bytes, filename)
    verified_images: List[Dict[str, Any]] = []

    import concurrent.futures

    def _verify_one(item):
        i, c = item
        res = verify_image_with_claude_vision(c["bytes"], property_name_hint=prop_name)
        return i, c, res

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        check_results = list(pool.map(_verify_one, enumerate(candidate_images)))

    check_results.sort(key=lambda x: x[0])
    for idx, cand, v_result in check_results:
        if v_result.get("is_valid", False):
            verified_images.append({
                "bytes": cand["bytes"],
                "mime": cand["mime"],
                "image_type": v_result.get("image_type", "exterior"),
                "category": v_result.get("category"),
                "alt_text": v_result.get("alt_text", f"{prop_name} Image {idx+1}"),
                "description": v_result.get("description", "")
            })
        else:
            logger.info(f"Filtered out invalid image #{idx+1} ({v_result.get('category')}): {v_result.get('description')}")

    # --- Step 3: Upload Verified Images to Firebase/GCS Cloud Bucket ---
    uploaded_urls: List[str] = []
    bucket = cloud_storage.bucket_name

    for idx, img_info in enumerate(verified_images):
        img_type = img_info.get("image_type", "photo")
        obj_name = f"properties/{prop_id}/images/{img_type}_{idx+1}.jpg"
        try:
            pub_url = _upload_and_make_public(bucket, obj_name, img_info["bytes"], img_info.get("mime", "image/jpeg"))
            uploaded_urls.append(pub_url)
            img_info["public_url"] = pub_url
        except Exception as e:
            logger.error(f"Failed to upload verified image {obj_name}: {e}")

    # Prioritize cover image: cover page or exterior photo
    cover_image_url = None
    for img in verified_images:
        if img.get("category") in ("PRESENTATION_COVER", "PHOTOGRAPHY") and "public_url" in img:
            cover_image_url = img["public_url"]
            break
    if not cover_image_url and uploaded_urls:
        cover_image_url = uploaded_urls[0]

    # --- Step 4: Assemble Complete Database Schema Payload ---
    payload = {
        "property_id": prop_id,
        "property_name": prop_name,
        "project_name": ai_data.get("project_name") or prop_name,
        "property_type": ai_data.get("property_type") or "Apartment",
        "status": ai_data.get("status") or "Available",
        "country": ai_data.get("country") or "India",
        "province": ai_data.get("province") or "Gujarat",
        "district": ai_data.get("district"),
        "subdistrict": ai_data.get("subdistrict"),
        "area": ai_data.get("area") or ai_data.get("province"),
        "address": ai_data.get("address") or f"{ai_data.get('area')}, {ai_data.get('province')}, {ai_data.get('country')}",
        "latitude": ai_data.get("latitude"),
        "longitude": ai_data.get("longitude"),
        "developer_name": ai_data.get("developer_name"),
        "price_display": ai_data.get("price_display") or "Price on Request",
        "price_numeric_min": ai_data.get("price_numeric_min") or 0.0,
        "price_numeric_max": ai_data.get("price_numeric_max"),
        "currency": ai_data.get("currency") or "INR",
        "price_note": ai_data.get("price_note"),
        "bedrooms_display": ai_data.get("bedrooms_display"),
        "bedrooms_min": ai_data.get("bedrooms_min"),
        "bedrooms_max": ai_data.get("bedrooms_max"),
        "bathrooms": ai_data.get("bathrooms"),
        "area_display": ai_data.get("area_display"),
        "area_sqm_min": ai_data.get("area_sqm_min"),
        "area_sqm_max": ai_data.get("area_sqm_max"),
        "units": ai_data.get("units"),
        "total_floors": ai_data.get("total_floors"),
        "has_pool": bool(ai_data.get("has_pool", False)),
        "beach_access": bool(ai_data.get("beach_access", False)),
        "sea_view": bool(ai_data.get("sea_view", False)),
        "amenities": ai_data.get("amenities", []),
        "features": ai_data.get("features", []),
        "cover_image": cover_image_url,
        "images": uploaded_urls,
        "unit_types": ai_data.get("unit_types", []),
        "specs": ai_data.get("specs", {})
    }

    # --- Step 5: Persist in PostgreSQL and Firestore ---
    if save_to_database:
        _save_to_postgresql_and_firestore(payload, verified_images)

    return payload


def process_and_ingest_property_url(
    url: str,
    property_id_override: Optional[str] = None,
    save_to_database: bool = True
) -> Dict[str, Any]:
    """
    Ingests a property directly from a web link or URL:
    - If URL points to a PDF or Image, downloads and passes through document pipeline.
    - If URL is an HTML webpage, fetches HTML, parses text & metadata, extracts candidate images,
      verifies them with Claude Vision, uploads to Firebase bucket, and saves property.
    """
    logger.info(f"Ingesting property from URL: {url}")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "").lower()
    path_name = urllib.parse.urlparse(url).path

    # If direct file download
    if "application/pdf" in content_type or path_name.lower().endswith(".pdf"):
        return process_and_ingest_property_document(resp.content, "web_download.pdf", property_id_override, save_to_database)

    if any(m in content_type for m in ("image/jpeg", "image/png", "image/webp")) or any(path_name.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
        ext = os.path.splitext(path_name)[1] or ".jpg"
        return process_and_ingest_property_document(resp.content, f"web_image{ext}", property_id_override, save_to_database)

    # HTML Webpage Ingestion
    soup = BeautifulSoup(resp.content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Extract text content
    page_text = soup.get_text(separator="\n", strip=True)

    # Discover candidate image URLs
    candidate_img_urls: List[str] = []
    # OpenGraph image
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        candidate_img_urls.append(urllib.parse.urljoin(url, og_img["content"]))

    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-lazy-src")
        if src:
            clean_url = urllib.parse.urljoin(url, src)
            if not any(noise in clean_url.lower() for noise in ("logo", "icon", "favicon", "avatar", ".svg", "pixel", "1x1")):
                if clean_url not in candidate_img_urls:
                    candidate_img_urls.append(clean_url)

    # Download candidate images (up to 12)
    candidate_images: List[Dict[str, Any]] = []
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for img_url in candidate_img_urls[:12]:
            try:
                img_resp = client.get(img_url, headers=headers)
                if img_resp.status_code == 200 and len(img_resp.content) >= 15000:
                    candidate_images.append({
                        "bytes": img_resp.content,
                        "mime": img_resp.headers.get("content-type", "image/jpeg"),
                        "source": img_url
                    })
            except Exception as e:
                logger.warning(f"Could not download candidate image {img_url}: {e}")

    prop_id = property_id_override or f"prop-{uuid.uuid4().hex[:12]}"

    # AI Extraction with Claude Opus
    ai_data = extract_property_details_with_claude(page_text, url)
    if not ai_data:
        ai_data = _deterministic_fallback(page_text, url)

    prop_name = ai_data.get("property_name") or (soup.title.string.strip() if soup.title else "Online Property")

    # Verify images with Claude Vision
    verified_images: List[Dict[str, Any]] = []
    import concurrent.futures

    def _verify_url_cand(item):
        i, c = item
        res = verify_image_with_claude_vision(c["bytes"], property_name_hint=prop_name)
        return i, c, res

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        check_results = list(pool.map(_verify_url_cand, enumerate(candidate_images)))

    check_results.sort(key=lambda x: x[0])
    for idx, cand, v_result in check_results:
        if v_result.get("is_valid", False):
            verified_images.append({
                "bytes": cand["bytes"],
                "mime": cand["mime"],
                "image_type": v_result.get("image_type", "exterior"),
                "category": v_result.get("category"),
                "alt_text": v_result.get("alt_text", f"{prop_name} Image {idx+1}"),
                "description": v_result.get("description", "")
            })

    # Upload verified images to Firebase/GCS
    uploaded_urls: List[str] = []
    bucket = cloud_storage.bucket_name
    for idx, img_info in enumerate(verified_images):
        img_type = img_info.get("image_type", "photo")
        obj_name = f"properties/{prop_id}/images/{img_type}_{idx+1}.jpg"
        try:
            pub_url = _upload_and_make_public(bucket, obj_name, img_info["bytes"], img_info.get("mime", "image/jpeg"))
            uploaded_urls.append(pub_url)
            img_info["public_url"] = pub_url
        except Exception as e:
            logger.error(f"Failed to upload verified image {obj_name}: {e}")

    cover_image_url = uploaded_urls[0] if uploaded_urls else None

    payload = {
        "property_id": prop_id,
        "property_name": prop_name,
        "project_name": ai_data.get("project_name") or prop_name,
        "property_type": ai_data.get("property_type") or "Apartment",
        "status": ai_data.get("status") or "Available",
        "country": ai_data.get("country") or "India",
        "province": ai_data.get("province") or "Gujarat",
        "district": ai_data.get("district"),
        "subdistrict": ai_data.get("subdistrict"),
        "area": ai_data.get("area") or ai_data.get("province"),
        "address": ai_data.get("address") or f"{ai_data.get('area')}, {ai_data.get('province')}, {ai_data.get('country')}",
        "latitude": ai_data.get("latitude"),
        "longitude": ai_data.get("longitude"),
        "developer_name": ai_data.get("developer_name"),
        "price_display": ai_data.get("price_display") or "Price on Request",
        "price_numeric_min": ai_data.get("price_numeric_min") or 0.0,
        "price_numeric_max": ai_data.get("price_numeric_max"),
        "currency": ai_data.get("currency") or "INR",
        "price_note": ai_data.get("price_note"),
        "bedrooms_display": ai_data.get("bedrooms_display"),
        "bedrooms_min": ai_data.get("bedrooms_min"),
        "bedrooms_max": ai_data.get("bedrooms_max"),
        "bathrooms": ai_data.get("bathrooms"),
        "area_display": ai_data.get("area_display"),
        "area_sqm_min": ai_data.get("area_sqm_min"),
        "area_sqm_max": ai_data.get("area_sqm_max"),
        "units": ai_data.get("units"),
        "total_floors": ai_data.get("total_floors"),
        "has_pool": bool(ai_data.get("has_pool", False)),
        "beach_access": bool(ai_data.get("beach_access", False)),
        "sea_view": bool(ai_data.get("sea_view", False)),
        "amenities": ai_data.get("amenities", []),
        "features": ai_data.get("features", []),
        "cover_image": cover_image_url,
        "images": uploaded_urls,
        "unit_types": ai_data.get("unit_types", []),
        "specs": ai_data.get("specs", {})
    }

    if save_to_database:
        _save_to_postgresql_and_firestore(payload, verified_images)

    return payload


def _upload_and_make_public(bucket: str, obj_name: str, content: bytes, content_type: str) -> str:
    """Uploads asset to Firebase/GCS bucket and returns direct public web URL."""
    token = cloud_storage._get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}

    quoted_name = urllib.parse.quote(obj_name, safe="")
    upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=media&name={quoted_name}"

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(upload_url, headers=headers, content=content)
        resp.raise_for_status()

    return f"https://storage.googleapis.com/{bucket}/{obj_name}"


def _save_to_postgresql_and_firestore(payload: Dict[str, Any], verified_images: List[Dict[str, Any]]):
    """Saves to PostgreSQL and synchronizes both Firestore collections (projects and properties)."""
    db = SessionLocal()
    pid = payload["property_id"]
    try:
        # Upsert PostgreSQL Property
        p = db.query(Property).filter(Property.property_id == pid).first()
        if not p:
            p = Property(
                property_id=pid,
                property_name=payload["property_name"],
                project_name=payload.get("project_name"),
                property_type=payload.get("property_type"),
                status=payload.get("status"),
                country=payload.get("country"),
                province=payload.get("province"),
                district=payload.get("district"),
                subdistrict=payload.get("subdistrict"),
                area=payload.get("area"),
                address=payload.get("address"),
                latitude=payload.get("latitude"),
                longitude=payload.get("longitude"),
                developer_name=payload.get("developer_name"),
                price_display=payload.get("price_display"),
                price_numeric_min=payload.get("price_numeric_min"),
                price_numeric_max=payload.get("price_numeric_max"),
                currency=payload.get("currency"),
                price_note=payload.get("price_note"),
                bedrooms_display=payload.get("bedrooms_display"),
                bedrooms_min=payload.get("bedrooms_min"),
                bedrooms_max=payload.get("bedrooms_max"),
                bathrooms=payload.get("bathrooms"),
                area_display=payload.get("area_display"),
                area_sqm_min=payload.get("area_sqm_min"),
                area_sqm_max=payload.get("area_sqm_max"),
                units=payload.get("units"),
                total_floors=payload.get("total_floors"),
                has_pool=payload.get("has_pool", False),
                beach_access=payload.get("beach_access", False),
                sea_view=payload.get("sea_view", False),
                amenities=payload.get("amenities", []),
                features=payload.get("features", []),
                raw_metadata={"unit_types": payload.get("unit_types", [])}
            )
            db.add(p)
        else:
            p.property_name = payload["property_name"]
            p.price_display = payload.get("price_display")
            p.price_numeric_min = payload.get("price_numeric_min")
            p.amenities = payload.get("amenities", [])
            p.raw_metadata = {"unit_types": payload.get("unit_types", [])}

        db.flush()

        # Specs
        specs = payload.get("specs", {})
        if specs:
            spec_rec = db.query(PropertySpec).filter(PropertySpec.property_id == pid).first()
            if not spec_rec:
                spec_rec = PropertySpec(
                    property_id=pid,
                    roof_material=specs.get("roof_material"),
                    flooring=specs.get("flooring"),
                    walls=specs.get("walls"),
                    doors_windows=specs.get("doors_windows"),
                    architectural_style=specs.get("architectural_style")
                )
                db.add(spec_rec)
            else:
                spec_rec.flooring = specs.get("flooring")
                spec_rec.doors_windows = specs.get("doors_windows")

        # PropertyImages
        db.query(PropertyImage).filter(PropertyImage.property_id == pid).delete()
        for idx, img_info in enumerate(verified_images):
            if "public_url" in img_info:
                rec = PropertyImage(
                    property_id=pid,
                    image_url=img_info["public_url"],
                    image_type=img_info.get("image_type", "exterior"),
                    alt_text=img_info.get("alt_text"),
                    sort_order=idx + 1
                )
                db.add(rec)

        db.commit()
        logger.info(f"Saved {pid} to PostgreSQL successfully")

        # Firestore Synchronized Write
        try:
            import app.services.firestore_service as fs
            fs_client = fs.get_firestore_client()
            fs_payload = {
                "property_id": pid,
                "name": payload["property_name"],
                "project_name": payload.get("project_name"),
                "property_type": payload.get("property_type"),
                "status": payload.get("status"),
                "country": payload.get("country"),
                "province": payload.get("province"),
                "district": payload.get("district"),
                "area": payload.get("area"),
                "address": payload.get("address"),
                "price": payload.get("price_display"),
                "price_numeric_min": payload.get("price_numeric_min"),
                "price_numeric_max": payload.get("price_numeric_max"),
                "currency": payload.get("currency"),
                "bedrooms": payload.get("bedrooms_display"),
                "bathrooms": payload.get("bathrooms"),
                "area_sqm_min": payload.get("area_sqm_min"),
                "total_units": payload.get("units"),
                "total_floors": payload.get("total_floors"),
                "amenities": payload.get("amenities", []),
                "features": payload.get("features", []),
                "images": payload.get("images", []),
                "image_urls": payload.get("images", []),
                "cover_image": payload.get("cover_image"),
                "unit_types": payload.get("unit_types", [])
            }

            for coll in ("projects", "properties"):
                fs_client.collection(coll).document(pid).set(fs_payload, merge=True)

            fs.clear_property_catalog_cache()
            logger.info(f"Synchronized {pid} to Firestore collections projects & properties")
        except Exception as fe:
            logger.warning(f"Firestore sync warning: {fe}")

    except Exception as e:
        db.rollback()
        logger.error(f"Failed persisting {pid}: {e}")
        raise
    finally:
        db.close()


def _deterministic_fallback(raw_text: str, filename: str) -> Dict[str, Any]:
    """Rule-based regex fallback when Claude API is unreachable."""
    prop_type = normalize_property_type(raw_text)
    status = normalize_status(raw_text)
    p_min, p_max, p_disp, curr = parse_and_normalize_price(raw_text)
    bed_min, bed_max, bed_disp, baths = parse_bedrooms_and_bathrooms(raw_text)
    a_min, a_max, a_disp, _ = parse_and_normalize_area(raw_text)
    floors, units = parse_floors_and_units(raw_text)

    return {
        "property_name": os.path.splitext(filename)[0].replace("_", " ").title(),
        "project_name": os.path.splitext(filename)[0].replace("_", " ").title(),
        "property_type": prop_type,
        "status": status,
        "country": "India" if "gujarat" in raw_text.lower() else "Thailand",
        "province": "Gujarat" if "gujarat" in raw_text.lower() else "Phuket",
        "district": "Nikol" if "nikol" in raw_text.lower() else None,
        "area": "Nikol" if "nikol" in raw_text.lower() else "Phuket",
        "address": "Nikol, Ahmedabad, Gujarat, India" if "nikol" in raw_text.lower() else "Phuket, Thailand",
        "price_display": p_disp or "Price on Request",
        "price_numeric_min": p_min or 0.0,
        "price_numeric_max": p_max,
        "currency": curr or "INR",
        "bedrooms_display": bed_disp,
        "bedrooms_min": bed_min,
        "bedrooms_max": bed_max,
        "bathrooms": baths,
        "area_display": a_disp,
        "area_sqm_min": a_min,
        "area_sqm_max": a_max,
        "units": units,
        "total_floors": floors,
        "has_pool": "pool" in raw_text.lower(),
        "beach_access": "beach" in raw_text.lower(),
        "sea_view": "sea view" in raw_text.lower(),
        "amenities": ["Club House", "Parking"] if "club house" in raw_text.lower() else [],
        "features": [],
        "specs": {},
        "unit_types": []
    }
