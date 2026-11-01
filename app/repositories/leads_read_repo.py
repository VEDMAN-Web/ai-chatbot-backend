"""
Leads read repository (admin) — Firestore.

Public bot writes leads; admin only reads and updates status.
Tolerant of missing fields and old document shapes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.repositories import _fs_utils as fs
from app.repositories.store_mode import require_firestore_client

logger = logging.getLogger(__name__)

COLLECTION = "leads"


class LeadRecord:
    """Duck-typed lead compatible with serialize_lead_row."""

    def __init__(self, data: Dict[str, Any], doc_id: Optional[str] = None):
        d = dict(data or {})
        self.id = str(doc_id or d.get("id") or "")
        self.contact_name = d.get("contact_name") or d.get("name") or "Inquiring Visitor"
        self.contact_email = d.get("contact_email") or d.get("email") or "Not specified"
        self.contact_phone = d.get("contact_phone") or d.get("phone") or "Not specified"
        self.preferred_location = d.get("preferred_location") or d.get("location")
        self.preferred_type = d.get("preferred_type") or d.get("property_type")
        self.budget_min = d.get("budget_min")
        self.budget_max = d.get("budget_max") or d.get("budget")
        # Stored score when present; unscored docs (portal/imported/older/camelCase,
        # or a 0 placeholder) are scored from their own signals so min_score / score
        # ordering work for historical leads too.
        from app.services.lead_service import resolve_lead_score, stored_lead_score

        self.lead_score_computed = stored_lead_score(d) is None
        self.lead_score = float(resolve_lead_score(d))
        signals = d.get("buying_signals")
        if signals is None and d.get("summary"):
            signals = [f"Note: {d.get('summary')}"]
        if signals is None and d.get("message"):
            signals = [f"Note: {d.get('message')}"]
        self.buying_signals = signals if isinstance(signals, list) else ([] if signals is None else [signals])
        self.status = d.get("status") or "new"
        self.site = d.get("site") or d.get("brand") or d.get("site_origin")
        self.source = d.get("source") or d.get("sourceType") or d.get("source_type")
        self.property_id = d.get("property_id") or d.get("propertyId")
        self.property_name = d.get("property_name") or d.get("propertyName")
        self.session_id = d.get("session_id") or d.get("sessionId")
        self.language = d.get("language")
        self.summary = d.get("summary") or d.get("message")
        self.dedupe_key = d.get("dedupe_key") or d.get("dedupeKey")
        self.created_at = fs.parse_dt(d.get("created_at") or d.get("createdAt"))
        self.updated_at = fs.parse_dt(d.get("updated_at") or d.get("updatedAt"))
        self.conversation_id = d.get("conversation_id")


def _leads_client():
    # Leads live on the user/public project (shared contract with public bot)
    client = fs.user_fs_client() or fs.admin_fs_client()
    return require_firestore_client(client, store_name="leads")


def query_leads(
    db: Any = None,
    *,
    days: Optional[int] = None,
    window_start: Optional[datetime] = None,
    status: Optional[str] = None,
    min_score: float = 0.0,
    name_or_contact: Optional[str] = None,
    ids: Optional[List[str]] = None,
    site: Optional[str] = None,
    limit: int = 50,
    order: str = "newest",
) -> List[LeadRecord]:
    """
    Read leads from Firestore.
    """
    rows = _fs_query(
        days=days,
        window_start=window_start,
        status=status,
        min_score=min_score,
        name_or_contact=name_or_contact,
        ids=ids,
        site=site,
        limit=limit,
        order=order,
    )
    return rows


def count_leads(
    db: Any = None,
    *,
    window_start: Optional[datetime] = None,
    status: Optional[str] = None,
    min_score: float = 0.0,
    site: Optional[str] = None,
) -> int:
    rows = query_leads(
        db,
        window_start=window_start,
        status=status,
        min_score=min_score,
        site=site,
        limit=10_000,
    )
    return len(rows)


def most_recent_lead(db: Any = None) -> Optional[LeadRecord]:
    rows = query_leads(db, limit=1, order="newest")
    return rows[0] if rows else None


def update_status(db: Any, lead_id: str, status: str) -> Optional[LeadRecord]:
    """Admin status update only."""
    now_iso = datetime.now(timezone.utc).isoformat()
    updated: Optional[LeadRecord] = None

    client = _leads_client()
    if client is not None:
        data = fs.get_document(client, COLLECTION, lead_id)
        if data is not None:
            data["status"] = status
            data["updated_at"] = now_iso
            data["updatedAt"] = now_iso
            fs.set_document(client, COLLECTION, lead_id, data, merge=True)
            updated = LeadRecord(data, lead_id)

    return updated


def _fs_query(
    *,
    days: Optional[int] = None,
    window_start: Optional[datetime] = None,
    status: Optional[str] = None,
    min_score: float = 0.0,
    name_or_contact: Optional[str] = None,
    ids: Optional[List[str]] = None,
    site: Optional[str] = None,
    limit: int = 50,
    order: str = "newest",
) -> List[LeadRecord]:
    client = _leads_client()
    if client is None:
        return []
    docs = [fs.doc_to_dict(s) for s in fs.stream_collection(client, COLLECTION)]
    start = window_start
    if start is None and days and int(days) > 0:
        from datetime import timedelta
        start = datetime.now(timezone.utc) - timedelta(days=int(days))

    id_set = {str(i).strip().lower() for i in (ids or []) if i}
    needle = (name_or_contact or "").strip().lower()
    out: List[LeadRecord] = []
    for d in docs:
        rec = LeadRecord(d, d.get("id"))
        if id_set:
            name_l = (rec.contact_name or "").strip().lower()
            if str(rec.id).lower() not in id_set and name_l not in id_set:
                continue
        if status and (rec.status or "").lower() != status.lower():
            continue
        if min_score and float(rec.lead_score or 0) < float(min_score):
            continue
        if site:
            site_l = (rec.site or "").lower()
            if site.lower() not in site_l and site_l not in site.lower():
                continue
        if needle:
            blob = " ".join([
                str(rec.contact_name or ""),
                str(rec.contact_email or ""),
                str(rec.contact_phone or ""),
            ]).lower()
            if needle not in blob:
                continue
        if start and rec.created_at:
            ca = rec.created_at if rec.created_at.tzinfo else rec.created_at.replace(tzinfo=timezone.utc)
            st = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            if ca < st:
                continue
        out.append(rec)

    if order == "score":
        out.sort(key=lambda r: (float(r.lead_score or 0), r.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    else:
        out.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:limit]

