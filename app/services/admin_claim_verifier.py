"""
Admin reply claim verifier.

After the model writes an answer with numbers about listings/leads/counts,
compare those claims to this turn's tool results (source of truth). On failure:
regenerate once with "use only values from the tool results"; if still failing,
replace with a deterministic template built from tool data.

Never allows success claims unless tool result has success=true and is_mock=false.
Interventions are audit-logged (not shown to the user).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Numeric claims about inventory / money / rates / ids
_COUNT_CLAIM_RE = re.compile(
    r"(?P<num>\d[\d,]*)\s*(?:properties|listings|leads|results|items|records|"
    r"villas?|condos?|apartments?|projects?|oferty|nieruchomo[sś]ci|leadów)",
    re.I,
)
_FOUND_CLAIM_RE = re.compile(
    r"(?:found|total|count(?:ing)?|showing|there\s+are|we\s+have|mamy|znaleziono|"
    r"łącznie|lacznie)\s*(?:of\s*)?(?P<num>\d[\d,]*)",
    re.I,
)
_PCT_CLAIM_RE = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*%")
_PRICE_CLAIM_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:million|mln|m)\s*(?:thb|baht)?",
    re.I,
)
_LARGE_NUM_RE = re.compile(r"(?P<num>\d{1,3}(?:,\d{3}){1,}|\d{6,})")
_PROP_ID_RE = re.compile(r"\b(PROP-[A-Z0-9]+-\d+)\b", re.I)
_SLUG_ID_RE = re.compile(
    r"\b([a-z0-9]+(?:-[a-z0-9]+){1,6})\b",
    re.I,
)

_SUCCESS_CLAIM_RE = re.compile(
    r"(?:"
    r"successfully\s+(?:sent|created|updated|applied|executed|completed|deleted|delivered)|"
    r"(?:email|message|mail)\s+(?:has\s+been\s+|was\s+)?sent\b|"
    r"(?:calendar\s+)?event\s+(?:has\s+been\s+|was\s+)?(?:created|scheduled)\b|"
    r"(?:action|operation|update|listing)\s+(?:completed|executed|applied)\s+successfully|"
    r"(?:has\s+been|was)\s+(?:successfully\s+)?(?:sent|created|updated|applied|deleted)|"
    r"wysłan[ayo]|wyslano|pomyślnie\s+(?:wysł|zaktualiz|utworz)|"
    r"✅\s*\*\*[^*]*(?:success|sent|updated|created|executed)"
    r")",
    re.I,
)

# Words that must never be treated as invented slug ids when checking claims
_SLUG_STOP = frozenset({
    "property", "properties", "listing", "listings", "status", "available",
    "under-construction", "ready-to", "price-on", "million-thb", "action-required",
    "pending-action", "tool-result", "did-not", "not-found",
})


@dataclass
class ClaimVerifyResult:
    ok: bool
    unsupported_numbers: List[str] = field(default_factory=list)
    unsupported_ids: List[str] = field(default_factory=list)
    success_violations: List[str] = field(default_factory=list)
    reason: str = ""
    intervention: str = "none"  # none | regenerated | templated
    final_text: Optional[str] = None


def _norm_num_token(raw: str) -> Set[str]:
    """Expand a numeric token into comparable forms (raw, plain digits, million scale)."""
    s = str(raw).strip().lower().replace(",", "")
    out: Set[str] = {s}
    try:
        f = float(s)
        if f == int(f):
            out.add(str(int(f)))
        else:
            out.add(f"{f:g}")
        # million shorthand already parsed as 5 from "5 million" — also allow 5e6
        if f < 1000 and f == int(f):
            out.add(str(int(f) * 1_000_000))
            out.add(f"{int(f)}000000")
    except ValueError:
        pass
    return out


def _walk_numbers(obj: Any, into: Set[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if obj is None:
        return
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        into.update(_norm_num_token(str(obj)))
        return
    if isinstance(obj, float):
        into.update(_norm_num_token(f"{obj:g}"))
        return
    if isinstance(obj, str):
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", obj):
            into.update(_norm_num_token(m.group(0)))
        for m in _PROP_ID_RE.finditer(obj):
            into.add(m.group(1).upper())
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, (list, tuple)) and any(
                x in lk for x in ("item", "result", "propert", "lead", "record", "row")
            ):
                into.update(_norm_num_token(str(len(v))))
            if any(x in lk for x in ("count", "total", "item_count", "found", "size", "length")):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    into.update(_norm_num_token(str(int(v) if float(v) == int(v) else v)))
                elif isinstance(v, list):
                    into.update(_norm_num_token(str(len(v))))
            _walk_numbers(v, into, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        into.update(_norm_num_token(str(len(obj))))
        for item in obj[:50]:
            _walk_numbers(item, into, depth + 1)


def _normalize_tool_payloads(tool_results: Optional[Sequence[Any]]) -> List[Any]:
    """Accept Claude shape [{result: ...}] or bare result dicts/lists."""
    out: List[Any] = []
    for tr in tool_results or []:
        if isinstance(tr, dict) and "result" in tr:
            out.append(tr.get("result"))
        else:
            out.append(tr)
    return out


def collect_allowed_tokens(tool_results: Optional[Sequence[Any]]) -> Set[str]:
    allowed: Set[str] = set()
    for payload in _normalize_tool_payloads(tool_results):
        _walk_numbers(payload, allowed)
        # Also allow ids from common keys
        if isinstance(payload, dict):
            for key in ("property_id", "id", "doc_id", "pending_action_id", "operation_id"):
                val = payload.get(key)
                if val:
                    allowed.add(str(val).strip())
                    allowed.add(str(val).strip().upper())
            items = payload.get("items") or payload.get("properties") or payload.get("results")
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        for key in ("property_id", "id", "doc_id", "name"):
                            if it.get(key):
                                allowed.add(str(it[key]).strip())
                                allowed.add(str(it[key]).strip().upper())
        if isinstance(payload, list):
            for it in payload:
                if isinstance(it, dict):
                    for key in ("property_id", "id", "doc_id"):
                        if it.get(key):
                            allowed.add(str(it[key]).strip())
                            allowed.add(str(it[key]).strip().upper())
    return allowed


def tool_results_allow_success_claim(tool_results: Optional[Sequence[Any]]) -> bool:
    """
    True only when at least one actionable result has success=True and is_mock=False,
    and no result contradicts with explicit failure/mock when reply would claim success.
    """
    payloads = _normalize_tool_payloads(tool_results)
    if not payloads:
        return False
    saw_real_success = False
    saw_failed_or_mock = False
    for p in payloads:
        if not isinstance(p, dict):
            continue
        # Nested execution_result
        er = p.get("execution_result") if isinstance(p.get("execution_result"), dict) else p
        if not isinstance(er, dict):
            continue
        is_mock = er.get("is_mock")
        success = er.get("success")
        delivered = er.get("delivered")
        completed = er.get("completed")
        if is_mock is True or success is False or delivered is False or completed is False:
            saw_failed_or_mock = True
        if success is True and is_mock is False:
            saw_real_success = True
        # Update/listing style without explicit success key but status EXECUTED
        if str(p.get("status") or "").upper() == "EXECUTED" and is_mock is not True:
            if success is not False and is_mock is False:
                saw_real_success = True
    if saw_failed_or_mock and not saw_real_success:
        return False
    return saw_real_success


def extract_numeric_claims(text: Optional[str]) -> Tuple[List[str], List[str]]:
    """Return (number_tokens, id_tokens) claimed in the reply."""
    raw = text or ""
    numbers: List[str] = []
    ids: List[str] = []

    for rx in (_COUNT_CLAIM_RE, _FOUND_CLAIM_RE, _PCT_CLAIM_RE, _PRICE_CLAIM_RE, _LARGE_NUM_RE):
        for m in rx.finditer(raw):
            numbers.append(m.group("num"))

    for m in _PROP_ID_RE.finditer(raw):
        ids.append(m.group(1).upper())

    # Slug-like ids only when backtick-quoted or after "id" context — avoid English words
    for m in re.finditer(
        r"(?:`|/|id[:\s]+|property_id[:\s]+)([a-z0-9]+(?:-[a-z0-9]+){1,6})",
        raw,
        re.I,
    ):
        slug = m.group(1).lower()
        if slug in _SLUG_STOP:
            continue
        if any(c.isdigit() for c in slug) or (slug.count("-") >= 1 and len(slug) > 8):
            ids.append(slug)

    # Dedupe preserving order
    def _dedupe(seq: List[str]) -> List[str]:
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return _dedupe(numbers), _dedupe(ids)


def reply_claims_success(text: Optional[str]) -> bool:
    return bool(_SUCCESS_CLAIM_RE.search(text or ""))


def verify_admin_claims(
    reply_text: Optional[str],
    tool_results: Optional[Sequence[Any]],
) -> ClaimVerifyResult:
    """
    Compare numeric/id/success claims in reply_text against this turn's tool results.
    If there are no tool results, only success-claim checks that need tools are skipped
    (nothing to verify against → ok unless we detect success claim with empty tools).
    """
    text = reply_text or ""
    payloads = _normalize_tool_payloads(tool_results)
    allowed = collect_allowed_tokens(tool_results) if payloads else set()

    numbers, ids = extract_numeric_claims(text)
    unsupported_numbers: List[str] = []
    unsupported_ids: List[str] = []
    success_violations: List[str] = []

    if payloads:
        for num in numbers:
            forms = _norm_num_token(num)
            if not (forms & allowed):
                unsupported_numbers.append(num)
        for pid in ids:
            forms = {pid, pid.upper(), pid.lower()}
            if not (forms & {a.upper() if isinstance(a, str) else a for a in allowed} or forms & allowed):
                # also check lowercase set
                if pid.lower() not in {str(a).lower() for a in allowed}:
                    unsupported_ids.append(pid)

    if reply_claims_success(text):
        if not payloads:
            success_violations.append("success_claimed_without_tool_results")
        elif not tool_results_allow_success_claim(tool_results):
            success_violations.append("success_claimed_without_success_true_and_is_mock_false")

    ok = not unsupported_numbers and not unsupported_ids and not success_violations
    reasons = []
    if unsupported_numbers:
        reasons.append(f"unsupported_numbers={unsupported_numbers}")
    if unsupported_ids:
        reasons.append(f"unsupported_ids={unsupported_ids}")
    if success_violations:
        reasons.append(f"success_violations={success_violations}")
    return ClaimVerifyResult(
        ok=ok,
        unsupported_numbers=unsupported_numbers,
        unsupported_ids=unsupported_ids,
        success_violations=success_violations,
        reason="; ".join(reasons) if reasons else "ok",
        intervention="none",
        final_text=text,
    )


def build_deterministic_reply_from_tools(
    tool_results: Optional[Sequence[Any]],
    *,
    language: str = "en",
    prior_reason: str = "",
) -> str:
    """Build a grounded reply strictly from tool payloads (no model invention)."""
    payloads = _normalize_tool_payloads(tool_results)
    pl = (language or "en").lower().startswith("pl")
    lines: List[str] = []

    if not payloads:
        return (
            "Brak danych z narzędzi w tej turze — nie mogę potwierdzić liczb ani sukcesu operacji."
            if pl
            else "No tool data for this turn — I cannot confirm counts or that any action succeeded."
        )

    # Prefer honest incomplete / mock messaging first
    for p in payloads:
        if not isinstance(p, dict):
            continue
        er = p.get("execution_result") if isinstance(p.get("execution_result"), dict) else p
        if not isinstance(er, dict):
            continue
        if er.get("is_mock") is True or er.get("success") is False or er.get("delivered") is False:
            err = er.get("error") or er.get("message") or er.get("provider") or "incomplete"
            if pl:
                lines.append(
                    f"⚠️ **Akcja nie została dokończona.** "
                    f"(success≠true lub is_mock=true). Szczegóły: {err}"
                )
            else:
                lines.append(
                    f"⚠️ **The action did not complete.** "
                    f"(requires success=true and is_mock=false). Detail: {err}"
                )

    for p in payloads:
        if isinstance(p, list):
            n = len(p)
            if pl:
                lines.append(f"Znaleziono **{n}** pozycji w wyniku narzędzia:")
            else:
                lines.append(f"Found **{n}** item(s) in the tool result:")
            for it in p[:10]:
                if isinstance(it, dict):
                    pid = it.get("property_id") or it.get("id") or ""
                    name = it.get("property_name") or it.get("name") or it.get("title") or pid
                    price = it.get("price_display") or it.get("price") or ""
                    lines.append(f"- `{pid}` **{name}** {price}".strip())
            continue
        if not isinstance(p, dict):
            lines.append(str(p)[:400])
            continue
        if p.get("count") is not None or p.get("total") is not None or p.get("item_count") is not None:
            n = p.get("count", p.get("total", p.get("item_count")))
            label = "pozycji" if pl else "item(s)"
            lines.append(f"{'Łącznie' if pl else 'Total'}: **{n}** {label}.")
        items = p.get("items") or p.get("properties") or p.get("results") or p.get("leads")
        if isinstance(items, list):
            n = len(items)
            lines.append(f"{'Lista' if pl else 'List'} ({n}):")
            for it in items[:10]:
                if isinstance(it, dict):
                    pid = it.get("property_id") or it.get("id") or ""
                    name = it.get("property_name") or it.get("name") or it.get("title") or pid
                    lines.append(f"- `{pid}` **{name}**")
        if p.get("diff_table") or p.get("diff_markdown"):
            lines.append(str(p.get("diff_table") or p.get("diff_markdown")))
        if p.get("message") and not lines:
            lines.append(str(p.get("message")))
        if p.get("status") and p.get("pending_action_id"):
            lines.append(
                f"Pending action `{p.get('pending_action_id')}` status=`{p.get('status')}`."
            )
        # Real success
        er = p.get("execution_result") if isinstance(p.get("execution_result"), dict) else p
        if isinstance(er, dict) and er.get("success") is True and er.get("is_mock") is False:
            lines.append(
                "✅ Operacja zakończona powodzeniem (success=true, is_mock=false)."
                if pl
                else "✅ Operation completed (success=true, is_mock=false)."
            )

    if not lines:
        # Compact JSON excerpt
        try:
            excerpt = json.dumps(_sanitize_for_template(payloads[0]), ensure_ascii=False)[:1200]
        except Exception:
            excerpt = str(payloads[0])[:1200]
        lines.append(
            ("Dane narzędzia:\n" if pl else "Tool data:\n") + f"```\n{excerpt}\n```"
        )

    header = (
        "Oto zweryfikowane dane wyłącznie z wyników narzędzi tej tury:"
        if pl
        else "Here are verified figures taken only from this turn's tool results:"
    )
    return header + "\n\n" + "\n".join(lines)


def _sanitize_for_template(obj: Any) -> Any:
    if isinstance(obj, dict):
        skip = {"raw_firestore_data", "raw_data", "html_content", "base64", "pdf_bytes", "images"}
        return {k: _sanitize_for_template(v) for k, v in obj.items() if k not in skip}
    if isinstance(obj, list):
        return [_sanitize_for_template(x) for x in obj[:15]]
    return obj


def _log_verifier_intervention(
    db: Any,
    *,
    staff_user_id: Optional[str],
    conversation_id: Optional[str],
    reason: str,
    intervention: str,
    unsupported: Dict[str, Any],
) -> None:
    try:
        from app.services.audit_service import record_audit_log

        record_audit_log(
            db=db,
            staff_user_id=staff_user_id,
            tool_name="admin_claim_verifier",
            tool_arguments={"intervention": intervention},
            proposed_payload=unsupported,
            confirmed_by_staff=False,
            status="INTERVENTION",
            error_message=reason,
            conversation_id=str(conversation_id) if conversation_id else None,
            execution_result=intervention,
        )
    except Exception as e:
        logger.warning(f"Failed to audit claim-verifier intervention: {e}")


def verify_and_correct_admin_reply(
    reply_text: str,
    tool_results: Optional[Sequence[Any]],
    *,
    language: str = "en",
    db: Any = None,
    staff_user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    regenerate_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> ClaimVerifyResult:
    """
    Verify reply against tool results. On failure:
    1) optionally regenerate once via regenerate_fn(instruction)
    2) if still failing (or no regenerator), replace with deterministic template
    Logs every intervention to the audit log.
    """
    # No tool results and no numeric/success claims → untouched
    first = verify_admin_claims(reply_text, tool_results)
    if first.ok:
        first.final_text = reply_text
        first.intervention = "none"
        return first

    # Attempt one regeneration
    regenerated: Optional[str] = None
    if regenerate_fn is not None:
        instruction = (
            "GUARDRAIL: use only values from the tool results. "
            "Do not invent counts, prices, percentages, or ids. "
            "Do not claim an action succeeded unless the tool result has success=true and is_mock=false. "
            f"Previous issue: {first.reason}"
        )
        try:
            regenerated = regenerate_fn(instruction)
        except Exception as e:
            logger.warning(f"Claim verifier regenerate failed: {e}")
            regenerated = None

    if regenerated and str(regenerated).strip():
        second = verify_admin_claims(regenerated, tool_results)
        if second.ok:
            second.final_text = regenerated
            second.intervention = "regenerated"
            _log_verifier_intervention(
                db,
                staff_user_id=staff_user_id,
                conversation_id=conversation_id,
                reason=f"regenerated_after: {first.reason}",
                intervention="regenerated",
                unsupported={
                    "unsupported_numbers": first.unsupported_numbers,
                    "unsupported_ids": first.unsupported_ids,
                    "success_violations": first.success_violations,
                },
            )
            return second

    # Template fallback
    templated = build_deterministic_reply_from_tools(
        tool_results, language=language, prior_reason=first.reason
    )
    result = ClaimVerifyResult(
        ok=False,
        unsupported_numbers=first.unsupported_numbers,
        unsupported_ids=first.unsupported_ids,
        success_violations=first.success_violations,
        reason=first.reason,
        intervention="templated",
        final_text=templated,
    )
    _log_verifier_intervention(
        db,
        staff_user_id=staff_user_id,
        conversation_id=conversation_id,
        reason=f"templated_after: {first.reason}",
        intervention="templated",
        unsupported={
            "unsupported_numbers": first.unsupported_numbers,
            "unsupported_ids": first.unsupported_ids,
            "success_violations": first.success_violations,
            "regenerate_attempted": regenerated is not None,
        },
    )
    return result
