"""
Before/after change previews for admin PendingActions.
Diffs are built from real stored records at proposal time; confirm re-checks currency.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Fields we may show in a staff-facing diff table
_TRACKED_FIELDS = (
    "property_name",
    "project_name",
    "property_type",
    "status",
    "province",
    "district",
    "area",
    "price_display",
    "price_numeric_min",
    "price_numeric_max",
    "bedrooms_display",
    "bedrooms_min",
    "bathrooms",
    "area_display",
    "has_pool",
    "sea_view",
    "beach_access",
    "websites",
)

_INACTIVE_STATUS_MARKERS = (
    "sold",
    "inactive",
    "off market",
    "off-market",
    "withdrawn",
    "deleted",
    "archived",
    "not available",
    "unavailable",
)


def _norm(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, float):
        return f"{val:g}"
    if isinstance(val, (list, dict)):
        return str(val)
    return str(val).strip()


def _values_equal(a: Any, b: Any) -> bool:
    return _norm(a) == _norm(b)


def _coerce_price_number(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).lower().replace(",", "").replace("thb", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(million|mln|m\b)?", s)
    if not m:
        try:
            return float(re.sub(r"[^\d.]", "", s) or 0) or None
        except Exception:
            return None
    num = float(m.group(1))
    if m.group(2):
        num *= 1_000_000
    return num


def read_stored_property_fields(
    db: Optional[Session],
    property_id: str,
    field_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Read current values from the authoritative store (Postgres Property, then Firestore).
    Never invents values from model text.
    """
    keys = list(field_keys or _TRACKED_FIELDS)
    out: Dict[str, Any] = {"property_id": property_id}
    # 1) PostgreSQL
    if db is not None and property_id:
        try:
            from app.db.models import Property

            prop = db.query(Property).filter(Property.property_id == property_id).first()
            if prop:
                for k in keys:
                    if hasattr(prop, k):
                        out[k] = getattr(prop, k, None)
                if not out.get("property_name") and getattr(prop, "property_name", None):
                    out["property_name"] = prop.property_name
                return out
        except Exception as e:
            logger.debug(f"SQL property read skipped for {property_id}: {e}")

    # 2) Firestore projects catalog
    try:
        from app.services.firestore_service import get_firestore_client
        from app.services.admin_batch_service import _resolve_firestore_document

        fs = get_firestore_client(target="admin") or get_firestore_client()
        if fs:
            doc_id, doc = _resolve_firestore_document(
                fs.collection("projects"), {"property_id": property_id, "name": property_id}
            )
            if doc_id and doc:
                out["property_id"] = doc_id
                mapping = {
                    "property_name": doc.get("name") or doc.get("title") or doc.get("property_name"),
                    "status": doc.get("status") or doc.get("saleStatus"),
                    "price_numeric_min": doc.get("priceFrom") or doc.get("price_numeric_min") or doc.get("price"),
                    "price_display": doc.get("price_display")
                    or (f"{doc.get('priceFrom'):,}" if isinstance(doc.get("priceFrom"), (int, float)) else doc.get("price")),
                    "province": doc.get("province") or doc.get("location"),
                    "district": doc.get("district") or doc.get("area"),
                    "area": doc.get("area") or doc.get("location"),
                    "property_type": doc.get("property_type") or doc.get("type"),
                    "has_pool": doc.get("has_pool") or doc.get("pool"),
                }
                for k in keys:
                    if k in mapping and mapping[k] is not None:
                        out[k] = mapping[k]
                    elif k in doc:
                        out[k] = doc.get(k)
                return out
    except Exception as e:
        logger.debug(f"Firestore property read skipped for {property_id}: {e}")

    return out


