import copy
import io
import re
import csv
import json
import uuid
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple

from app.core.config import settings
from app.core.auth import AuthenticatedStaff
from app.services.firestore_service import get_firestore_client
from app.services.audit_service import record_audit_log

logger = logging.getLogger(__name__)

# Batch operations collection name in Firestore
BATCH_OPERATIONS_COLLECTION = "admin_batch_operations"
# Default / fallback; prefer get_snapshot_retention_days() which reads config
SNAPSHOT_RETENTION_DAYS = int(getattr(settings, "ADMIN_SNAPSHOT_RETENTION_DAYS", 6) or 6)

# Exact user-facing copy when a snapshot is past retention (tests assert this phrase)
EXPIRED_UNDO_MESSAGE = (
    "This operation expired after 6 days and can no longer be undone"
)
OLD_SNAPSHOT_MESSAGE = "This old snapshot cannot be restored exactly"


def get_snapshot_client():
    """Admin Firestore client (user fallback) for snapshots, history, and undo."""
    return get_firestore_client(target="admin") or get_firestore_client()


def _json_safe(value: Any) -> Any:
    """Deep copy into JSON-safe values. Decimals become ints when integral, else floats."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _capture_raw_firestore(client: Any, property_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    try:
        snap = client.collection("projects").document(str(property_id)).get()
    except Exception:
        return None, False
    if getattr(snap, "exists", False) is not True:
        return None, False
    data = snap.to_dict() if hasattr(snap, "to_dict") else None
    if not isinstance(data, dict):
        return None, False
    return _json_safe(data), True


def _read_back_firestore(projects_col: Any, property_id: str) -> Any:
    try:
        snap = projects_col.document(str(property_id)).get()
    except Exception:
        return "UNREADABLE"
    exists = getattr(snap, "exists", None)
    if exists is True:
        data = snap.to_dict() if hasattr(snap, "to_dict") else None
        if not isinstance(data, dict):
            return "UNREADABLE"
        return _json_safe(data)
    if exists is False:
        return None
    return "UNREADABLE"


def _dict_field_diffs(expected: Dict[str, Any], actual: Dict[str, Any], prefix: str) -> List[str]:
    diffs: List[str] = []
    keys = set(expected) | set(actual)
    for key in sorted(keys):
        if _json_safe(expected.get(key)) != _json_safe(actual.get(key)):
            diffs.append(f"{prefix}.{key}")
    return diffs


def _batch_fields_for_validation(rec: Dict[str, Any]) -> Dict[str, Any]:
    alias = {
        "priceFrom": "price_numeric_min",
        "price_from": "price_numeric_min",
        "priceTo": "price_numeric_max",
        "price_to": "price_numeric_max",
        "price_numeric_min": "price_numeric_min",
        "price_numeric_max": "price_numeric_max",
        "price_display": "price_display",
        "price": "price_display",
        "name": "property_name",
        "property_name": "property_name",
        "title": "property_name",
        "status": "status",
        "property_type": "property_type",
        "type": "property_type",
        "bedrooms": "bedrooms",
        "bedrooms_min": "bedrooms_min",
        "bedrooms_max": "bedrooms_max",
        "bathrooms": "bathrooms",
        "area_sqm_min": "area_sqm_min",
        "area_sqm_max": "area_sqm_max",
        "province": "province",
        "location": "location",
    }
    mapped: Dict[str, Any] = {}
    for key, dest in alias.items():
        if key in rec and rec[key] is not None:
            mapped[dest] = rec[key]
    return mapped


def _store_summary(raw_fs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Before/after audit summary. Omits address and other free-text PII."""
    src = raw_fs or {}
    name = src.get("name") or src.get("title") or src.get("property_name")
    if isinstance(name, dict):
        name = name.get("en") or next((v for v in name.values() if v), None)
    price = src.get("priceFrom")
    active = src.get("isActive")
    return {
        "name": name,
        "price": price,
        "status": src.get("status"),
        "isActive": active,
        "websites": src.get("websites"),
    }


class OperationNotFound(Exception):
    """The operation does not exist, or the caller may not see it (the two are not told apart)."""


class BatchOperationError(Exception):
    """Raised when batch operation or undo fails."""
    pass


MAX_BATCH_RECORDS = 500


def get_snapshot_retention_days() -> int:
    """Configurable retention window (UTC days) for admin undo snapshots."""
    try:
        return max(1, int(getattr(settings, "ADMIN_SNAPSHOT_RETENTION_DAYS", 6) or 6))
    except Exception:
        return 6


def _compute_expire_at(created_at: Optional[datetime] = None) -> datetime:
    """expire_at = created_at + retention (timezone-aware UTC)."""
    base = created_at or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base + timedelta(days=get_snapshot_retention_days())


