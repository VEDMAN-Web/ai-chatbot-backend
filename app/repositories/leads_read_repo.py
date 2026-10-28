"""
Leads read repository (admin) — Firestore first with optional SQL fallback.

Public bot writes leads; admin only reads and updates status.
Tolerant of missing fields and old document shapes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.repositories import _fs_utils as fs
from app.repositories.store_mode import (
    reads_firestore_first,
    reads_postgres,
    require_firestore_client,
    store_mode,
    writes_firestore,
    writes_postgres,
)

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
        self.lead_score = float(d.get("lead_score") or d.get("score") or 0.0)
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


def _mode() -> str:
    return store_mode("STORE_LEADS_READ")


def _leads_client():
    # Leads live on the user/public project (shared contract with public bot)
    client = fs.user_fs_client() or fs.admin_fs_client()
    if _mode() == "firestore":
        return require_firestore_client(client, store_name="leads")
    return client


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
    Read leads. Default STORE_LEADS_READ=both: Firestore first, SQL if empty.
    """
    mode = _mode()
    rows: List[LeadRecord] = []

    if reads_firestore_first(mode):
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
    if (not rows) and reads_postgres(mode) and db is not None:
        rows = _pg_query(
            db,
            days=days,
            window_start=window_start,
            status=status,
            min_score=min_score,
            name_or_contact=name_or_contact,
            ids=ids,
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
    """Admin status update only. Writes follow STORE_LEADS_READ mode."""
    mode = _mode()
    now_iso = datetime.now(timezone.utc).isoformat()
    updated: Optional[LeadRecord] = None

    if writes_firestore(mode):
        client = _leads_client()
        if client is not None:
            data = fs.get_document(client, COLLECTION, lead_id)
            if data is not None:
                data["status"] = status
                data["updated_at"] = now_iso
                data["updatedAt"] = now_iso
                fs.set_document(client, COLLECTION, lead_id, data, merge=True)
                updated = LeadRecord(data, lead_id)

    if writes_postgres(mode) and db is not None:
        try:
            from app.db.models import Lead
            import uuid as _uuid
            row = None
            try:
                row = db.query(Lead).filter(Lead.id == _uuid.UUID(str(lead_id))).first()
            except Exception:
                row = None
            if row is None:
                # Match by stringified id scan (sqlite tests)
                for candidate in db.query(Lead).all():
                    if str(candidate.id) == str(lead_id):
                        row = candidate
                        break
            if row is not None:
                row.status = status
                db.commit()
                updated = LeadRecord({
                    "id": str(row.id),
                    "contact_name": row.contact_name,
                    "contact_email": row.contact_email,
                    "contact_phone": row.contact_phone,
                    "preferred_location": row.preferred_location,
                    "preferred_type": row.preferred_type,
                    "budget_min": row.budget_min,
                    "budget_max": row.budget_max,
                    "lead_score": row.lead_score,
                    "buying_signals": row.buying_signals,
                    "status": row.status,
                    "created_at": row.created_at,
                }, str(row.id))
        except Exception as e:
            logger.warning("Lead status SQL update failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass

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


def _pg_query(
    db: Any,
    *,
    days: Optional[int] = None,
    window_start: Optional[datetime] = None,
    status: Optional[str] = None,
    min_score: float = 0.0,
    name_or_contact: Optional[str] = None,
    ids: Optional[List[str]] = None,
    limit: int = 50,
    order: str = "newest",
) -> List[LeadRecord]:
    from app.db.models import Lead

    try:
        query = db.query(Lead)
        if min_score:
            query = query.filter(Lead.lead_score >= min_score)
        if status:
            query = query.filter(Lead.status == status)
        start = window_start
        if start is None and days and int(days) > 0:
            from datetime import timedelta
            start = datetime.now(timezone.utc) - timedelta(days=int(days))
        if start is not None:
            try:
                query = query.filter(Lead.created_at >= start.replace(tzinfo=None))
            except Exception:
                query = query.filter(Lead.created_at >= start)
        if ids:
            clean = [str(i).strip() for i in ids if i]
            query = query.filter((Lead.contact_name.in_(clean)) | (Lead.id.in_(clean)))
        if name_or_contact:
            like = f"%{name_or_contact.strip()}%"
            query = query.filter(
                (Lead.contact_name.ilike(like))
                | (Lead.contact_email.ilike(like))
                | (Lead.contact_phone.ilike(like))
            )
        if order == "score":
            rows = query.order_by(Lead.lead_score.desc(), Lead.created_at.desc()).limit(limit).all()
        else:
            rows = query.order_by(Lead.created_at.desc(), Lead.lead_score.desc()).limit(limit).all()
        out: List[LeadRecord] = []
        for r in rows or []:
            out.append(
                LeadRecord({
                    "id": str(getattr(r, "id", "") or ""),
                    "contact_name": getattr(r, "contact_name", None),
                    "contact_email": getattr(r, "contact_email", None),
                    "contact_phone": getattr(r, "contact_phone", None),
                    "preferred_location": getattr(r, "preferred_location", None),
                    "preferred_type": getattr(r, "preferred_type", None),
                    "budget_min": getattr(r, "budget_min", None),
                    "budget_max": getattr(r, "budget_max", None),
                    "lead_score": getattr(r, "lead_score", 0),
                    "buying_signals": getattr(r, "buying_signals", None),
                    "status": getattr(r, "status", None),
                    "created_at": getattr(r, "created_at", None),
                    "site": getattr(r, "site", None),
                }, str(getattr(r, "id", "") or ""))
            )
        return out
    except Exception as e:
        logger.warning("Lead postgres query failed: %s", e)
        return []
