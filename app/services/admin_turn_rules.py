"""
Deterministic admin safety and write parsing.

These rules run before the model. They never invent a property, a price, or a token.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_REFUSALS = (
    (
        re.compile(r"(turn off|disable|hide|stop|wylacz|wyłącz|band karo).{0,40}(audit|audyt|logging|log)", re.I),
        "I cannot turn off or hide audit logging. Nothing was changed.",
    ),
    (
        re.compile(r"(audit|audyt).{0,40}(off|disable|hide|band)", re.I),
        "I cannot turn off or hide audit logging. Nothing was changed.",
    ),
    (
        re.compile(
            r"\b(delete|archive|remove|wipe|usun|usuń|hata)\b.{0,40}\b(all|every|entire|bulk|wszystkie|sab)\b.{0,40}\b(propert|listing|nieruchomo|properties)\b",
            re.I,
        ),
        "I will not delete or archive every property without a specific confirmed list. Nothing was changed.",
    ),
    (
        re.compile(r"without (asking|confirmation|confirm|puche|pooch|pytania)", re.I),
        "I will not skip confirmation. Nothing was changed.",
    ),
    (
        re.compile(r"\b(skip|auto-approve|auto approve|bypass)\b.{0,30}\bconfirm", re.I),
        "I will not skip or auto-approve confirmations. Nothing was changed.",
    ),
    (
        re.compile(r"\b(api key|password|system prompt|secret token|reveal the key)\b", re.I),
        "I cannot reveal keys, passwords, tokens, or the system prompt.",
    ),
    (
        re.compile(r"(export|download).{0,40}(personal data|customer data|all leads).{0,40}(public|internet)", re.I),
        "I will not export customer personal data to a public place. Nothing was sent.",
    ),
    (
        re.compile(r"\b(invent|make up|fabricate)\b.{0,30}\b(data|price|listing|property)\b", re.I),
        "I will not invent data. Nothing was changed.",
    ),
)

_LEGITIMATE_BULK = re.compile(
    r"\b(archive|update|increase|decrease)\b.{0,40}\b(\d+|the properties listed above)\b",
    re.I,
)


def safety_refusal_text(message: Optional[str]) -> Optional[str]:
    """Plain refusal, or None when the request may continue."""
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text:
        return None
    if _LEGITIMATE_BULK.search(text) and not re.search(r"\b(all|every|entire|bulk)\b", text, re.I):
        if not re.search(r"without (asking|confirmation|confirm)", text, re.I):
            return None
    for pattern, sentence in _REFUSALS:
        if pattern.search(text):
            return sentence
    return None


def _text(conv: Any, text: str, *, requires: bool = False, pending_id: Optional[str] = None, token: Optional[str] = None, tools: Optional[list] = None) -> Dict[str, Any]:
    return {
        "type": "text",
        "text": text,
        "session_id": getattr(conv, "session_id", None),
        "conversation_id": str(getattr(conv, "id", "") or ""),
        "requires_confirmation": requires,
        "pending_action_id": pending_id,
        "confirmation_token": token,
        "action_status": "awaiting_confirmation" if requires else None,
        "tools_used": tools or [],
        "download_link": None,
        "language": "en",
    }


def _which(rows: List[Any]) -> str:
    lines = ["Which property do you mean?"]
    for index, row in enumerate(rows[:5], start=1):
        name = getattr(row, "property_name", None) or getattr(row, "property_id", None)
        lines.append(f"{index}. {name}")
    return "\n".join(lines)


def resolve_write_target(db: Any, name: str) -> Tuple[Any, Optional[str]]:
    """Return (record, None) or (None, question). Uses the shared property resolver."""
    from app.db.models import Property
    from app.services.admin_property_fields import resolve_existing_property

    label = (name or "").strip(" ?.!")
    if len(label) < 2:
        return None, "Name the property you want to change. Nothing was changed."
    rows: List[Any] = []
    try:
        queried = db.query(Property).all()
        if isinstance(queried, list):
            rows = queried
    except Exception:
        rows = []
    named = [row for row in rows if isinstance(getattr(row, "property_name", None), str)]
    exact = [row for row in named if row.property_name.casefold() == label.casefold()]
    if len(exact) > 1:
        return None, _which(exact)
    if len(exact) == 1:
        return exact[0], None
    partial = [row for row in named if label.casefold() in row.property_name.casefold()]
    if len(partial) > 1:
        return None, _which(partial)
    if len(partial) == 1:
        return partial[0], None
    source, record = resolve_existing_property(db, label)
    if source and record is not None:
        return record, None
    return None, f"I could not find a property named '{label}'. Nothing was changed."


def _round_money(value: float) -> int:
    number = float(value)
    unit = 10000 if number >= 1_000_000 else 1000 if number >= 10_000 else 1
    return int(round(number / unit) * unit)


def _property_id(record: Any) -> str:
    if isinstance(record, dict):
        return str(record.get("property_id") or record.get("_firestore_doc_id") or "")
    return str(getattr(record, "property_id", "") or "")


def _property_name(record: Any) -> str:
    if isinstance(record, dict):
        return str(record.get("property_name") or record.get("name") or _property_id(record))
    return str(getattr(record, "property_name", None) or _property_id(record))


def _current_price(db: Any, record: Any) -> Optional[float]:
    if isinstance(record, dict):
        raw = record.get("price_numeric_min") or record.get("priceFrom")
    else:
        raw = getattr(record, "price_numeric_min", None)
    try:
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _propose_listing(db: Any, conv: Any, staff: Any, message: str, record: Any, fields: Dict[str, Any]) -> Dict[str, Any]:
    from app.services.admin_agent import _execute_admin_tool

    pid = _property_id(record)
    name = _property_name(record)
    args = {"property_id": pid, "fields_to_update": fields, "mode": "update", "_user_message": message}
    result, requires, pending_id = _execute_admin_tool(
        db, "create_or_update_listing", args, staff, str(getattr(conv, "id", "") or "")
    )
    if not isinstance(result, dict) or not requires:
        message_text = (result or {}).get("message") if isinstance(result, dict) else "I could not prepare that change."
        return _text(conv, message_text or "I could not prepare that change.")
    diff = result.get("diff_table") or result.get("message") or ""
    text = (
        f"Matched {name} ({pid}).\n\n{diff}\n\n"
        "Confirm to apply, or cancel. Nothing has been written."
    )
    return _text(
        conv,
        text,
        requires=True,
        pending_id=pending_id,
        token=result.get("confirmation_token"),
        tools=[{"name": "create_or_update_listing", "args": {"property_id": pid}}],
    )


def _google_connected(db: Any, staff: Any) -> bool:
    try:
        from app.db.models import User

        user = db.query(User).filter(User.email.ilike(getattr(staff, "email", ""))).first()
        prefs = getattr(user, "preferences", None) or {}
        workspace = prefs.get("google_workspace") or {}
        return bool(workspace.get("connected") and workspace.get("refresh_token"))
    except Exception:
        return False


def _google_missing(conv: Any) -> Dict[str, Any]:
    return _text(
        conv,
        "Google Workspace is not connected. Connect it at /api/v1/auth/google/login and then ask again. Nothing was sent.",
    )


def _listed_ids(conv: Any) -> List[str]:
    history = getattr(conv, "message_history", None) or []
    last = ""
    for turn in reversed(history):
        if isinstance(turn, dict) and turn.get("role") == "assistant":
            last = str(turn.get("content") or "")
            break
    return re.findall(r"\*\*[^*]+\*\* \(([^)]+)\)", last)


def _undo_last(db: Any, conv: Any, staff: Any) -> Dict[str, Any]:
    from app.db.models import PendingAction

    try:
        rows = (
            db.query(PendingAction)
            .filter(PendingAction.conversation_id == str(getattr(conv, "id", "") or ""))
            .filter(PendingAction.status == "EXECUTED")
            .all()
        )
    except Exception:
        rows = []
    if not isinstance(rows, list) or not rows:
        return _text(conv, "There is no confirmed change in this conversation to undo.")
    rows.sort(key=lambda row: str(getattr(row, "executed_at", None) or getattr(row, "created_at", "") or ""), reverse=True)
    action = rows[0]
    payload = action.action_payload or {}
    fields = payload.get("fields_to_update") or payload.get("fields") or {}
    from app.services.admin_chat_quality import humanise_field_list
    label = humanise_field_list(list(fields)[:4]) or action.tool_name
    result = action.execution_result or {}
    operation_id = result.get("operation_id") if isinstance(result, dict) else None
    if not operation_id:
        return _text(conv, f"The last confirmed change ({label}) has no undo snapshot. Nothing was restored.")
    from app.services.admin_agent import _execute_admin_tool

    undo_res, _, _ = _execute_admin_tool(
        db,
        "undo_batch_property_operation",
        {"operation_id": operation_id},
        staff,
        str(getattr(conv, "id", "") or ""),
    )
    if isinstance(undo_res, dict) and undo_res.get("success") is False:
        return _text(conv, f"I found the last change ({label}) but could not restore it: {undo_res.get('error')}")
    return _text(conv, f"Undone. The last change ({label}) was restored in both databases.")


def try_rule_based_write(db: Any, conv: Any, message: str, staff: Any, language: str = "en") -> Optional[Dict[str, Any]]:
    """Proposal or honest answer for common writes. None leaves the turn to the model."""
    from app.services.admin_reply_helpers import is_undo_request

    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text or safety_refusal_text(text):
        return None
    if is_undo_request(text):
        return _undo_last(db, conv, staff)

    email = re.search(r"send an email to\s+(\S+@\S+)\s+saying\s+(.+)$", text, re.I)
    if email:
        if not _google_connected(db, staff):
            return _google_missing(conv)
        from app.services.admin_agent import _execute_admin_tool

        args = {"to": email.group(1), "subject": "Message from Tajlandia admin", "body": email.group(2).strip()}
        result, requires, pending_id = _execute_admin_tool(db, "send_email", args, staff, str(conv.id))
        token = result.get("confirmation_token") if isinstance(result, dict) else None
        return _text(
            conv,
            f"Email to {args['to']} is ready. Confirm to send it. Nothing has been sent.\n\n{args['body']}",
            requires=bool(requires),
            pending_id=pending_id,
            token=token,
            tools=[{"name": "send_email", "args": args}],
        )

    if re.search(r"\bcreate a calendar event\b", text, re.I):
        if not _google_connected(db, staff):
            return _google_missing(conv)
        from datetime import datetime, timedelta, timezone
        from app.services.admin_agent import _execute_admin_tool

        start = datetime.now(timezone.utc) + timedelta(days=1)
        args = {
            "title": "Calendar event",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "description": text,
        }
        result, requires, pending_id = _execute_admin_tool(db, "create_calendar_event", args, staff, str(conv.id))
        token = result.get("confirmation_token") if isinstance(result, dict) else None
        return _text(
            conv,
            "Calendar event is ready. Confirm to add it. Nothing has been created.",
            requires=bool(requires),
            pending_id=pending_id,
            token=token,
            tools=[{"name": "create_calendar_event", "args": args}],
        )

    listed = re.search(r"archive the (\d+) properties listed above", text, re.I)
    if listed:
        ids = _listed_ids(conv)
        if not ids:
            return _text(conv, "I could not find a property list in this conversation to archive. Nothing was changed.")
        from app.services.action_service import create_pending_action
        from app.services.admin_change_diff import build_batch_change_diff

        items = []
        for pid in ids:
            target, problem = resolve_write_target(db, pid)
            if problem or target is None:
                continue
            items.append({
                "property_id": _property_id(target),
                "name": _property_name(target),
                "change": {"fields": {"status": "Archived", "is_public": False}},
            })
        if not items:
            return _text(conv, "I could not match the properties listed above. Nothing was changed.")
        change_diff = build_batch_change_diff(db, "update", items, {})
        payload = {
            "operation_type": "update",
            "items": items,
            "records": [
                {"property_id": item["property_id"], "name": item["name"], **item["change"]["fields"]}
                for item in items
            ],
            "update_fields": {"status": "Archived", "is_public": False},
            "notes": text,
            "change_diff": change_diff,
            "diff_markdown": change_diff.get("diff_markdown"),
            "risk_warnings": change_diff.get("risk_warnings") or [],
            "requires_explicit_delete_ack": False,
            "delete_count": 0,
        }
        action = create_pending_action(
            db=db,
            staff_user_id=getattr(staff, "email", None) or "admin",
            tool_name="execute_batch_property_action",
            action_payload=payload,
            conversation_id=str(getattr(conv, "id", "") or ""),
        )
        return _text(
            conv,
            f"I found {len(items)} properties from the list above. Confirm to archive them. Nothing has been written.\n\n{change_diff.get('diff_markdown') or ''}",
            requires=True,
            pending_id=str(action.id),
            token=action.confirmation_token,
            tools=[{"name": "execute_batch_property_action", "args": {"count": len(items)}}],
        )

    bulk = re.search(
        r"\b(increase|decrease|raise|lower)\s+all\s+(.+?)\s+by\s+(\d+(?:\.\d+)?)\s*%",
        text,
        re.I,
    )
    if bulk:
        from app.db.models import Property
        from app.services.action_service import create_pending_action
        from app.services.admin_change_diff import build_batch_change_diff

        direction, place_type, percent_text = bulk.group(1), bulk.group(2), bulk.group(3)
        percent = float(percent_text)
        factor = 1 + percent / 100.0 if direction.lower() in {"increase", "raise"} else 1 - percent / 100.0
        try:
            queried = db.query(Property).all()
            rows = queried if isinstance(queried, list) else []
        except Exception:
            rows = []
        words = [word for word in re.findall(r"[A-Za-z]+", place_type) if word.lower() not in {"the", "villas", "villa", "properties", "property"}]
        place = words[0] if words else ""
        matched = []
        for row in rows:
            province = str(getattr(row, "province", "") or "")
            kind = str(getattr(row, "property_type", "") or "")
            if place and place.lower() not in province.lower():
                continue
            if "villa" in place_type.lower() and "villa" not in kind.lower():
                continue
            if getattr(row, "price_numeric_min", None) is None:
                continue
            matched.append(row)
        if not matched:
            return _text(conv, "I could not find properties for that bulk price change. Nothing was changed.")
        items = []
        for row in matched:
            new_price = _round_money(float(row.price_numeric_min) * factor)
            items.append({
                "property_id": row.property_id,
                "name": row.property_name,
                "change": {"fields": {"price_numeric_min": new_price, "price_display": f"{new_price:,.0f} THB"}},
            })
        change_diff = build_batch_change_diff(db, "update", items, {})
        examples = ", ".join(item["name"] for item in items[:3])
        payload = {
            "operation_type": "update",
            "items": items,
            "records": [
                {"property_id": item["property_id"], "name": item["name"], **item["change"]["fields"]}
                for item in items
            ],
            "update_fields": {},
            "notes": text,
            "change_diff": change_diff,
            "diff_markdown": change_diff.get("diff_markdown"),
            "risk_warnings": change_diff.get("risk_warnings") or [],
            "requires_explicit_delete_ack": False,
            "delete_count": 0,
        }
        action = create_pending_action(
            db=db,
            staff_user_id=getattr(staff, "email", None) or "admin",
            tool_name="execute_batch_property_action",
            action_payload=payload,
            conversation_id=str(getattr(conv, "id", "") or ""),
        )
        return _text(
            conv,
            f"I found {len(items)} matching properties ({examples}). Each price changes by {percent:g}%. Confirm to apply. Nothing has been written.\n\n{change_diff.get('diff_markdown') or ''}",
            requires=True,
            pending_id=str(action.id),
            token=action.confirmation_token,
            tools=[{"name": "execute_batch_property_action", "args": {"count": len(items)}}],
        )

    from app.services.admin_property_fields import MONEY_PHRASE, describe_money, money_phrase, price_sanity_message

    bulk_amount = re.search(
        rf"\b(increase|decrease|raise|lower)\s+all\s+(.+?)\s+by\s+({MONEY_PHRASE})",
        text,
        re.I,
    )
    if bulk_amount and "%" not in bulk_amount.group(0):
        from app.db.models import Property
        from app.services.action_service import create_pending_action
        from app.services.admin_change_diff import build_batch_change_diff

        direction, place_type, raw_amount = bulk_amount.group(1), bulk_amount.group(2), bulk_amount.group(3)
        money = describe_money(raw_amount)
        if money is None:
            return _text(conv, "I could not read that price. Nothing was changed.")
        delta = float(money["thb"])
        try:
            queried = db.query(Property).all()
            rows = queried if isinstance(queried, list) else []
        except Exception:
            rows = []
        words = [word for word in re.findall(r"[A-Za-z]+", place_type) if word.lower() not in {"the", "villas", "villa", "properties", "property"}]
        place = words[0] if words else ""
        matched = []
        for row in rows:
            province = str(getattr(row, "province", "") or "")
            kind = str(getattr(row, "property_type", "") or "")
            if place and place.lower() not in province.lower():
                continue
            if "villa" in place_type.lower() and "villa" not in kind.lower():
                continue
            if getattr(row, "price_numeric_min", None) is None:
                continue
            matched.append(row)
        if not matched:
            return _text(conv, "I could not find properties for that bulk price change. Nothing was changed.")
        items = []
        for row in matched:
            current_price = float(row.price_numeric_min)
            new_price = _round_money(current_price + delta if direction.lower() in {"increase", "raise"} else current_price - delta)
            refusal = price_sanity_message(new_price, current_price)
            if refusal:
                return _text(conv, refusal)
            items.append({
                "property_id": row.property_id,
                "name": row.property_name,
                "change": {"fields": {"price_numeric_min": new_price, "price_display": f"{new_price:,.0f} THB"}},
            })
        change_diff = build_batch_change_diff(db, "update", items, {})
        examples = ", ".join(item["name"] for item in items[:3])
        payload = {
            "operation_type": "update",
            "items": items,
            "records": [
                {"property_id": item["property_id"], "name": item["name"], **item["change"]["fields"]}
                for item in items
            ],
            "update_fields": {},
            "notes": text,
            "change_diff": change_diff,
            "diff_markdown": change_diff.get("diff_markdown"),
            "risk_warnings": change_diff.get("risk_warnings") or [],
            "requires_explicit_delete_ack": False,
            "delete_count": 0,
        }
        action = create_pending_action(
            db=db,
            staff_user_id=getattr(staff, "email", None) or "admin",
            tool_name="execute_batch_property_action",
            action_payload=payload,
            conversation_id=str(getattr(conv, "id", "") or ""),
        )
        return _text(
            conv,
            f"I found {len(items)} matching properties ({examples}). Each price changes by {money_phrase(delta, money)}. Confirm to apply. Nothing has been written.\n\n{change_diff.get('diff_markdown') or ''}",
            requires=True,
            pending_id=str(action.id),
            token=action.confirmation_token,
            tools=[{"name": "execute_batch_property_action", "args": {"count": len(items)}}],
        )

    status_match = re.search(r"\bset\s+(.+?)\s+status\s+to\s+(.+)$", text, re.I) or re.search(
        r"\bmark\s+(.+?)\s+as\s+(sold out|available|reserved|archived|off-plan|ready to move in)$",
        text,
        re.I,
    )
    if status_match:
        from app.services.action_service import ALLOWED_STATUSES

        target, problem = resolve_write_target(db, status_match.group(1))
        if problem:
            return _text(conv, problem)
        raw_status = status_match.group(2).strip()
        status = ALLOWED_STATUSES.get(raw_status.lower(), raw_status)
        return _propose_listing(db, conv, staff, text, target, {"status": status})

    from app.services.admin_property_fields import MONEY_PHRASE, describe_money, money_phrase, price_sanity_message

    percent_match = re.search(
        r"\b(increase|decrease|raise|lower)\s+the price of\s+(.+?)\s+by\s+(\d+(?:\.\d+)?)\s*%",
        text,
        re.I,
    ) or re.search(
        r"\b(increase|decrease|raise|lower)\s+(.+?)\s+price\s+by\s+(\d+(?:\.\d+)?)\s*%",
        text,
        re.I,
    )
    amount_match = re.search(
        rf"\b(raise|lower|increase|decrease)\s+the price of\s+(.+?)\s+by\s+({MONEY_PHRASE})",
        text,
        re.I,
    ) or re.search(
        rf"\b(raise|lower|increase|decrease)\s+(.+?)\s+price\s+by\s+({MONEY_PHRASE})",
        text,
        re.I,
    )
    absolute = re.search(
        rf"\b(?:set|change|update)(?: the)? price of\s+(.+?)\s+to\s+({MONEY_PHRASE})",
        text,
        re.I,
    ) or re.search(
        rf"\b(?:set|change|update)\s+(.+?)\s+price\s+to\s+({MONEY_PHRASE})",
        text,
        re.I,
    )
    if percent_match or amount_match or absolute:
        if percent_match:
            direction, raw_name, raw_number = percent_match.group(1), percent_match.group(2), percent_match.group(3)
            money = None
        elif amount_match:
            direction, raw_name, raw_number = amount_match.group(1), amount_match.group(2), amount_match.group(3)
            money = describe_money(raw_number)
        else:
            direction, raw_name, raw_number = "set", absolute.group(1), absolute.group(2)
            money = describe_money(raw_number)
        target, problem = resolve_write_target(db, raw_name)
        if problem:
            return _text(conv, problem)
        current = _current_price(db, target)
        if absolute and direction == "set":
            if money is None:
                return _text(conv, "I could not read that price. Nothing was changed.")
            new_price = _round_money(float(money["thb"]))
            percent_note = ""
        elif current is None:
            return _text(conv, f"I could not find a stored price for {_property_name(target)}. Nothing was changed.")
        elif percent_match:
            percent = float(raw_number)
            factor = 1 + percent / 100.0 if direction.lower() in {"increase", "raise"} else 1 - percent / 100.0
            new_price = _round_money(current * factor)
            percent_note = f" ({percent:g}% from {current:,.0f} THB)"
            money = {"note": "", "short": ""}
        else:
            if money is None:
                return _text(conv, "I could not read that price. Nothing was changed.")
            delta = float(money["thb"])
            new_price = _round_money(current + delta if direction.lower() in {"raise", "increase"} else current - delta)
            percent_note = f" (from {current:,.0f} THB)"
        if not percent_match:
            refusal = price_sanity_message(new_price, current)
            if refusal:
                return _text(conv, refusal)
        shown = money_phrase(new_price, money if not percent_match else None)
        proposal = _propose_listing(
            db,
            conv,
            staff,
            text,
            target,
            {"price_numeric_min": new_price, "price_display": f"{new_price:,.0f} THB"},
        )
        if proposal.get("text"):
            proposal["text"] = f"New price {shown}{percent_note}.\n\n" + proposal["text"]
        return proposal

    delete_match = re.search(r"^(?:please\s+)?delete\s+(.+)$", text, re.I)
    if delete_match and not re.search(r"\b(all|every|entire)\b", delete_match.group(1), re.I):
        target, problem = resolve_write_target(db, delete_match.group(1))
        if problem:
            return _text(conv, problem)
        proposal = _propose_listing(
            db,
            conv,
            staff,
            text,
            target,
            {"status": "Archived", "is_public": False},
        )
        if proposal.get("text"):
            proposal["text"] = "This is a soft delete and still needs confirmation. The listing is not removed yet.\n\n" + proposal["text"]
        return proposal
    return None
