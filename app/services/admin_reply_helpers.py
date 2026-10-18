"""
Admin speed helpers (TASK 7): dossier template stability, LLM context trim,
and non-actionable follow-up suggestions.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Sequence


ADMIN_LLM_SLOW_MESSAGE = (
    "This is taking too long, please try a smaller request"
)
ADMIN_LLM_SLOW_MESSAGE_PL = (
    "To trwa zbyt długo — spróbuj mniejszego zapytania"
)

ADMIN_HISTORY_MESSAGE_LIMIT = 12


def choose_dossier_template_variant(
    property_ids: Optional[Sequence[str]] = None,
    explicit: Optional[Any] = None,
) -> int:
    """
    Pick template 1–7. Explicit admin request wins; otherwise stable hash of property id(s).
    Same property id(s) always yield the same template.
    """
    if explicit is not None and str(explicit).strip() != "":
        try:
            n = int(explicit)
            if 1 <= n <= 7:
                return n
        except (TypeError, ValueError):
            pass
    parts = [str(p).strip().lower() for p in (property_ids or []) if p and str(p).strip()]
    key = "|".join(parts) if parts else "default"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 7) + 1


def is_capabilities_question(message: Optional[str]) -> bool:
    raw = (message or "").strip().lower()
    if not raw:
        return False
    clean = re.sub(r"[^\w\s]", " ", raw)
    clean = re.sub(r"\s+", " ", clean).strip()
    phrases = (
        "what can you do",
        "what do you do",
        "what can you help",
        "how can you help",
        "your capabilities",
        "what are you able",
        "co potrafisz",
        "co umiesz",
        "czym mozesz",
        "czym możesz",
        "jak mozesz pomoc",
        "jak możesz pomóc",
    )
    return clean in phrases or any(p in clean for p in phrases)


def is_undo_request(message: Optional[str]) -> bool:
    raw = (message or "").strip().lower()
    return bool(
        re.search(r"\b(undo|rollback|revert|cofnij|cofnąć|cofniecie)\b", raw)
    )


def build_admin_structured_state(
    *,
    pending_action_id: Optional[str] = None,
    last_resolved_property_id: Optional[str] = None,
    last_report_file: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    open_pending: Optional[Sequence[Any]] = None,
    recent_finished: Optional[Sequence[Any]] = None,
) -> str:
    """
    Short structured state for the model.
    Prefer DB-backed open_pending / recent_finished so the model never claims
    an action is still pending when its DB status is executed/rejected.
    """
    lines = [
        "[SESSION STATE — use these ids; do not invent others]",
        "RULE: Never say an action is still pending unless listed under open_pending below.",
    ]
    open_list = list(open_pending or [])
    if open_list:
        lines.append("open_pending:")
        for pa in open_list[:5]:
            pid = getattr(pa, "id", None) or (pa.get("id") if isinstance(pa, dict) else None)
            tool = getattr(pa, "tool_name", None) or (pa.get("tool_name") if isinstance(pa, dict) else "?")
            lines.append(f"  - id={pid} tool={tool} status=PENDING")
        lines.append(f"pending_action_id: {getattr(open_list[0], 'id', None) or pending_action_id}")
    else:
        lines.append("open_pending: none")
        lines.append("pending_action_id: none")
        if pending_action_id:
            lines.append(
                f"(note: prefs had pending_action_id={pending_action_id} but DB has no open PENDING — ignore it)"
            )

    fin = list(recent_finished or [])
    if fin:
        lines.append("recent_finished_actions:")
        for pa in fin[:3]:
            pid = getattr(pa, "id", None) or (pa.get("id") if isinstance(pa, dict) else None)
            st = getattr(pa, "status", None) or (pa.get("status") if isinstance(pa, dict) else "?")
            tool = getattr(pa, "tool_name", None) or (pa.get("tool_name") if isinstance(pa, dict) else "?")
            one = ""
            er = getattr(pa, "execution_result", None) or (pa.get("execution_result") if isinstance(pa, dict) else None)
            if isinstance(er, dict):
                uf = er.get("updated_fields") or {}
                if uf:
                    one = " result=" + ",".join(f"{k}" for k in list(uf.keys())[:3])
            lines.append(f"  - id={pid} status={st} tool={tool}{one}")

    lines.append(f"last_resolved_property_id: {last_resolved_property_id or 'none'}")
    lines.append(f"last_report_file: {last_report_file or 'none'}")
    if extra:
        for k, v in extra.items():
            if v is not None and str(v).strip():
                lines.append(f"{k}: {v}")
    return "\n".join(lines)


def load_admin_action_state_from_db(
    db: Any,
    conversation_id: Optional[str],
) -> Dict[str, Any]:
    """Load open PENDING + last 3 finished actions for SESSION STATE."""
    out: Dict[str, Any] = {"open_pending": [], "recent_finished": []}
    if db is None or not conversation_id:
        return out
    try:
        from app.db.models import PendingAction
        from app.services.action_service import ACTION_STATUS_PENDING

        open_q = (
            db.query(PendingAction)
            .filter(
                PendingAction.conversation_id == str(conversation_id),
                PendingAction.status == ACTION_STATUS_PENDING,
            )
            .order_by(PendingAction.created_at.desc())
            .limit(5)
            .all()
        )
        out["open_pending"] = list(open_q or [])
        finished = (
            db.query(PendingAction)
            .filter(
                PendingAction.conversation_id == str(conversation_id),
                PendingAction.status != ACTION_STATUS_PENDING,
            )
            .order_by(PendingAction.created_at.desc())
            .limit(8)
            .all()
        )
        # Prefer most recently executed/rejected
        finished_sorted = sorted(
            finished or [],
            key=lambda a: getattr(a, "executed_at", None) or getattr(a, "created_at", None) or 0,
            reverse=True,
        )
        out["recent_finished"] = list(finished_sorted[:3])
    except Exception:
        pass
    return out


def trim_admin_history_for_llm(
    message_history: Optional[Sequence[Dict[str, Any]]],
    limit: int = ADMIN_HISTORY_MESSAGE_LIMIT,
) -> List[Dict[str, Any]]:
    """Keep only the last ~12 role/content turns for the LLM."""
    hist = list(message_history or [])
    if limit <= 0:
        return []
    return hist[-limit:]


def extract_session_state_from_conversation(conv: Any) -> Dict[str, Optional[str]]:
    prefs = dict(getattr(conv, "preferences", None) or {})
    return {
        "pending_action_id": prefs.get("last_pending_action_id"),
        "last_resolved_property_id": prefs.get("last_resolved_property_id"),
        "last_report_file": prefs.get("last_report_file"),
    }


def remember_admin_session_fact(
    conv: Any,
    *,
    pending_action_id: Optional[str] = None,
    property_id: Optional[str] = None,
    report_file: Optional[str] = None,
) -> None:
    """Best-effort in-memory preference update (caller may commit)."""
    if conv is None:
        return
    prefs = dict(getattr(conv, "preferences", None) or {})
    if pending_action_id:
        prefs["last_pending_action_id"] = str(pending_action_id)
    if property_id:
        prefs["last_resolved_property_id"] = str(property_id)
    if report_file:
        prefs["last_report_file"] = str(report_file)
    conv.preferences = prefs


def contextual_followup_suggestions(
    tool_calls: Optional[Sequence[Any]] = None,
    tool_results: Optional[Sequence[Any]] = None,
    *,
    language: str = "en",
    limit: int = 3,
) -> List[str]:
    """
    Up to 3 contextual suggestions from the type of tool result.
    Suggestions never trigger an action by themselves — display only.
    """
    names: List[str] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            n = tc.get("name") or tc.get("tool")
            if n:
                names.append(str(n).lower())
        elif isinstance(tc, str):
            names.append(tc.lower())

    # Infer from result shape when tool name missing
    for tr in tool_results or []:
        payload = tr.get("result") if isinstance(tr, dict) and "result" in tr else tr
        if not isinstance(payload, dict):
            if isinstance(payload, list) and payload:
                names.append("search_properties_full")
            continue
        if payload.get("leads") is not None or (
            isinstance(payload.get("results"), list)
            and payload.get("results")
            and any(
                isinstance(x, dict) and (x.get("email") or x.get("lead_id"))
                for x in payload["results"][:3]
            )
        ):
            names.append("list_buyer_leads")
        if payload.get("properties") is not None or payload.get("property_id"):
            names.append("search_properties_full")
        if payload.get("file_url") or payload.get("filename"):
            names.append("generate_report")
        if payload.get("diff_table") or payload.get("pending_action_id"):
            names.append("create_or_update_listing")
        if payload.get("is_mock") is True or payload.get("delivered") is False:
            names.append("send_email")

    pl = (language or "en").lower().startswith("pl")
    catalog = {
        "list_buyer_leads": (
            ["Export", "Show the next 5", "Email top lead"]
            if not pl
            else ["Eksport", "Pokaż kolejne 5", "Napisz do top leada"]
        ),
        "get_buyer_leads": (
            ["Export", "Show the next 5", "Email top lead"]
            if not pl
            else ["Eksport", "Pokaż kolejne 5", "Napisz do top leada"]
        ),
        "search_properties_full": (
            ["Update price on one listing", "Export these to Excel", "Generate a PDF report"]
            if not pl
            else ["Zmień cenę jednej oferty", "Eksportuj do Excela", "Wygeneruj raport PDF"]
        ),
        "create_or_update_listing": (
            ["Confirm", "Cancel", "Show before-after"]
            if not pl
            else ["Potwierdź", "Anuluj", "Pokaż przed/po"]
        ),
        "execute_batch_property_action": (
            ["Confirm the batch change", "Cancel the proposal", "Preview fewer items"]
            if not pl
            else ["Potwierdź zmianę wsadową", "Anuluj propozycję", "Pokaż mniej pozycji"]
        ),
        "generate_report": (
            ["Email this report to a client", "Export another format (Excel)", "Generate a leads report"]
            if not pl
            else ["Wyślij raport do klienta", "Eksport w Excelu", "Raport leadów"]
        ),
        "generate_client_offer_dossier": (
            ["Email it", "Regenerate with template 3", "Change branding"]
            if not pl
            else ["Wyślij", "Wygeneruj ponownie (szablon 3)", "Zmień branding"]
        ),
        "send_email": (
            ["Check Google Workspace connection", "Draft a calendar invite", "Try a different recipient"]
            if not pl
            else ["Sprawdź połączenie Google Workspace", "Utwórz wydarzenie w kalendarzu", "Inny odbiorca"]
        ),
        "get_property_knowledge": (
            ["Generate property PDF report", "Update price", "Show amenities from the record"]
            if not pl
            else ["Wygeneruj PDF oferty", "Zmień cenę", "Pokaż udogodnienia z rekordu"]
        ),
        "generate_property_brochure": (
            ["Email this PDF", "Create client offer dossier", "Update listing price"]
            if not pl
            else ["Wyślij ten PDF", "Utwórz dossier klienta", "Zmień cenę"]
        ),
        "inventory_stats": (
            ["Break down by Phuket vs Samui", "Show incomplete listings", "Export inventory summary"]
            if not pl
            else ["Podział Phuket vs Samui", "Pokaż niepełne oferty", "Eksport podsumowania"]
        ),
    }

    # Alias map
    aliases = {
        "search_properties": "search_properties_full",
        "list_leads": "list_buyer_leads",
        "buyer_leads": "list_buyer_leads",
        "ingest_property_from_drive": "create_or_update_listing",
        "propose_listing_update": "create_or_update_listing",
        "get_inventory_stats": "inventory_stats",
        "database_summary": "inventory_stats",
    }

    picked: List[str] = []
    seen = set()
    for n in names:
        key = aliases.get(n, n)
        for s in catalog.get(key, []):
            if s not in seen:
                seen.add(s)
                picked.append(s)
            if len(picked) >= limit:
                return picked[:limit]
    return picked[:limit]


def append_followup_suggestions(text: str, suggestions: Sequence[str], language: str = "en") -> str:
    if not suggestions:
        return text or ""
    pl = (language or "en").lower().startswith("pl")
    header = "**Sugestie (nie uruchamiają akcji):**" if pl else "**Suggestions (do not run an action by themselves):**"
    body = (text or "").rstrip()
    # Merge duplicated Suggestions blocks into one per reply
    if "Suggestions (do not run" in body or "Sugestie (nie uruchamiają" in body:
        # Strip previous suggestion sections
        body = re.sub(
            r"\n\n\*\*(?:Suggestions \(do not run an action by themselves\)|Sugestie \(nie uruchamiają akcji\)):\*\*(?:\n\d+\).*)+",
            "",
            body,
            flags=re.I,
        ).rstrip()
    lines = [header] + [f"{i}) {s}" for i, s in enumerate(suggestions[:3], start=1)]
    return body + "\n\n" + "\n".join(lines)


def is_read_only_admin_query(message: Optional[str]) -> bool:
    """True for inventory/lead/knowledge questions that should not surface pending-action cards."""
    raw = (message or "").strip().lower()
    if not raw:
        return True
    write_markers = (
        "confirm", "reject", "cancel", "approve", "zatwierdź", "zatwierdz", "anuluj",
        "update", "change", "delete", "create", "add listing", "set price", "modify",
        "propose", "apply", "undo", "rollback", "wykonaj", "usuń", "usun", "zmień", "zmien",
        "pending action", "akcja oczekująca",
    )
    if any(w in raw for w in write_markers):
        return False
    read_markers = (
        "tell me", "show me", "how many", "what is", "what's", "list ", "find ",
        "about ", "leads", "lead", "knowledge", "dossier info", "pokaż", "pokaz",
        "ile ", "opowiedz", "opowiedz", "powiedz", "szczegóły", "szczegoly",
    )
    return any(w in raw for w in read_markers) or len(raw.split()) <= 12


def message_references_pending_action(message: Optional[str]) -> bool:
    raw = (message or "").strip().lower()
    if not raw:
        return False
    return bool(
        re.search(
            r"\b(confirm|reject|cancel|approve|pending\s+action|zatwierd|anuluj|odrzuć|odrzuc)\b",
            raw,
        )
        or re.search(r"\bact_[a-f0-9]{8,}\b", raw)
        or re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            raw,
        )
    )


def should_surface_pending_action_ui(
    message: Optional[str],
    *,
    has_confirm_intent: bool = False,
    has_reject_intent: bool = False,
    action: Optional[str] = None,
    this_turn_proposed_write: bool = False,
) -> bool:
    """
    Confirmation card / waiting note only when the message is about that action
    or this turn proposes another write. Pure read-only questions never surface it.
    """
    if has_confirm_intent or has_reject_intent or action in ("confirm", "reject"):
        return True
    if this_turn_proposed_write:
        return True
    if message_references_pending_action(message):
        return True
    if is_read_only_admin_query(message):
        return False
    return False


def reply_ends_mid_word(text: Optional[str]) -> bool:
    """
    Detect likely mid-word truncation. Complete sentences ending in a number/name
    are NOT treated as truncated.
    """
    t = (text or "").rstrip()
    if not t:
        return False
    if re.search(r"[.!?…:\"')\]]\s*$", t) or t.endswith("?"):
        return False
    last = t.split()[-1] if t.split() else ""
    # Trailing hyphen / ellipsis fragment
    if last.endswith("-") or last.endswith("…") or last.endswith("..."):
        return True
    # Markdown bold/italic left open
    if last.count("*") % 2 == 1 or last.count("`") % 2 == 1:
        return True
    # Very short alphabetic fragment (1–2 letters) suggests cut mid-word
    if re.fullmatch(r"[A-Za-z]{1,2}", last):
        return True
    return False


def append_truncated_continuation_note(
    text: str,
    *,
    remaining: int = 0,
    language: str = "en",
) -> str:
    pl = (language or "en").lower().startswith("pl")
    body = (text or "").rstrip()
    # Trim trailing partial word
    body = re.sub(r"\s+\S{1,20}$", "", body) if reply_ends_mid_word(body) else body
    if remaining > 0:
        note = (
            f"\n\n…i jeszcze **{remaining}**. Napisz „pokaż więcej”, aby kontynuować."
            if pl
            else f"\n\n…and **{remaining}** more. Say 'show more' to continue."
        )
    else:
        note = (
            "\n\n…(odpowiedź ucięta — napisz „kontynuuj”, aby dokończyć)."
            if pl
            else "\n\n…(reply truncated — say 'continue' to finish)."
        )
    return body.rstrip() + note


def format_leads_date_window(
    *,
    days: int,
    window_start: Any,
    window_end: Any,
    calendar_week: bool = False,
    language: str = "en",
) -> str:
    pl = (language or "en").lower().startswith("pl")

    def _d(v: Any) -> str:
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
        return str(v)[:10]

    start_s, end_s = _d(window_start), _d(window_end)
    if calendar_week:
        return (
            f"ten tydzień kalendarzowy ({start_s} – {end_s})"
            if pl
            else f"this calendar week ({start_s} to {end_s})"
        )
    return (
        f"ostatnie {days} dni ({start_s} – {end_s})"
        if pl
        else f"last {days} days ({start_s} to {end_s})"
    )


def is_anthropic_timeout_error(exc: BaseException) -> bool:
    """True for request timeouts — do NOT retry these."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timedout" in name:
        return True
    if any(x in msg for x in ("timed out", "timeout", "read timed out", "deadline exceeded")):
        # Connection errors may also mention timeout in nested messages — prefer connection first
        if any(x in msg for x in ("connection reset", "connection error", "connection refused", "apiconnection")):
            return False
        return True
    return False


def is_anthropic_connection_error(exc: BaseException) -> bool:
    """True for connection / overload / 5xx — one retry allowed."""
    if is_anthropic_timeout_error(exc):
        return False
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(x in name for x in ("connection", "ratelimit", "internalserver", "apiconnection", "overloaded")):
        return True
    if any(
        x in msg
        for x in (
            "temporarily",
            "overloaded",
            "rate limit",
            "429",
            "500",
            "502",
            "503",
            "529",
            "connection reset",
            "connection error",
            "connection refused",
            "apiconnection",
        )
    ):
        return True
    return False


def is_transient_anthropic_error(exc: BaseException) -> bool:
    """Timeouts OR connection errors (used for friendly slow message)."""
    return is_anthropic_timeout_error(exc) or is_anthropic_connection_error(exc)