def build_change_diff_rows(
    current: Dict[str, Any],
    proposed_fields: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return only fields that will change: {field, current, new}."""
    rows: List[Dict[str, Any]] = []
    for field, new_val in (proposed_fields or {}).items():
        if field in ("property_id", "images", "image_urls", "cover_image", "mode", "websites") and field == "images":
            continue
        if field in ("images", "image_urls", "cover_image", "mode"):
            # still allow websites/status/price etc.
            if field in ("images", "image_urls", "cover_image"):
                continue
        cur_val = current.get(field)
        # Map common aliases
        if cur_val is None and field == "price":
            cur_val = current.get("price_numeric_min") or current.get("price_display")
        if new_val is None:
            continue
        if _values_equal(cur_val, new_val):
            continue
        rows.append({
            "field": field,
            "current": cur_val if cur_val is not None else "—",
            "new": new_val,
        })
    return rows


def format_single_diff_markdown(
    property_id: str,
    rows: List[Dict[str, Any]],
    property_name: Optional[str] = None,
) -> str:
    title = property_name or property_id
    lines = [
        f"**Change preview for `{title}`** (`{property_id}`)",
        "",
        "| Field | Current | New |",
        "| :--- | :--- | :--- |",
    ]
    if not rows:
        lines.append("| _(no field changes)_ | — | — |")
    else:
        for r in rows:
            lines.append(
                f"| `{r['field']}` | {_norm(r['current']) or '—'} | {_norm(r['new']) or '—'} |"
            )
    return "\n".join(lines)


def format_batch_diff_markdown(
    operation_type: str,
    item_diffs: List[Dict[str, Any]],
    preview_limit: int = 5,
) -> str:
    """
    item_diffs: [{property_id, name, rows: [...]}]
    """
    n = len(item_diffs)
    lines = [
        f"**{n} properties will change** (`{operation_type}`).",
        "",
    ]
    shown = item_diffs[:preview_limit]
    for it in shown:
        pid = it.get("property_id")
        name = it.get("name") or pid
        rows = it.get("rows") or []
        if operation_type == "delete":
            lines.append(f"- `{pid}` **{name}**: delete (current status=`{_norm((rows[0]['current'] if rows else it.get('current_status')) or '—')}`)")
            continue
        if not rows:
            lines.append(f"- `{pid}` **{name}**: _(no field delta)_")
            continue
        # Compact Current -> New for first fields
        bits = []
        for r in rows[:4]:
            bits.append(f"`{r['field']}` {_norm(r['current'])} → {_norm(r['new'])}")
        lines.append(f"- `{pid}` **{name}**: " + "; ".join(bits))
    remaining = n - len(shown)
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return "\n".join(lines)


def assess_risky_changes(
    *,
    operation_type: str = "update",
    rows: Optional[List[Dict[str, Any]]] = None,
    item_diffs: Optional[List[Dict[str, Any]]] = None,
    delete_count: int = 0,
) -> List[str]:
    """Human-readable risk warnings (never silent on deletes / big price swings)."""
    warnings: List[str] = []
    op = (operation_type or "update").lower()
    if op == "delete" or delete_count > 0:
        n = delete_count or len(item_diffs or []) or 1
        warnings.append(
            f"⚠️ **Risky: DELETE** of {n} listing(s). This permanently removes live catalog data "
            f"(undo window still applies after confirm). Type the count to confirm (e.g. `yes {n}`)."
        )
        return warnings

    all_rows: List[Dict[str, Any]] = list(rows or [])
    inactive_hits = 0
    for it in item_diffs or []:
        all_rows.extend(it.get("rows") or [])
        for r in it.get("rows") or []:
            if r.get("field") == "status":
                new_s = _norm(r.get("new")).lower()
                if any(m in new_s for m in _INACTIVE_STATUS_MARKERS):
                    inactive_hits += 1

    for r in rows or []:
        if r.get("field") == "status":
            new_s = _norm(r.get("new")).lower()
            if any(m in new_s for m in _INACTIVE_STATUS_MARKERS):
                inactive_hits += 1

    for r in all_rows:
        field = (r.get("field") or "").lower()
        if "price" not in field:
            continue
        old_n = _coerce_price_number(r.get("current"))
        new_n = _coerce_price_number(r.get("new"))
        if old_n and new_n and old_n > 0:
            pct = abs(new_n - old_n) / old_n * 100.0
            if pct > 30:
                warnings.append(
                    f"⚠️ **Risky: price change over 30%** on `{r.get('field')}` "
                    f"({_norm(r.get('current'))} → {_norm(r.get('new'))}, ~{pct:.0f}%)."
                )
                break

    if inactive_hits >= 2:
        warnings.append(
            f"⚠️ **Risky: status set to sold/inactive** on {inactive_hits} items."
        )
    elif inactive_hits == 1 and not item_diffs:
        warnings.append("⚠️ **Risky: status set to sold/inactive**.")

    return warnings


def verify_diff_still_current(
    db: Optional[Session],
    change_diff: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Re-read stored records and ensure each 'current' in the staged diff still matches.
    Returns (ok, list of stale descriptions).
    """
    stale: List[str] = []
    op = (change_diff.get("operation_type") or "update").lower()

    # Single-property shape
    rows = change_diff.get("rows") or []
    property_id = change_diff.get("property_id")
    if property_id and rows:
        live = read_stored_property_fields(db, property_id, [r["field"] for r in rows])
        for r in rows:
            field = r["field"]
            expected = r.get("current")
            actual = live.get(field)
            if expected == "—" or expected is None:
                if actual is not None and _norm(actual):
                    stale.append(f"{property_id}.{field}: was empty, now `{_norm(actual)}`")
                continue
            if not _values_equal(expected, actual):
                stale.append(
                    f"{property_id}.{field}: expected `{_norm(expected)}`, now `{_norm(actual)}`"
                )

    # Batch shape
    for it in change_diff.get("items") or []:
        pid = it.get("property_id")
        if not pid:
            continue
        if op == "delete":
            live = read_stored_property_fields(db, pid, ["status", "property_name"])
            # If property vanished entirely that's ok for delete; if status snapshot drifted, flag
            snap_status = None
            for r in it.get("rows") or []:
                if r.get("field") == "status":
                    snap_status = r.get("current")
            if snap_status is not None and snap_status != "—" and live:
                if live.get("status") is not None and not _values_equal(snap_status, live.get("status")):
                    stale.append(f"{pid}.status: expected `{_norm(snap_status)}`, now `{_norm(live.get('status'))}`")
            continue
        item_rows = it.get("rows") or []
        if not item_rows:
            continue
        live = read_stored_property_fields(db, pid, [r["field"] for r in item_rows])
        for r in item_rows:
            field = r["field"]
            expected = r.get("current")
            actual = live.get(field)
            if expected == "—" or expected is None:
                continue
            if not _values_equal(expected, actual):
                stale.append(
                    f"{pid}.{field}: expected `{_norm(expected)}`, now `{_norm(actual)}`"
                )

    return (len(stale) == 0), stale


def build_single_listing_change_diff(
    db: Optional[Session],
    property_id: str,
    proposed_fields: Dict[str, Any],
    mode: str = "update",
) -> Dict[str, Any]:
    current = read_stored_property_fields(db, property_id, list(proposed_fields.keys()) + list(_TRACKED_FIELDS))
    rows = build_change_diff_rows(current, proposed_fields) if mode != "create" else [
        {"field": k, "current": "—", "new": v}
        for k, v in proposed_fields.items()
        if v is not None and k not in ("images", "image_urls", "cover_image", "mode")
    ]
    warnings = assess_risky_changes(operation_type=mode, rows=rows)
    md = format_single_diff_markdown(
        property_id, rows, property_name=current.get("property_name")
    )
    if warnings:
        md = md + "\n\n" + "\n".join(warnings)
    return {
        "property_id": property_id,
        "operation_type": mode,
        "rows": rows,
        "risk_warnings": warnings,
        "diff_markdown": md,
        "snapshot_current": {r["field"]: r["current"] for r in rows},
    }


def build_batch_change_diff(
    db: Optional[Session],
    operation_type: str,
    items: List[Dict[str, Any]],
    update_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    op = (operation_type or "update").lower()
    up = dict(update_fields or {})
    item_diffs: List[Dict[str, Any]] = []

    for it in items or []:
        pid = str(it.get("property_id") or "")
        name = it.get("name") or pid
        # Prefer freshly read store over any model-supplied current
        live = read_stored_property_fields(db, pid)
        if op == "delete":
            rows = [{
                "field": "status",
                "current": live.get("status") if live.get("status") is not None else (it.get("current") or {}).get("status", "—"),
                "new": "[DELETE]",
            }]
        else:
            proposed = dict(up)
            change_fields = (it.get("change") or {}).get("fields") or {}
            proposed.update(change_fields)
            # Merge item-level overrides except meta
            for k, v in it.items():
                if k in ("property_id", "name", "current", "change", "rows"):
                    continue
                if v is not None:
                    proposed.setdefault(k, v)
            current_map = dict(live)
            # Also fold resolver current if store miss
            if isinstance(it.get("current"), dict):
                for k, v in it["current"].items():
                    current_map.setdefault(k, v)
            rows = build_change_diff_rows(current_map, proposed)
        item_diffs.append({
            "property_id": pid,
            "name": name or live.get("property_name") or pid,
            "rows": rows,
            "current_status": live.get("status"),
        })

    delete_count = len(item_diffs) if op == "delete" else 0
    warnings = assess_risky_changes(
        operation_type=op, item_diffs=item_diffs, delete_count=delete_count
    )
    md = format_batch_diff_markdown(op, item_diffs)
    if warnings:
        md = md + "\n\n" + "\n".join(warnings)

    return {
        "operation_type": op,
        "items": item_diffs,
        "item_count": len(item_diffs),
        "delete_count": delete_count,
        "requires_explicit_delete_ack": op == "delete",
        "risk_warnings": warnings,
        "diff_markdown": md,
    }


def message_acks_delete_count(message: Optional[str], delete_count: int) -> bool:
    """True when staff typed the delete count in the confirm message."""
    if not message or delete_count is None:
        return False
    return bool(re.search(rf"\b{int(delete_count)}\b", message))
