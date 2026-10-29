"""
Before/after change previews for admin PendingActions.
Diffs are built from real stored records at proposal time; confirm re-checks currency.
Uses the canonical field map in admin_property_fields for PG + Firestore parity.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple


from app.services.admin_property_fields import (
    UNREADABLE,
    _TRACKED_FOR_DIFF,
    coerce_price_storage,
    expand_proposed_fields,
    format_price_for_display,
    format_price_thb,
    localize_name,
    normalize_compare_value,
    parse_price_amount,
    read_firestore_canonical,
    read_pg_canonical,
    resolve_alias,
    resolve_existing_property,
    values_equal,
)

logger = logging.getLogger(__name__)

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
    if val is None or val == UNREADABLE:
        return ""
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        # Never scientific notation
        try:
            if float(val) == int(float(val)):
                return f"{int(float(val)):,}"
            return f"{float(val):,.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(val)
    if isinstance(val, (list, dict)):
        return localize_name(val) or str(val)
    s = str(val).strip()
    if re.match(r"^-?\d+(\.\d+)?e[+-]?\d+$", s, re.I):
        try:
            return f"{int(float(s)):,}"
        except Exception:
            pass
    return s


def _values_equal(a: Any, b: Any) -> bool:
    return values_equal(a, b)


def _coerce_price_number(val: Any) -> Optional[float]:
    return parse_price_amount(val)


def read_stored_property_fields(
    db: Optional[Any],
    property_id: str,
    field_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Read current values under CANONICAL names via resolve_existing_property
    (Firestore). Same source at proposal and confirm.
    """
    raw_keys = list(field_keys or _TRACKED_FOR_DIFF)
    keys: List[str] = []
    for k in raw_keys:
        canon = resolve_alias(k) or (k if k in _TRACKED_FOR_DIFF or k == "location" else None)
        if canon and canon not in keys:
            keys.append(canon)
        if canon in ("price_numeric_min", "price_display", "price_numeric_max"):
            for extra in ("price_numeric_min", "price_display", "currency"):
                if extra not in keys:
                    keys.append(extra)
    if not keys:
        keys = list(_TRACKED_FOR_DIFF)

    out: Dict[str, Any] = {"property_id": property_id}
    readable: Dict[str, bool] = {}

    source, record = resolve_existing_property(db, property_id)
    if source == "firestore" and isinstance(record, dict):
        out["property_id"] = record.get("property_id") or record.get("_firestore_doc_id") or property_id
        out["_resolve_source"] = "firestore"
        for k in keys:
            if record.get(k) is not None and record.get(k) != "":
                out[k] = record[k]
                readable[k] = True
        if record.get("property_name"):
            out["property_name"] = localize_name(record["property_name"]) or record["property_name"]
            readable["property_name"] = True

    # Coerce stored prices to int for stable display / compare
    for pk in ("price_numeric_min", "price_numeric_max"):
        if out.get(pk) not in (None, "", UNREADABLE):
            coerced = coerce_price_storage(out[pk])
            if coerced is not None:
                out[pk] = coerced

    for k in keys:
        if k not in out or out.get(k) is None or out.get(k) == "":
            if k not in readable:
                out[k] = UNREADABLE
                readable[k] = False

    out["_readable"] = readable
    return out


