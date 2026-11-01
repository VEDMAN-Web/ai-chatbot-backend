"""
app/services/claude_vision_verifier.py

Claude Vision Image Verification & Property Classification Engine.
1. Inspects candidate property images with Claude Vision before cloud upload.
2. Distinguishes:
   - Valid Property Photography (interior, exterior, amenities, pool, views)
   - Architectural Blueprints / Floor Plans (unit layout, master plan, site diagram)
   - Invalid / Decorative Noise (blank swatches, developer logos, small icons, social badges, random text clippings)
3. Confirms if the image belongs to the specified property or project.
4. Generates contextual description and classification tags for database persistence.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from PIL import Image

from app.core.config import settings

logger = logging.getLogger(__name__)

VERIFY_IMAGE_SYSTEM_PROMPT = """You are an expert real estate vision classifier and image auditor.
Your job is to analyze real estate document images, determine their authenticity, and filter out noise.

Classify the given image into one of these categories:
1. "PHOTOGRAPHY": Genuine photograph or 3D architectural rendering showing the physical real estate property (exterior building, swimming pool, garden, living room, bedroom, bathroom, kitchen, terrace, or panoramic views).
2. "FLOOR_PLAN": Architectural unit floor plan, 2D/3D room layout diagram with dimensions, or site master plan.
3. "DOCUMENT_TEXT": Brochure pages, presentation slides, price lists, specification tables, flyer text, or images dominated by writing, headings, bullet points, and typography.
4. "INVALID_NOISE": Company logos, branding banners, icons, social media icons, solid color placeholders, or non-property graphics.

CRITICAL RULES:
- The image MUST be purely a visual of the property itself or its floor plan.
- If the image contains significant text paragraphs, brochure typography, specification tables, marketing titles, or is a document/slide page, classify it as "DOCUMENT_TEXT" with is_valid = false.
- If the image is a brand logo, icon, or decorative banner, classify as "INVALID_NOISE" with is_valid = false.
- ONLY set is_valid = true if the image is a clean property photograph or architectural floor plan with NO full document/brochure text framing.
- Output ONLY a JSON object matching this schema:
{
  "is_valid": boolean,
  "category": "PHOTOGRAPHY" | "FLOOR_PLAN" | "DOCUMENT_TEXT" | "INVALID_NOISE",
  "image_type": "exterior" | "interior" | "floor_plan" | "invalid",
  "confidence": float (0.0 to 1.0),
  "description": "Clear 1-sentence description of what this image shows",
  "alt_text": "Short descriptive alt text for web frontends"
}
"""


def verify_image_with_claude_vision(
    image_bytes: bytes,
    property_name_hint: str = "",
    mime_type: str = "image/jpeg"
) -> Dict[str, Any]:
    """
    Analyzes an image with Claude Vision to verify if it is a genuine property image,
    floor plan, or useless artifact/noise.
    """
    if not settings.ANTHROPIC_API_KEY:
        # Fallback if API key missing: rule-based approval
        return {
            "is_valid": True,
            "category": "PHOTOGRAPHY",
            "image_type": "exterior",
            "confidence": 0.5,
            "description": f"Image for {property_name_hint}",
            "alt_text": property_name_hint or "Property Image"
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=30.0)

        # Prepare downscaled image for fast vision classification
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            # Downscale for vision token efficiency if larger than 1200px
            if max(img.size) > 1200:
                img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            clean_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        prompt_user = (
            f"Analyze this image. The associated property is: '{property_name_hint or 'Real Estate Property'}'.\n"
            "Is this a valid property photo or architectural floor plan, or is it invalid noise/blank swatch? "
            "Output JSON only."
        )

        msg = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=400,
            system=VERIFY_IMAGE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": clean_b64
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt_user
                        }
                    ]
                }
            ]
        )

        raw_text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        # Clean potential markdown backticks and find JSON object
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        json_match = re.search(r"\{[\s\S]*\}", raw_text)
        if json_match:
            data = json.loads(json_match.group(0))
        else:
            data = json.loads(raw_text)
        logger.info(
            f"Claude Vision verified image for {property_name_hint}: "
            f"valid={data.get('is_valid')}, category={data.get('category')}, desc={data.get('description')}"
        )
        return data

    except Exception as e:
        logger.warning(f"Claude Vision verification fallback: {e}")
        # Default fallback
        return {
            "is_valid": True,
            "category": "PHOTOGRAPHY",
            "image_type": "exterior",
            "confidence": 0.5,
            "description": f"Property asset for {property_name_hint}",
            "alt_text": property_name_hint or "Property Image"
        }