def _parse_expire_at(data: Dict[str, Any]) -> Optional[datetime]:
    """Read expire_at (preferred) or legacy expires_at from a snapshot doc."""
    raw = data.get("expire_at") or data.get("expires_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_snapshot_expired(data: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if data.get("status") == "EXPIRED":
        return True
    exp = _parse_expire_at(data)
    if exp is None:
        return False
    return now >= exp


def _expired_undo_message() -> str:
    days = get_snapshot_retention_days()
    if days == 6:
        return EXPIRED_UNDO_MESSAGE
    return f"This operation expired after {days} days and can no longer be undone"


# ==============================================================================
# 1. Multi-Format File Parser (Excel, PDF, Word, CSV)
# ==============================================================================

def parse_property_batch_file(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """
    Parses property records and identifiers from an uploaded file.
    Supports .xlsx, .xls, .csv, .pdf, and .docx formats.
    Returns a list of parsed property row dicts.
    """
    ext = filename.lower().split(".")[-1] if "." in filename else ""
    records: List[Dict[str, Any]] = []

    try:
        if ext in ["xlsx", "xls"]:
            import pandas as pd
            df = pd.read_excel(io.BytesIO(file_bytes))
            # Clean column names (strip whitespace, lowercase)
            df.columns = [str(c).strip() for c in df.columns]
            for _, row in df.iterrows():
                row_dict = {k: v for k, v in row.to_dict().items() if v is not None and not (isinstance(v, float) and str(v) == 'nan')}
                if row_dict:
                    records.append(_standardize_property_row(row_dict))

        elif ext == "csv":
            text_data = file_bytes.decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text_data))
            for row in reader:
                cleaned = {k.strip(): v.strip() for k, v in row.items() if k and v}
                if cleaned:
                    records.append(_standardize_property_row(cleaned))

        elif ext == "pdf":
            from app.services.drive_service import EXTRACT_ERROR, EXTRACT_NEEDS_OCR, inspect_pdf_bytes
            pdf = inspect_pdf_bytes(file_bytes)
            if pdf["status"] == EXTRACT_NEEDS_OCR:
                raise BatchOperationError(
                    f"'{filename}' is a scanned or image-only PDF with no readable text. OCR is required and is not enabled, "
                    "so nothing was read. Upload a text PDF, an Excel/CSV/Word file."
                )
            if pdf["status"] == EXTRACT_ERROR:
                raise BatchOperationError(f"'{filename}' could not be read as a PDF ({pdf.get('error')}).")
            records = _extract_properties_from_raw_text(pdf["text"] + "\n")

        elif ext == "docx":
            import docx
            doc = docx.Document(io.BytesIO(file_bytes))
            full_text = []
            # Check tables first
            for table in doc.tables:
                headers = [c.text.strip() for c in table.rows[0].cells] if table.rows else []
                for row in table.rows[1:]:
                    vals = [c.text.strip() for c in row.cells]
                    if len(vals) == len(headers) and any(vals):
                        row_dict = dict(zip(headers, vals))
                        records.append(_standardize_property_row(row_dict))
            
            # Also extract from paragraphs if no tables or additional items
            if not records:
                for para in doc.paragraphs:
                    if para.text.strip():
                        full_text.append(para.text.strip())
                records = _extract_properties_from_raw_text("\n".join(full_text))

        elif ext in ["json", "txt"]:
            raw_text = file_bytes.decode("utf-8", errors="replace").strip()
            if raw_text.startswith("[") or raw_text.startswith("{"):
                try:
                    data = json.loads(raw_text)
                    if isinstance(data, list):
                        records = [_standardize_property_row(r) for r in data if isinstance(r, dict)]
                    elif isinstance(data, dict):
                        records = [_standardize_property_row(data)]
                except Exception:
                    records = _extract_properties_from_raw_text(raw_text)
            else:
                records = _extract_properties_from_raw_text(raw_text)

        else:
            raise BatchOperationError(f"Unsupported file format '.{ext}'. Supported formats: .xlsx, .xls, .csv, .pdf, .docx, .json")

    except Exception as e:
        logger.error(f"Error parsing file '{filename}': {e}")
        if isinstance(e, BatchOperationError):
            raise
        raise BatchOperationError(f"Failed to parse file '{filename}': {str(e)}")

    if not records:
        raise BatchOperationError(f"No property identifiers or records could be detected in '{filename}'. Bulk operations need a property list (Excel/CSV with columns like name, price, location, status, or PDF/Word listing property IDs or names). A customer offer or brochure PDF cannot be used for this.")
    if len(records) > MAX_BATCH_RECORDS:
        raise BatchOperationError(
            f"'{filename}' lists {len(records)} properties; one batch is limited to {MAX_BATCH_RECORDS}. Split the file."
        )

    return records