def build_change_diff_rows(
    current: Dict[str, Any],
    proposed_fields: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return only fields that will change: {field, current, new, readable}."""
    rows: List[Dict[str, Any]] = []
    readable_map = current.get("_readable") or {}

    # Expand aliases in proposed
    expanded, _errs = expand_proposed_fields(
        proposed_fields,
        current_stored=current,
        allow_websites=("websites" in (proposed_fields or {})),
    )
    # If expand emptied because of errors, fall back to raw for known keys only
    work = expanded if expanded else {}
    if not work:
        for k, v in (proposed_fields or {}).items():
            canon = resolve_alias(k) or k
            if canon == "websites" and "websites" not in (proposed_fields or {}):
                continue
            work[canon] = v

    for field, new_val in work.items():
        if field in ("images", "image_urls", "cover_image", "mode", "property_id", "_readable"):
            continue
        if field == "websites" and "websites" not in (proposed_fields or {}):
            continue
        if new_val is None:
            continue

        cur_val = current.get(field)
        # Companion lookups
        if (cur_val is None or cur_val == UNREADABLE) and field == "price_numeric_min":
            alt = current.get("price_display")
            if alt and alt != UNREADABLE:
                cur_val = alt
        if (cur_val is None or cur_val == UNREADABLE) and field == "price_display":
            alt = current.get("price_numeric_min")
            if alt and alt != UNREADABLE:
                cur_val = alt

        is_readable = readable_map.get(field, cur_val is not None and cur_val != UNREADABLE)
        if cur_val == UNREADABLE:
            is_readable = False

        if is_readable and _values_equal(cur_val, new_val):
            continue

        if field in ("price_numeric_min", "price_numeric_max"):
            coerced_new = coerce_price_storage(new_val)
            if coerced_new is not None:
                new_val = coerced_new
            display_new = format_price_thb(new_val)
            if not is_readable:
                display_current = UNREADABLE
            elif cur_val not in (None, UNREADABLE):
                display_current = format_price_thb(cur_val)
            else:
                display_current = "—"
        else:
            display_new = new_val
            if not is_readable:
                display_current = UNREADABLE
            else:
                display_current = cur_val if cur_val is not None else "—"

        rows.append({
            "field": field,
            "current": display_current,
            "new": display_new,
            "readable": is_readable,
            "raw_new": new_val,
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
            cur = r["current"]
            if cur == UNREADABLE:
                cur_s = "(unreadable at proposal)"
            else:
                cur_s = _norm(cur) or "—"
            from app.services.admin_chat_quality import human_field_label

            lines.append(
                f"| {human_field_label(str(r['field']))} | {cur_s} | {_norm(r['new']) or '—'} |"
            )
    return "\n".join(lines)


def format_batch_diff_markdown(
    operation_type: str,
    item_diffs: List[Dict[str, Any]],
    preview_limit: int = 5,
) -> str:
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
            lines.append(
                f"- `{pid}` **{name}**: delete (current status=`{_norm((rows[0]['current'] if rows else it.get('current_status')) or '—')}`)"
            )
            continue
        if not rows:
            lines.append(f"- `{pid}` **{name}**: _(no field delta)_")
            continue
        bits = []
        for r in rows[:4]:
            cur = "(unreadable)" if r.get("current") == UNREADABLE else _norm(r["current"])
            from app.services.admin_chat_quality import human_field_label

            bits.append(f"{human_field_label(str(r['field']))} {cur} → {_norm(r['new'])}")
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
        if r.get("current") == UNREADABLE:
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
    db: Optional[Any],
    change_diff: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Re-read stored records. Skip fields that were unreadable at proposal time.
    Ignore formatting-only differences via normalize_compare_value.
    """
    stale: List[str] = []
    op = (change_diff.get("operation_type") or "update").lower()

    rows = change_diff.get("rows") or []
    property_id = change_diff.get("property_id")
    if property_id and rows:
        live = read_stored_property_fields(db, property_id, [r["field"] for r in rows])
        for r in rows:
            field = r["field"]
            expected = r.get("current")
            # Skip unreadable-at-proposal fields
            if expected == UNREADABLE or r.get("readable") is False:
                continue
            if expected == "—" or expected is None:
                # Legacy: do NOT treat as "was empty" — skip (unreadable)
                continue
            actual = live.get(field)
            if actual == UNREADABLE:
                # Cannot compare — skip rather than false-stale
                continue
            if not _values_equal(expected, actual):
                stale.append(
                    f"{property_id}.{field}: expected `{_norm(expected)}`, now `{_norm(actual)}`"
                )

    for it in change_diff.get("items") or []:
        pid = it.get("property_id")
        if not pid:
            continue
        if op == "delete":
            live = read_stored_property_fields(db, pid, ["status", "property_name"])
            snap_status = None
            for r in it.get("rows") or []:
                if r.get("field") == "status":
                    snap_status = r.get("current")
            if snap_status is not None and snap_status not in ("—", UNREADABLE) and live:
                if live.get("status") not in (None, UNREADABLE) and not _values_equal(
                    snap_status, live.get("status")
                ):
                    stale.append(
                        f"{pid}.status: expected `{_norm(snap_status)}`, now `{_norm(live.get('status'))}`"
                    )
            continue
        item_rows = it.get("rows") or []
        if not item_rows:
            continue
        live = read_stored_property_fields(db, pid, [r["field"] for r in item_rows])
        for r in item_rows:
            field = r["field"]
            expected = r.get("current")
            if expected in (UNREADABLE, "—", None) or r.get("readable") is False:
                continue
            actual = live.get(field)
            if actual == UNREADABLE:
                continue
            if not _values_equal(expected, actual):
                stale.append(
                    f"{pid}.{field}: expected `{_norm(expected)}`, now `{_norm(actual)}`"
                )

    return (len(stale) == 0), stale


def build_single_listing_change_diff(
    db: Optional[Any],
    property_id: str,
    proposed_fields: Dict[str, Any],
    mode: str = "update",
    user_message: Optional[str] = None,
) -> Dict[str, Any]:
    current = read_stored_property_fields(
        db, property_id, list((proposed_fields or {}).keys()) + list(_TRACKED_FOR_DIFF)
    )
    # If the listing exists in either store, always treat as update for diff purposes
    resolve_source = current.get("_resolve_source")
    if resolve_source == "firestore":
        mode = "update"

    allow_web = "websites" in (proposed_fields or {})
    expanded, expand_errors = expand_proposed_fields(
        proposed_fields,
        current_stored=current,
        user_message=user_message,
        allow_websites=allow_web,
    )
    use_fields = expanded if expanded else dict(proposed_fields or {})
    # Coerce prices in canonical payload
    for pk in ("price_numeric_min", "price_numeric_max"):
        if pk in use_fields and use_fields[pk] is not None:
            coerced = coerce_price_storage(use_fields[pk])
            if coerced is not None:
                use_fields[pk] = coerced
                if pk == "price_numeric_min":
                    use_fields.setdefault("price_display", format_price_thb(coerced))

    # Relative / absolute price: only min + display unless admin mentioned max/range top
    um = (user_message or "").lower()
    mentions_max = any(
        w in um for w in ("max", "maximum", "up to", "range", "price_to", "price to", "ceiling")
    )
    if not mentions_max and "price_numeric_max" in use_fields and "price_numeric_min" in use_fields:
        # Drop max unless it was the only price field proposed
        if "price_numeric_max" not in (proposed_fields or {}) and "price_to" not in {
            str(k).lower() for k in (proposed_fields or {})
        }:
            use_fields.pop("price_numeric_max", None)

    if mode != "create":
        rows = build_change_diff_rows(current, use_fields)
    else:
        rows = []
        for k, v in use_fields.items():
            if v is None:
                continue
            if k in ("images", "image_urls", "cover_image", "mode"):
                continue
            if k == "websites" and not allow_web:
                continue
            display_new = format_price_thb(v) if k.startswith("price_numeric") else v
            rows.append({"field": k, "current": "—", "new": display_new, "readable": True, "raw_new": v})

    # Restrict canonical_fields to rows that actually change (plus required companions)
    if mode != "create" and rows:
        changed_keys = {r["field"] for r in rows}
        use_fields = {k: v for k, v in use_fields.items() if k in changed_keys}
        # Keep price_display with price_numeric_min
        if "price_numeric_min" in use_fields and "price_display" not in use_fields:
            use_fields["price_display"] = format_price_thb(use_fields["price_numeric_min"])

    no_change = bool(mode != "create" and not rows)
    no_change_message = ""
    if no_change:
        cur_price = current.get("price_numeric_min") or current.get("price_display")
        if cur_price and cur_price != UNREADABLE:
            no_change_message = f"No change: price is already {format_price_thb(cur_price)}."
        else:
            no_change_message = "No change: proposed values already match the stored record."

    warnings = assess_risky_changes(operation_type=mode, rows=rows)
    name = current.get("property_name")
    if name == UNREADABLE:
        name = None
    if no_change:
        md = no_change_message
    else:
        md = format_single_diff_markdown(property_id, rows, property_name=name)
        if warnings:
            md = md + "\n\n" + "\n".join(warnings)
        # One-line summary of what changed for price-only updates
        price_rows = [r for r in rows if r.get("field") in ("price_numeric_min", "price_display")]
        if price_rows and len(rows) <= 3:
            pr = next((r for r in price_rows if r.get("field") == "price_numeric_min"), price_rows[0])
            md = md + f"\n\nChanging starting price: {_norm(pr.get('current'))} → {_norm(pr.get('new'))}."
    return {
        "property_id": current.get("property_id") or property_id,
        "operation_type": mode,
        "rows": rows,
        "risk_warnings": warnings,
        "diff_markdown": md,
        "snapshot_current": {r["field"]: r["current"] for r in rows},
        "canonical_fields": use_fields,
        "expand_errors": expand_errors,
        "no_change": no_change,
        "no_change_message": no_change_message,
        "resolve_source": resolve_source,
    }


def build_batch_change_diff(
    db: Optional[Any],
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
        live = read_stored_property_fields(db, pid)
        if op == "delete":
            cur_status = live.get("status")
            if cur_status == UNREADABLE:
                cur_status = (it.get("current") or {}).get("status", UNREADABLE)
            rows = [{
                "field": "status",
                "current": cur_status if cur_status is not None else "—",
                "new": "[DELETE]",
                "readable": cur_status not in (None, UNREADABLE),
            }]
        else:
            proposed = dict(up)
            change_fields = (it.get("change") or {}).get("fields") or {}
            proposed.update(change_fields)
            for k, v in it.items():
                if k in ("property_id", "name", "current", "change", "rows"):
                    continue
                if v is not None:
                    proposed.setdefault(k, v)
            current_map = dict(live)
            if isinstance(it.get("current"), dict):
                for k, v in it["current"].items():
                    if k not in current_map or current_map.get(k) in (None, UNREADABLE, ""):
                        current_map[k] = v
            rows = build_change_diff_rows(current_map, proposed)
        pname = name or live.get("property_name") or pid
        if pname == UNREADABLE:
            pname = pid
        item_diffs.append({
            "property_id": pid,
            "name": pname,
            "rows": rows,
            "current_status": live.get("status") if live.get("status") != UNREADABLE else None,
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
    if not message or delete_count is None:
        return False
    return bool(re.search(rf"\b{int(delete_count)}\b", message))


def substitute_real_pending_id(text: Optional[str], real_id: Optional[str]) -> str:
    """Strip model-invented action IDs and substitute the real DB pending action id."""
    body = text or ""
    if not real_id:
        return body
    # Replace common fake / placeholder patterns and any UUID-like or act_ tokens
    body = re.sub(
        r"(?i)(pending[_ ]?action(?:\s*id)?\s*[:=]?\s*|ID:\s*)[`'\"]?(?:act_[a-f0-9]+|[0-9a-f]{8}-[0-9a-f-]{27}|PENDING_ID|xxx+)[`'\"]?",
        rf"\1`{real_id}`",
        body,
    )
    # If no ID mentioned yet but we have a real one, append once
    if str(real_id) not in body:
        body = body.rstrip() + f"\n\nPending action ID: `{real_id}`."
    return body


def format_provenance_table(rows: List[Dict[str, Any]]) -> str:
    """Markdown table Field | Value | Source for an ingest draft. Does not write anything."""
    from app.services.admin_chat_quality import human_field_label

    lines = ["| Field | Value | Source |", "| --- | --- | --- |"]
    seen: Dict[str, int] = {}
    for row in rows or []:
        field = human_field_label(str(row.get("field") or "")).replace("|", "/")
        value = str(row.get("value") if row.get("value") not in (None, "") else "—").replace("|", "/")
        source = str(row.get("source") or "").replace("|", "/")
        if field in seen and value not in {"", "—"}:
            previous = lines[seen[field]]
            if "THB" in value or (previous.endswith("| — |") or "| — |" in previous):
                lines[seen[field]] = f"| {field} | {value} | {source} |"
            continue
        seen[field] = len(lines)
        lines.append(f"| {field} | {value} | {source} |")
    return "\n".join(lines)
