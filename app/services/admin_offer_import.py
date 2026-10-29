"""
Create listings from an offer document (PDF, DOCX or TXT) uploaded in the bulk-operations modal.

The extractor is rule based. It reads one listing per section and fills only values the document states;
anything else stays missing and blocks that listing. Nothing is written here: the upload stages a pending
action, and the admin must confirm it. Saving goes through save_ingested_listing, the same verified path
the URL import uses (Firestore, undo snapshot, read-back).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

logger = logging.getLogger(__name__)

TOOL_NAME = "bulk_create_from_document"
OFFER_EXTENSIONS = {"pdf", "docx", "txt"}
PAGE_BREAK = "\n\f\n"  # a listing section never runs past the end of its page
_REQUIRED = ("property_name", "property_type", "province", "price", "status")

_SPACES = re.compile(r"[   \t]+")
# "City Garden Tropicana — przy Wongamat" followed by "Pattaya · Wongamat (Naklua), ..."
_HEADER = re.compile(r"^(?P<name>[^\n—–]{3,80}?)\s+[—–]\s+(?P<tag>.+)$")
_LOCATION = re.compile(r"^(?P<city>[^\s·]+(?: [^\s·]+)?)\s*·\s*(?P<area>.+)$")
_THB = re.compile(r"(?P<n>\d{1,3}(?: \d{3})+|\d{4,})\s*THB")
_UNIT = re.compile(
    r"(?P<kind>Studio|(?P<beds>\d+)\s*sypialni\w*)\s*·\s*"
    r"(?P<a>\d+(?:,\d+)?)(?:\s*[–-]\s*(?P<b>\d+(?:,\d+)?))?\s*m²",
    re.IGNORECASE,
)


def read_document_text(file_bytes: bytes, ext: str) -> str:
    """Text of an uploaded document. pdfium is preferred because pypdf drops ligatures ("Pattaya" -> "Paaya")."""
    ext = (ext or "").lower()
    if ext == "pdf":
        try:
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(file_bytes)
            try:
                return PAGE_BREAK.join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
            finally:
                doc.close()
        except Exception as exc:
            logger.info("pdfium text extraction unavailable, using pypdf: %s", exc)
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        return PAGE_BREAK.join((page.extract_text() or "") for page in reader.pages)
    if ext == "docx":
        import docx

        document = docx.Document(io.BytesIO(file_bytes))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                parts.append(" ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    return file_bytes.decode("utf-8", errors="replace")


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _clean_lines(text: str) -> List[str]:
    cleaned = _SPACES.sub(" ", (text or "").replace("\r", "\n"))
    return ["\f" if "\f" in line else line.strip() for line in cleaned.split("\n")]  # keep page breaks as "\f" lines


def _sections(lines: List[str]) -> List[Tuple[Dict[str, str], str]]:
    """Split the document at every 'Name - tagline' line that is followed by a 'City . Area' line."""
    starts: List[Tuple[int, Dict[str, str]]] = []
    for i, line in enumerate(lines[:-1]):
        header = _HEADER.match(line)
        if not header or len(header.group("name").split()) > 7:
            continue
        nxt = next((l for l in lines[i + 1:i + 3] if l), "")
        location = _LOCATION.match(nxt)
        if location:
            starts.append((i, {"name": header.group("name").strip(), "city": location.group("city").strip()}))
    out: List[Tuple[Dict[str, str], str]] = []
    for pos, (i, info) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        end = next((j for j in range(i, end) if lines[j] == "\f"), end)
        out.append((info, "\n".join(lines[i:end])))
    return out


def extract_offer_listings(text: str) -> List[Dict[str, Any]]:
    """
    One draft per section: {"fields", "provenance", "missing", "label"}.
    Values come only from the text. Missing required values are listed, never filled in.
    """
    from app.services.action_service import ALLOWED_PROPERTY_TYPES, ALLOWED_STATUSES

    drafts: List[Dict[str, Any]] = []
    seen = set()
    for info, body in _sections(_clean_lines(text)):
        name = info["name"]
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        fields: Dict[str, Any] = {"property_name": name}
        prov: Dict[str, Dict[str, Any]] = {
            "property_name": {"value": name, "status": "found", "source": "document", "evidence": "section title"},
        }

        def put(field: str, value: Any, evidence: str) -> None:
            fields[field] = value
            prov[field] = {"value": value, "status": "found", "source": "document", "evidence": evidence}

        put("province", info["city"], f"location line '{info['city']} · ...'")
        if re.search(r"mieszkani|apartament|apartment", body, re.IGNORECASE):
            put("property_type", ALLOWED_PROPERTY_TYPES["apartment"], "the section describes apartments (mieszkanie)")
        if re.search(r"\bgotow\w*|\boddan\w*|\bready\b", body, re.IGNORECASE):
            put("status", ALLOWED_STATUSES["ready to move in"], "the section says the building is ready (gotowe/oddany)")

        prices = sorted({int(m.group("n").replace(" ", "")) for m in _THB.finditer(body)})
        if prices:
            put("price_numeric_min", prices[0], f"{prices[0]:,} THB listed in the section")
            if prices[-1] != prices[0]:
                put("price_numeric_max", prices[-1], f"{prices[-1]:,} THB listed in the section")
            put("currency", "THB", "prices are given in THB")

        units = list(_UNIT.finditer(body))
        if units:
            beds = [0 if u.group("kind").lower() == "studio" else int(u.group("beds")) for u in units]
            areas = [_number(u.group(g)) for u in units for g in ("a", "b") if u.group(g)]
            put("bedrooms_min", min(beds), "unit lines such as '1 sypialnia · 32,46 m²'")
            put("bedrooms_max", max(beds), "unit lines such as '1 sypialnia · 32,46 m²'")
            put("bedrooms", min(beds), "lowest bedroom count among the listed units")
            put("area_sqm_min", min(areas), "unit areas in m²")
            if max(areas) != min(areas):
                put("area_sqm_max", max(areas), "unit areas in m²")

        missing = [f for f in _REQUIRED if (f == "price" and "price_numeric_min" not in fields) or (f != "price" and f not in fields)]
        drafts.append({"fields": fields, "provenance": prov, "missing": missing, "label": name})
    return drafts


def build_import_payload(db: Any, staff: Any, drafts: List[Dict[str, Any]], filename: str) -> Dict[str, Any]:
    """Attach duplicate info and target websites. Pure data: nothing is written."""
    from app.services.property_web_ingest import _target_websites, find_duplicate

    listings = []
    for draft in drafts:
        duplicate = None
        try:
            duplicate = find_duplicate(db, f"file:{filename}", draft["fields"]["property_name"])
        except Exception as exc:
            logger.debug("duplicate scan skipped: %s", exc)
        listings.append({**draft, "duplicate": duplicate})
    return {
        "source_filename": filename,
        "websites": _target_websites(staff, None),
        "listings": listings,
    }


def preview_summary(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for item in payload.get("listings") or []:
        f = item["fields"]
        if item.get("duplicate"):
            outcome = f"Skipped: already exists as {item['duplicate'].get('property_id')}"
        elif item["missing"]:
            outcome = "Cannot create: missing " + ", ".join(item["missing"])
        else:
            outcome = "Ready to create"
        rows.append({
            "name": f.get("property_name"),
            "province": f.get("province"),
            "property_type": f.get("property_type"),
            "status": f.get("status"),
            "price_min": f.get("price_numeric_min"),
            "price_max": f.get("price_numeric_max"),
            "bedrooms_min": f.get("bedrooms_min"),
            "bedrooms_max": f.get("bedrooms_max"),
            "area_sqm_min": f.get("area_sqm_min"),
            "area_sqm_max": f.get("area_sqm_max"),
            "outcome": outcome,
            "ready": not item["missing"] and not item.get("duplicate"),
        })
    return rows


def stage_document_import(db: Any, staff: Any, file_bytes: bytes, filename: str) -> Optional[Dict[str, Any]]:
    """Extract listings and stage ONE pending action. Returns None when the document has no listing sections."""
    from app.services.action_service import create_pending_action

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in OFFER_EXTENSIONS:
        return None
    drafts = extract_offer_listings(read_document_text(file_bytes, ext))
    if not drafts:
        return None
    payload = build_import_payload(db, staff, drafts, filename)
    payload["content_hash"] = hashlib.sha256(file_bytes).hexdigest()
    action = create_pending_action(
        db=db,
        staff_user_id=getattr(staff, "email", None) or "admin",
        tool_name=TOOL_NAME,
        action_payload=payload,
        conversation_id=None,
    )
    rows = preview_summary(payload)
    ready = sum(1 for r in rows if r["ready"])
    return {
        "status": "PREVIEW",
        "requires_confirmation": True,
        "pending_action_id": str(action.id),
        "confirmation_token": action.confirmation_token,
        "expires_at": action.expires_at.isoformat() if getattr(action, "expires_at", None) else None,
        "source_filename": filename,
        "websites": payload["websites"],
        "listings": rows,
        "ready_count": ready,
        "message": (
            f"Found {len(rows)} listing(s) in '{filename}'. {ready} can be created. "
            "Nothing has been saved. Review the table, then confirm."
        ),
    }


def execute_document_import(db: Any, payload: Dict[str, Any], staff: Any) -> Dict[str, Any]:
    """Save every ready listing through the verified save path. Raises when nothing could be saved."""
    from app.services.action_service import generate_property_id, save_ingested_listing

    websites = list(payload.get("websites") or [])
    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in payload.get("listings") or []:
        fields = dict(item.get("fields") or {})
        name = str(fields.get("property_name") or "")
        if item.get("duplicate"):
            skipped.append({"name": name, "reason": f"already exists as {item['duplicate'].get('property_id')}"})
            continue
        if item.get("missing"):
            skipped.append({"name": name, "reason": "missing " + ", ".join(item["missing"])})
            continue
        property_id = generate_property_id(db, name)
        draft = {
            "mode": "create",
            "property_id": property_id,
            "fields": fields,
            "websites": websites,
            "provenance": item.get("provenance") or {},
            "admin_fields": [],
            "guessed_fields": [],
            "images": [],
            "source_url": f"file:{payload.get('source_filename')}",
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "content_hash": payload.get("content_hash"),
        }
        try:
            result = save_ingested_listing(db, staff, draft)
        except HTTPException as exc:
            skipped.append({"name": name, "reason": str(exc.detail)})
            continue
        except Exception as exc:
            logger.warning("document import failed for %s: %s", name, exc)
            skipped.append({"name": name, "reason": f"save failed: {exc}"})
            continue
        if result.get("saved"):
            created.append({"name": name, "property_id": property_id, "operation_id": result.get("operation_id")})
        else:
            skipped.append({"name": name, "reason": result.get("message") or "not saved"})
    if not created:
        reasons = "; ".join(f"{s['name']}: {s['reason']}" for s in skipped) or "no listings"
        raise HTTPException(status_code=400, detail=f"Nothing was saved. {reasons}")
    return {"created": created, "skipped": skipped, "source_filename": payload.get("source_filename")}