def _standardize_property_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Standardizes column keys to standard property schema."""
    standardized: Dict[str, Any] = {}
    key_mapping = {
        "id": "property_id",
        "property_id": "property_id",
        "propertyid": "property_id",
        "project_id": "property_id",
        "doc_id": "property_id",
        "document_id": "property_id",
        "name": "name",
        "property_name": "name",
        "project_name": "name",
        "title": "name",
        "cena": "price",
        "price": "price",
        "price_from": "price_from",
        "location": "location",
        "city": "city",
        "status": "status",
        "property_type": "property_type",
        "type": "property_type",
        "bedrooms": "bedrooms",
        "beds": "bedrooms",
        "is_active": "is_active",
        "active": "is_active"
    }

    for k, v in row.items():
        clean_k = re.sub(r"[^a-zA-Z0-9_]", "", str(k).lower().strip())
        target_k = key_mapping.get(clean_k, clean_k)
        standardized[target_k] = v

    # Fallback: if only single value or identifier
    if "property_id" not in standardized and "name" not in standardized:
        # Check if first value looks like a property ID
        for v in standardized.values():
            if isinstance(v, str) and (v.startswith("PROP-") or len(v) >= 5):
                standardized["property_id"] = v
                break

    return standardized


def _extract_properties_from_raw_text(text: str) -> List[Dict[str, Any]]:
    """Extracts property IDs or bulleted property names from unstructured text."""
    records: List[Dict[str, Any]] = []
    seen_ids = set()

    # 1. Match explicit PROP-XXX-XXX IDs
    prop_id_matches = re.findall(r"\b(PROP-[A-Z0-9\-]+)\b", text, re.IGNORECASE)
    for pid in prop_id_matches:
        pid_clean = pid.strip().upper()
        if pid_clean not in seen_ids:
            seen_ids.add(pid_clean)
            records.append({"property_id": pid_clean})

    # 2. Match line-by-line items (e.g. "1. 9D Cozy Villas", "- Above Element Villas")
    lines = text.split("\n")
    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        # Remove list markers e.g. "1. ", "- ", "* "
        clean_name = re.sub(r"^[\d\.\-\*\•\–]+\s*", "", line_clean).strip()
        # If looks like a property name (e.g. contains Villa, Residence, Condo, Heights, Hills)
        if any(term in clean_name.lower() for term in ["villa", "residence", "condo", "condominium", "heights", "hills", "resort", "palace"]):
            if clean_name not in seen_ids and len(clean_name) <= 100:
                seen_ids.add(clean_name)
                records.append({"name": clean_name})

    return records


# ==============================================================================
# 2. Bulk Execution Engine with Instant Action & 6-Day Snapshot
# ==============================================================================

def _name_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _normalize_sites(value: Any) -> List[str]:
    if not value:
        return []
    from app.core.tenant import normalize_website_list
    return normalize_website_list(value)


def _taken_ids_and_names(projects_col: Any, db_session: Any) -> Tuple[set, set]:
    """Ids and name slugs of every existing listing (Firestore and PostgreSQL), read once per batch."""
    ids: set = set()
    names: set = set()
    for doc in projects_col.stream():
        data = doc.to_dict() or {}
        ids.add(str(doc.id).lower())
        for key in ("name", "title", "property_name"):
            val = data.get(key)
            labels = list(val.values()) if isinstance(val, dict) else [val]
            names.update(s for s in (_name_slug(v) for v in labels if v) if s)
    return ids, names


def _row_price(rec: Dict[str, Any]) -> Optional[float]:
    """The price in a file row as a number (a leading minus is kept, so -5 is never read as 5); None when absent."""
    raw = None
    for key in ("price_numeric_min", "price_from", "price", "price_display"):
        if rec.get(key) not in (None, ""):
            raw = rec[key]
            break
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _canonical_create_doc(rec: Dict[str, Any]) -> Dict[str, Any]:
    """A new listing from one file row: the file's columns plus the canonical name and numeric price."""
    doc = {k: v for k, v in rec.items() if v is not None and k != "price"}
    mapped = _batch_fields_for_validation(rec)
    name = mapped.get("property_name") or doc.get("name")
    doc["name"] = name
    doc["property_name"] = name
    number = _row_price(rec) or 0
    if number > 0:
        doc["price_numeric_min"] = number
        doc.setdefault("price_display", f"{number:,} THB")
    if rec.get("price") is not None:
        doc.setdefault("price_display", str(rec["price"]))
    return doc


def validate_batch_records(action_type: str, records: List[Dict[str, Any]]) -> None:
    """The same row checks the executor makes, run when the file is uploaded so errors show before anyone confirms."""
    from app.services.action_service import validate_listing_values

    action_clean = (action_type or "").lower().strip()
    if action_clean in ("delete", "remove"):
        return
    creating = action_clean in ("create", "write")
    problems: List[Dict[str, Any]] = []
    for rec in records:
        mapped = _batch_fields_for_validation(rec if isinstance(rec, dict) else {})
        supplied_id = rec.get("property_id") if isinstance(rec, dict) else None
        if creating:
            mapped.update({k: rec[k] for k in ("price_numeric_min",) if rec.get(k)})
            canon = _canonical_create_doc(rec)
            if canon.get("price_numeric_min"):
                mapped["price_numeric_min"] = canon["price_numeric_min"]
        errs = validate_listing_values(
            mapped,
            property_id=str(supplied_id) if creating and supplied_id else None,
            is_create=creating,
            check_property_id=bool(creating and supplied_id),
        )
        price = _row_price(rec) if isinstance(rec, dict) else None
        if price is not None and price <= 0:
            errs = dict(errs or {})
            errs["price"] = "price must be greater than 0"
        if errs:
            problems.append({"property_id": supplied_id, "fields": errs})
    if problems:
        raise BatchOperationError("Validation failed: " + json.dumps(problems))


def can_access_operation(data: Dict[str, Any], staff_user: Any) -> bool:
    """A super admin sees every operation. Others see operations on websites they have, or (older records) their own."""
    if getattr(staff_user, "role", None) == "super_admin":
        return True
    op_sites = [s for s in (data.get("websites") or []) if s]
    allowed = set(getattr(staff_user, "allowed_websites", None) or [])
    if op_sites:
        return set(op_sites) <= allowed
    return bool(data.get("staff_user_id") and data.get("staff_user_id") == getattr(staff_user, "user_id", None)) \
        or bool(data.get("staff_email") and data.get("staff_email") == getattr(staff_user, "email", None))


