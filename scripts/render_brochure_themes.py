#!/usr/bin/env python3
"""
Render all 7 brochure themes for one property into an output folder.

Usage:
  python scripts/render_brochure_themes.py --property-id PROP --lang en --out data/brochure_theme_qa

Also writes PNG thumbnails of every page when pdftoppm or pypdfium2 is available.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("render_brochure_themes")


def _pdf_to_pngs(pdf_path: Path, out_dir: Path, stem: str) -> int:
    """Best-effort page thumbnails. Returns number of PNGs written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Try pypdfium2
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(pdf_path))
        count = 0
        for i, page in enumerate(doc):
            bitmap = page.render(scale=1.5)
            pil = bitmap.to_pil()
            dest = out_dir / f"{stem}_p{i + 1:02d}.png"
            pil.save(dest, "PNG")
            count += 1
        return count
    except Exception as e:
        logger.debug("pypdfium2 thumbnails skipped: %s", e)

    # Try pdftoppm
    import shutil
    import subprocess

    if shutil.which("pdftoppm"):
        prefix = out_dir / stem
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "110", str(pdf_path), str(prefix)],
                check=True,
                capture_output=True,
            )
            return len(list(out_dir.glob(f"{stem}*.png")))
        except Exception as e:
            logger.warning("pdftoppm failed: %s", e)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Render brochure themes 1–7 for QA")
    parser.add_argument("--property-id", required=True)
    parser.add_argument("--lang", default="en", choices=["en", "pl", "th"])
    parser.add_argument("--site-origin", default=None)
    parser.add_argument(
        "--out",
        default="data/brochure_theme_qa",
        help="Output folder for theme_N.pdf and PNG thumbs",
    )
    parser.add_argument("--no-png", action="store_true")
    args = parser.parse_args()

    from app.db.session import SessionLocal
    from app.services.brochure_data import load_brochure_data
    from app.services.brochure_renderer import build_brochure_html, render_brochure_pdf
    from app.services.brochure_themes import THEMES

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        data = load_brochure_data(
            db=db,
            property_id=args.property_id,
            site_origin=args.site_origin,
            language=args.lang,
        )
    finally:
        db.close()

    logger.info(
        "Loaded property=%s name=%s lang=%s",
        data.property_id,
        data.name,
        data.language,
    )

    for theme_id, spec in THEMES.items():
        # Also dump HTML for offline inspection
        html = build_brochure_html(
            data, embed_remote_images=True, theme_id=theme_id, use_legacy_base=False
        )
        html_path = out_dir / f"theme_{theme_id}.html"
        html_path.write_text(html, encoding="utf-8")

        try:
            pdf_bytes = render_brochure_pdf(
                data, use_cache=False, theme_id=theme_id
            )
        except Exception as e:
            logger.error("Theme %s render failed: %s", theme_id, e)
            continue

        pdf_path = out_dir / f"theme_{theme_id}.pdf"
        pdf_path.write_bytes(pdf_bytes)
        logger.info(
            "Wrote %s (%s bytes) theme=%s name=%s",
            pdf_path.name,
            len(pdf_bytes),
            theme_id,
            spec.name,
        )

        if not args.no_png:
            n = _pdf_to_pngs(pdf_path, thumbs, f"theme_{theme_id}")
            logger.info("Thumbnails for theme %s: %s", theme_id, n)

    logger.info("Done. Output: %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