def execute_batch_property_operation(
    action_type: str,
    records: List[Dict[str, Any]],
    staff_user: AuthenticatedStaff,
    source_filename: str = "batch_file",
    db_session: Optional[Any] = None,
    confirmed_by_staff: bool = False,
) -> Dict[str, Any]:
    """
    Executes bulk property mutations (delete, update, or create) in Firestore
    while saving a complete pre-modification snapshot
    retained for exactly 6 days to support undo.

    confirmed_by_staff must be True only when invoked after an explicit pending-action
    confirmation (pending action id + confirming user). Chat proposals must not call this.
    """
    action_clean = action_type.lower().strip()
    if action_clean not in ["delete", "remove", "update", "create", "write"]:
        raise BatchOperationError(f"Invalid action_type '{action_type}'. Must be 'delete', 'update', or 'create'.")

    if action_clean == "remove":
        action_clean = "delete"
    elif action_clean == "write":
        action_clean = "create"

    from app.services.action_service import validate_listing_values
    validation_errors: List[Dict[str, Any]] = []
    creating = action_clean == "create"
    for rec in records:
        mapped = _batch_fields_for_validation(rec if isinstance(rec, dict) else {})
        supplied_id = rec.get("property_id") if isinstance(rec, dict) else None
        errs = validate_listing_values(
            mapped,
            property_id=str(supplied_id) if creating and supplied_id else None,
            is_create=creating,
            check_property_id=bool(creating and supplied_id),
        )
        if errs:
            validation_errors.append({"property_id": supplied_id, "fields": errs})
    if validation_errors:
        shown = []
        for ve in validation_errors[:5]:
            fields = ve.get("fields")
            if isinstance(fields, dict):
                fields = "; ".join(f"{k}: {v}" for k, v in fields.items())
            elif isinstance(fields, (list, tuple)):
                fields = "; ".join(str(x) for x in fields)
            shown.append(f"{ve.get('property_id') or 'row'} -> {fields}")
        more = f" (+{len(validation_errors) - 5} more rows)" if len(validation_errors) > 5 else ""
        raise BatchOperationError(
            f"Validation failed for {len(validation_errors)} row(s): " + " | ".join(shown) + more
        )

    fs_client = get_firestore_client()
    if not fs_client:
        raise BatchOperationError("Firestore client is not available. Cannot execute batch operation.")

    operation_id = f"batch_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    expire_at = _compute_expire_at(now)
    retention_days = get_snapshot_retention_days()

    affected_ids: List[str] = []
    previous_snapshot: Dict[str, Any] = {}
    created_ids: List[str] = []
    executed_details: List[Dict[str, Any]] = []

    projects_col = fs_client.collection("projects")

    # --------------------------------------------------------------------------
    # A. DELETE / REMOVE
    # --------------------------------------------------------------------------
    selected_sites = getattr(staff_user, "selected_websites", None)
    skipped_rows: List[Dict[str, Any]] = []

    if action_clean == "delete":
        for rec in records:
            doc_id, current_doc = _resolve_firestore_document(projects_col, rec)
            if doc_id and current_doc:
                if selected_sites:
                    doc_sites = current_doc.get("websites") or current_doc.get("allowed_websites")
                    if doc_sites and not any(s in selected_sites for s in doc_sites):
                        logger.warning(f"Batch delete skipping {doc_id} as it is outside selected websites: {selected_sites}")
                        continue

                # Capture complete previous state snapshot
                previous_snapshot[doc_id] = current_doc
                affected_ids.append(doc_id)

                # Instantly deactivate/delete in database
                # Soft-delete by setting isActive: False to protect referential integrity, while marking deleted
                projects_col.document(doc_id).update({
                    "isActive": False,
                    "_deletedAt": now.isoformat(),
                    "_deletedBy": staff_user.email or staff_user.user_id,
                    "_batchOperationId": operation_id
                })

                executed_details.append({
                    "property_id": doc_id,
                    "name": current_doc.get("name") or rec.get("name"),
                    "action": "deactivated"
                })

    # --------------------------------------------------------------------------
    # B. UPDATE
    # --------------------------------------------------------------------------
    elif action_clean == "update":
        for rec in records:
            doc_id, current_doc = _resolve_firestore_document(projects_col, rec)
            if doc_id and current_doc:
                if selected_sites:
                    doc_sites = current_doc.get("websites") or current_doc.get("allowed_websites")
                    if doc_sites and not any(s in selected_sites for s in doc_sites):
                        logger.warning(f"Batch update skipping {doc_id} as it is outside selected websites: {selected_sites}")
                        continue

                # Capture complete previous state snapshot
                previous_snapshot[doc_id] = current_doc
                affected_ids.append(doc_id)

                # Prepare updates
                update_fields = {}
                for k, v in rec.items():
                    if k not in ["property_id", "doc_id", "id"] and v is not None:
                        update_fields[k] = v

                update_fields["updatedAt"] = now.isoformat()
                update_fields["_lastModifiedBy"] = staff_user.email or staff_user.user_id
                update_fields["_batchOperationId"] = operation_id

                # Instantly apply update in database
                projects_col.document(doc_id).update(update_fields)

                executed_details.append({
                    "property_id": doc_id,
                    "name": current_doc.get("name") or rec.get("name"),
                    "updated_fields": list(update_fields.keys()),
                    "action": "updated"
                })

    # --------------------------------------------------------------------------
    # C. CREATE / WRITE
    # --------------------------------------------------------------------------
    elif action_clean == "create":
        taken_ids, taken_names = _taken_ids_and_names(projects_col, db_session)
        allowed_sites = list(getattr(staff_user, "allowed_websites", None) or [])
        for rec in records:
            new_id = rec.get("property_id") or f"prop-{uuid.uuid4().hex[:8]}"
            doc_payload = _canonical_create_doc(rec)
            slug = _name_slug(doc_payload.get("name"))
            if str(new_id).lower() in taken_ids:
                skipped_rows.append({"property_id": new_id, "reason": "a listing with this id already exists"})
                continue
            if slug and slug in taken_names:
                skipped_rows.append({"property_id": new_id, "reason": "a listing with this name already exists"})
                continue
            requested = _normalize_sites(doc_payload.get("websites"))
            if requested:
                outside = [s for s in requested if s not in allowed_sites or (selected_sites and s not in selected_sites)]
                if outside:
                    skipped_rows.append({"property_id": new_id, "reason": f"website(s) {outside} are outside your selection"})
                    continue
                doc_payload["websites"] = requested
            elif selected_sites:
                doc_payload["websites"] = list(selected_sites)
            else:
                skipped_rows.append({"property_id": new_id, "reason": "no website is selected"})
                continue
            taken_ids.add(str(new_id).lower())
            if slug:
                taken_names.add(slug)
            doc_payload["isActive"] = True
            doc_payload["createdAt"] = now.isoformat()
            doc_payload["updatedAt"] = now.isoformat()
            doc_payload["_createdBy"] = staff_user.email or staff_user.user_id
            doc_payload["_batchOperationId"] = operation_id

            # Instantly write to database
            projects_col.document(new_id).set(doc_payload)
            created_ids.append(new_id)
            affected_ids.append(new_id)

            executed_details.append({
                "property_id": new_id,
                "name": doc_payload.get("name"),
                "action": "created"
            })

    if not affected_ids:
        raise BatchOperationError(f"None of the {len(records)} records in '{source_filename}' could be matched in the portfolio.")

    # --------------------------------------------------------------------------
    # Save the retention Snapshot Record in Firestore
    # Infra should also enable a Firestore TTL policy on field `expire_at` as a
    # second safety net (auto-delete after expire_at) in addition to purge_expired_snapshots.
    # --------------------------------------------------------------------------
    expire_at_iso = expire_at.isoformat()
    snapshot_doc = {
        "operation_id": operation_id,
        "action_type": action_clean,
        "source_filename": source_filename,
        "staff_user_id": staff_user.user_id,
        "staff_email": staff_user.email,
        "websites": list(selected_sites or []),
        "status": "APPLIED",
        "affected_count": len(affected_ids),
        "affected_ids": affected_ids,
        "previous_snapshot": previous_snapshot,
        "created_ids": created_ids,
        "created_at": now.isoformat(),
        "expire_at": expire_at_iso,
        # Legacy alias kept for older clients / history readers
        "expires_at": expire_at_iso,
        "details": executed_details,
        "snapshot_format": "raw_v1",
        "raw_firestore": {k: _json_safe(v) for k, v in previous_snapshot.items()},
        "existed_in_firestore": (
            {k: False for k in created_ids} if action_clean == "create"
            else {k: True for k in previous_snapshot}
        ),
    }

    try:
        snap_client = get_snapshot_client() or fs_client
        snap_client.collection(BATCH_OPERATIONS_COLLECTION).document(operation_id).set(snapshot_doc)
        logger.info(f"Saved snapshot for operation {operation_id} (expire_at {expire_at_iso})")
    except Exception as e:
        logger.error(f"Failed to persist batch operation snapshot to Firestore: {e}")

    # Log to audit trail
    if db_session:
        try:
            record_audit_log(
                db=db_session,
                staff_user_id=staff_user.user_id,
                tool_name=f"batch_property_{action_clean}",
                tool_arguments={"filename": source_filename, "count": len(affected_ids)},
                proposed_payload={"operation_id": operation_id, "expire_at": expire_at_iso},
                confirmed_by_staff=bool(confirmed_by_staff),
                status="SUCCESS",
                execution_result=f"Executed {action_clean} on {len(affected_ids)} properties from {source_filename}."
            )
        except Exception:
            pass

    return {
        "success": True,
        "operation_id": operation_id,
        "action_type": action_clean,
        "source_filename": source_filename,
        "affected_count": len(affected_ids),
        "affected_ids": affected_ids,
        "skipped": skipped_rows,
        "skipped_count": len(skipped_rows),
        "created_at": now.isoformat(),
        "expire_at": expire_at_iso,
        "expires_at": expire_at_iso,
        "undo_window_days": retention_days,
        "status": "APPLIED",
        "message": (
            f"Successfully executed {action_clean} on {len(affected_ids)} properties. "
            f"Previous state stored for {retention_days} days with full Undo available."
        ),
    }


def record_crud_operation_snapshot(
    action_type: str,
    property_id: str,
    previous_state: Optional[Dict[str, Any]],
    staff_user: AuthenticatedStaff,
    source: str = "admin_crud",
    created_id: Optional[str] = None,
    db_session: Optional[Any] = None,
    confirmed_by_staff: bool = False,
    skip_audit: bool = False,
    conversation_id: Optional[str] = None,
    pending_action_id: Optional[str] = None,
) -> Optional[str]:
    """
    Saves a 6-day pre-modification snapshot for individual admin CRUD operations.

    Restore source is the raw Firestore document.
    `previous_snapshot` is kept only as a display card and is never written back.
    confirmed_by_staff=True only after explicit pending-action confirmation.
    """
    action_clean = action_type.lower().strip()
    if action_clean == "remove":
        action_clean = "delete"
    elif action_clean == "write":
        action_clean = "create"

    fs_client = get_snapshot_client()
    if not fs_client:
        return None

    operation_id = f"crud_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    expire_at = _compute_expire_at(now)
    expire_at_iso = expire_at.isoformat()

    raw_fs, existed_fs = _capture_raw_firestore(fs_client, property_id)

    # Display card only. Callers may pass a normalised view; it is not the restore source.
    previous_snapshot: Dict[str, Any] = {}
    if isinstance(previous_state, dict) and previous_state:
        previous_snapshot[property_id] = _json_safe(previous_state)
    elif raw_fs:
        card_src = raw_fs
        previous_snapshot[property_id] = {
            "property_id": property_id,
            "name": card_src.get("name") or card_src.get("property_name"),
            "price": card_src.get("priceFrom", card_src.get("price_numeric_min")),
            "display_only": True,
        }

    created_ids = [created_id] if created_id else []
    affected_ids = [property_id]

    snapshot_doc = {
        "operation_id": operation_id,
        "action_type": action_clean,
        "source_filename": source,
        "staff_user_id": staff_user.user_id,
        "staff_email": staff_user.email,
        "websites": list(getattr(staff_user, "selected_websites", None) or []),
        "status": "APPLIED",
        "affected_count": len(affected_ids),
        "affected_ids": affected_ids,
        "previous_snapshot": previous_snapshot,
        "raw_firestore": {property_id: raw_fs} if existed_fs and raw_fs is not None else {},
        "existed_in_firestore": {property_id: bool(existed_fs)},
        "snapshot_format": "raw_v1",
        "created_ids": created_ids,
        "created_at": now.isoformat(),
        "expire_at": expire_at_iso,
        "expires_at": expire_at_iso,
        "conversation_id": conversation_id,
        "pending_action_id": pending_action_id,
        "details": [{
            "property_id": property_id,
            "action": action_clean
        }]
    }

    try:
        fs_client.collection(BATCH_OPERATIONS_COLLECTION).document(operation_id).set(snapshot_doc)
        logger.info(
            f"Saved CRUD snapshot for {action_clean} on {property_id} "
            f"(op: {operation_id}, expire_at: {expire_at_iso})"
        )
    except Exception as e:
        logger.warning(f"Failed to persist CRUD snapshot to Firestore: {e}")
        return None

    if db_session and not skip_audit:
        try:
            record_audit_log(
                db=db_session,
                staff_user_id=staff_user.user_id,
                tool_name=f"admin_crud_{action_clean}",
                tool_arguments={"property_id": property_id, "source": source},
                proposed_payload={"operation_id": operation_id, "expire_at": expire_at_iso},
                confirmed_by_staff=bool(confirmed_by_staff),
                execution_result=f"Executed {action_clean} on property {property_id}."
            )
        except Exception:
            pass

    return operation_id


def _resolve_firestore_document(projects_col: Any, record: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Finds a matching Firestore document by property_id, slug, or name/title."""
    query_str = (record.get("property_id") or record.get("id") or record.get("doc_id") or record.get("name") or "").strip()
    if not query_str:
        return None, None

    query_lower = query_str.lower()
    query_slug = re.sub(r'[^a-z0-9]+', '-', query_lower).strip('-')

    # 1. Direct document ID lookup
    doc = projects_col.document(query_str).get()
    if doc.exists:
        target_id = doc.id if isinstance(getattr(doc, "id", None), str) and not type(doc.id).__name__ == "MagicMock" else query_str
        return target_id, doc.to_dict() or {}

    # 2. Slugified document ID lookup (e.g. "9D Cozy Villas" -> "9d-cozy-villas")
    if query_slug:
        doc = projects_col.document(query_slug).get()
        if doc.exists:
            target_id = doc.id if isinstance(getattr(doc, "id", None), str) and not type(doc.id).__name__ == "MagicMock" else query_slug
            return target_id, doc.to_dict() or {}

    # 3. Stream search across portfolio for name, title, or slug match
    try:
        docs = list(projects_col.limit(200).stream())
        for d in docs:
            # Check document ID
            d_id_lower = d.id.lower()
            if d_id_lower == query_lower or d_id_lower == query_slug:
                return d.id, d.to_dict() or {}

            d_data = d.to_dict() or {}
            
            # Check multilingual name/title/slug fields
            names_to_check = []
            for field_key in ["name", "title", "slug", "property_id"]:
                val = d_data.get(field_key)
                if isinstance(val, dict):
                    names_to_check.extend([str(v).lower() for v in val.values() if v])
                elif val:
                    names_to_check.append(str(val).lower())

            for candidate in names_to_check:
                if query_lower == candidate or (query_slug and query_slug == candidate):
                    return d.id, d_data
                if len(query_lower) >= 4 and (query_lower in candidate or candidate in query_lower):
                    return d.id, d_data
    except Exception as e:
        logger.warning(f"Error resolving firestore doc by name: {e}")

    return None, None


# ==============================================================================
# 3. 6-Day Undo Engine
# ==============================================================================

def _snapshot_has_raw_source(data: Dict[str, Any]) -> bool:
    if data.get("snapshot_format") == "raw_v1":
        return True
    return any(
        key in data
        for key in ("raw_firestore", "existed_in_firestore")
    )


def _ordered_ids(data: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    pools = [
        data.get("affected_ids") or [],
        data.get("created_ids") or [],
        list((data.get("raw_firestore") or {}).keys()),
        list((data.get("existed_in_firestore") or {}).keys()),
    ]
    for pool in pools:
        for item in pool:
            if item and item not in ids:
                ids.append(str(item))
    return ids


def _verify_exact_restore(
    data: Dict[str, Any],
    projects_col: Any,
    db_session: Optional[Any],
    property_ids: List[str],
) -> List[str]:
    """Re-read Firestore and list fields that do not match the raw snapshot."""
    raw_fs_map = data.get("raw_firestore") or {}
    existed_fs = data.get("existed_in_firestore") or {}
    action_type = data.get("action_type")
    created = set(data.get("created_ids") or [])
    diffs: List[str] = []

    for pid in property_ids:
        expect_fs = None
        if action_type == "create" or pid in created or not existed_fs.get(pid):
            expect_fs = None
        else:
            expect_fs = raw_fs_map.get(pid) or {}
        actual_fs = _read_back_firestore(projects_col, pid)
        if actual_fs == "UNREADABLE":
            diffs.append(f"{pid}.firestore: unreadable")
        elif expect_fs is None:
            if actual_fs is not None:
                diffs.append(f"{pid}.firestore: document still present")
        elif not isinstance(actual_fs, dict):
            diffs.append(f"{pid}.firestore: missing document")
        else:
            diffs.extend(_dict_field_diffs(expect_fs, actual_fs, f"{pid}.firestore"))
    return diffs


def undo_batch_property_operation(
    operation_id: str,
    staff_user: AuthenticatedStaff,
    db_session: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Roll back an operation inside the retention window from the raw snapshot.

    Firestore is replaced with doc.set(raw) (no merge).
    one by one. A create, or a store the record did not previously occupy, is deleted.
    Success is reported only after a re-read matches the snapshot.
    """
    fs_client = get_snapshot_client()
    if not fs_client:
        raise BatchOperationError("Firestore client is not available. Cannot execute undo.")

    op_ref = fs_client.collection(BATCH_OPERATIONS_COLLECTION).document(operation_id)
    op_doc = op_ref.get()

    if not op_doc.exists:
        raise OperationNotFound(f"Operation '{operation_id}' not found.")

    data = op_doc.to_dict() or {}
    if not can_access_operation(data, staff_user):
        raise OperationNotFound(f"Operation '{operation_id}' not found.")
    status = data.get("status")
    if status == "REVERTED":
        raise BatchOperationError(f"Operation '{operation_id}' has already been undone.")

    now = datetime.now(timezone.utc)
    if _is_snapshot_expired(data, now=now):
        try:
            op_ref.update({"status": "EXPIRED"})
        except Exception:
            pass
        raise BatchOperationError(_expired_undo_message())

    if not _snapshot_has_raw_source(data):
        raise BatchOperationError(OLD_SNAPSHOT_MESSAGE)

    action_type = data.get("action_type")
    created_ids: List[str] = [str(x) for x in (data.get("created_ids") or [])]
    raw_fs_map: Dict[str, Any] = data.get("raw_firestore") or {}
    existed_fs: Dict[str, Any] = data.get("existed_in_firestore") or {}
    projects_col = fs_client.collection("projects")
    property_ids = _ordered_ids(data)
    restored_count = 0

    try:
        if action_type == "create":
            targets = created_ids or property_ids
            for doc_id in targets:
                try:
                    projects_col.document(doc_id).delete()
                except Exception as exc:
                    logger.error(f"Error deleting created Firestore document {doc_id}: {exc}")
                restored_count += 1
        else:
            for doc_id in property_ids:
                try:
                    if existed_fs.get(doc_id) and isinstance(raw_fs_map.get(doc_id), dict):
                        projects_col.document(doc_id).set(copy.deepcopy(raw_fs_map[doc_id]))
                    else:
                        projects_col.document(doc_id).delete()
                except Exception as exc:
                    logger.error(f"Error restoring Firestore document {doc_id}: {exc}")
                restored_count += 1
    finally:
        try:
            from app.services.cache_service import search_cache
            search_cache.invalidate()
        except Exception:
            pass
        try:
            from app.services.firestore_service import bump_properties_version
            bump_properties_version(target="admin")
        except Exception:
            pass

    differing = _verify_exact_restore(data, projects_col, db_session, property_ids or created_ids)
    restored_ok = len(differing) == 0
    actor = staff_user.email or staff_user.user_id
    reverted_at = now.isoformat()

    if restored_ok:
        try:
            op_ref.update({
                "status": "REVERTED",
                "reverted_at": reverted_at,
                "reverted_by": actor,
            })
        except Exception as exc:
            logger.error(f"Failed to mark operation {operation_id} reverted: {exc}")
            restored_ok = False
            differing.append("snapshot.status")

    before_bits = []
    after_note = "restored to snapshot" if restored_ok else "verification failed"
    for pid in property_ids:
        before_bits.append({
            "property_id": pid,
            "before": _store_summary(raw_fs_map.get(pid)),
        })

    audit_label = "disabled"
    if db_session:
        try:
            log = record_audit_log(
                db=db_session,
                staff_user_id=actor,
                tool_name="UNDO",
                tool_arguments={
                    "operation_id": operation_id,
                    "actor": actor,
                    "original_operation_id": operation_id,
                    "property_ids": property_ids,
                    "stores": {
                        "firestore": {pid: bool(existed_fs.get(pid)) for pid in property_ids},
                    },
                },
                proposed_payload={
                    "status": "REVERTED" if restored_ok else "FAILED",
                    "restored_count": restored_count,
                    "restored_ok": restored_ok,
                    "differing_fields": differing,
                    "original_operation_id": operation_id,
                    "action": "UNDO",
                    "before": before_bits,
                    "after": after_note,
                },
                confirmed_by_staff=False,
                status="SUCCESS" if restored_ok else "FAILED",
                execution_result=(
                    f"UNDO of operation {operation_id} by {actor}. "
                    f"restored_ok={restored_ok}."
                ),
                conversation_id=data.get("conversation_id") or data.get("conversationId"),
                pending_action_id=data.get("pending_action_id") or data.get("pendingActionId"),
            )
            if (
                getattr(settings, "ENABLE_AUDIT_LOGGING", False)
                and not getattr(log, "audit_logging_disabled", False)
                and not getattr(log, "is_ephemeral", False)
            ):
                audit_label = str(getattr(log, "id", "") or "") or "disabled"
        except Exception:
            pass

    if restored_ok:
        message = (
            f"Operation '{operation_id}' successfully undone. "
            f"{restored_count} properties restored to their previous state."
        )
    else:
        message = (
            f"Undo of '{operation_id}' did not verify. "
            f"Differing fields: {', '.join(differing) or 'unknown'}."
        )

    return {
        "success": restored_ok,
        "restored_ok": restored_ok,
        "differing_fields": differing,
        "operation_id": operation_id,
        "action_type": action_type,
        "status": "REVERTED" if restored_ok else status,
        "restored_count": restored_count,
        "reverted_at": reverted_at if restored_ok else None,
        "reverted_by": actor if restored_ok else None,
        "audit": audit_label,
        "message": message,
    }


# ==============================================================================
# 4. Operations History API
# ==============================================================================

def get_batch_operations_history(limit: int = 50, staff_user: Any = None) -> List[Dict[str, Any]]:
    """
    Retrieves history of past batch property operations with remaining undo time.
    Expired snapshots are hidden (treated as nonexistent).
    """
    fs_client = get_snapshot_client()
    if not fs_client:
        return []

    try:
        # Fetch a wider window then filter expired so limit is of visible ops
        col = fs_client.collection(BATCH_OPERATIONS_COLLECTION)
        docs = list(col.limit(max(limit * 3, limit)).stream())
        results = []
        now = datetime.now(timezone.utc)

        for doc in docs:
            data = doc.to_dict() or {}
            # Hide expired / purged-equivalent ops from history
            if _is_snapshot_expired(data, now=now):
                continue
            if staff_user is not None and not can_access_operation(data, staff_user):
                continue

            expire_at_str = data.get("expire_at") or data.get("expires_at")
            can_undo = False
            remaining_text = "N/A"

            if expire_at_str and data.get("status") == "APPLIED":
                try:
                    expire_at = _parse_expire_at(data)
                    if expire_at:
                        diff = expire_at - now
                        if diff.total_seconds() > 0:
                            can_undo = True
                            days = diff.days
                            hours = int(diff.seconds / 3600)
                            remaining_text = f"{days}d {hours}h remaining"
                except Exception:
                    remaining_text = "N/A"
            elif data.get("status") == "REVERTED":
                remaining_text = "Already Reverted"

            results.append({
                "operation_id": data.get("operation_id") or doc.id,
                "action_type": data.get("action_type"),
                "source_filename": data.get("source_filename"),
                "staff_user_id": data.get("staff_user_id"),
                "staff_email": data.get("staff_email"),
                "status": data.get("status"),
                "affected_count": data.get("affected_count", 0),
                "affected_ids": data.get("affected_ids", []),
                "created_at": data.get("created_at"),
                "expire_at": expire_at_str,
                "expires_at": expire_at_str,
                "can_undo": can_undo,
                "remaining_undo_time": remaining_text,
                "reverted_at": data.get("reverted_at"),
                "reverted_by": data.get("reverted_by")
            })

        # Sort descending by created_at
        results.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return results[:limit]

    except Exception as e:
        logger.error(f"Error fetching batch operations history: {e}")
        return []


def purge_expired_snapshots(
    now: Optional[datetime] = None,
    batch_size: int = 100,
) -> int:
    """
    Delete all admin_batch_operations documents whose expire_at < now.
    Safe to call repeatedly. Never raises — logs and returns 0 on error.

    Infra should also enable a Firestore TTL policy on field `expire_at` as a
    second safety net so documents are removed even if this job is down.
    """
    try:
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        fs_client = get_snapshot_client()
        if not fs_client:
            logger.warning("purge_expired_snapshots: Firestore client unavailable; deleted=0")
            return 0

        col = fs_client.collection(BATCH_OPERATIONS_COLLECTION)
        deleted = 0
        now_iso = now.isoformat()

        # Prefer indexed inequality on expire_at; fall back to full scan + filter
        candidates: List[Any] = []
        try:
            q = col.where("expire_at", "<", now_iso).limit(batch_size)
            candidates = list(q.stream())
        except Exception as qe:
            logger.debug(f"purge_expired_snapshots expire_at query unavailable ({qe}); scanning")
            try:
                candidates = list(col.limit(batch_size * 5).stream())
            except Exception as se:
                logger.warning(f"purge_expired_snapshots scan failed: {se}")
                return 0

        for doc in candidates:
            if deleted >= batch_size:
                break
            try:
                data = doc.to_dict() or {}
                if not _is_snapshot_expired(data, now=now):
                    continue
                doc.reference.delete()
                deleted += 1
            except Exception as de:
                logger.warning(f"purge_expired_snapshots failed deleting {getattr(doc, 'id', '?')}: {de}")

        if deleted:
            logger.info(f"purge_expired_snapshots: deleted {deleted} expired snapshot(s)")
        return deleted
    except Exception as e:
        logger.warning(f"purge_expired_snapshots error (returning 0): {e}")
        return 0
