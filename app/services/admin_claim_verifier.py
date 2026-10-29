"""
Admin reply claim verifier.

HARD violations (wrong listing/lead/item COUNT; success claimed without
success=true & is_mock=false; wrong id/price of the property being changed)
→ regenerate once, then deterministic template.

SOFT mismatches (percentage, year, rounded number, or a number that only
appears inside a text field of the tool result) → keep the model reply;
audit-log only.

Never dump raw JSON or empty bullets in templates.
Never surface internal_notes / developer_contact / secrets in templates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

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
_YEAR_CLAIM_RE = re.compile(r"\b(?P<num>19\d{2}|20\d{2})\b")
_PRICE_CLAIM_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?:million|mln|m)\s*(?:thb|baht)?",
    re.I,
)
_LARGE_NUM_RE = re.compile(r"(?P<num>\d{1,3}(?:,\d{3}){1,}|\d{6,})")
_PROP_ID_RE = re.compile(r"\b(PROP-[A-Z0-9]+-\d+)\b", re.I)

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

_SLUG_STOP = frozenset({
    "property", "properties", "listing", "listings", "status", "available",
    "under-construction", "ready-to", "price-on", "million-thb", "action-required",
    "pending-action", "tool-result", "did-not", "not-found",
})

_REDACT_KEYS = frozenset({
    "internal_notes", "developer_contact", "raw_firestore_data", "raw_data",
    "html_content", "base64", "pdf_bytes", "images", "password", "token",
    "access_token", "refresh_token", "api_key", "secret",
})

_NS = "Not specified"
_NS_PL = "Brak danych"


@dataclass
class ClaimVerifyResult:
    ok: bool
    unsupported_numbers: List[str] = field(default_factory=list)
    unsupported_ids: List[str] = field(default_factory=list)
    success_violations: List[str] = field(default_factory=list)
    soft_mismatches: List[str] = field(default_factory=list)
    hard_mismatches: List[str] = field(default_factory=list)
    reason: str = ""
    intervention: str = "none"  # none | soft_logged | regenerated | templated
    final_text: Optional[str] = None


def _norm_num_token(raw: str) -> Set[str]:
    s = str(raw).strip().lower().replace(",", "")
    out: Set[str] = {s}
    try:
        f = float(s)
        if f == int(f):
            out.add(str(int(f)))
        else:
            out.add(f"{f:g}")
            out.add(f"{f:.1f}")
            out.add(f"{f:.0f}")
        if f < 1000 and f == int(f):
            out.add(str(int(f) * 1_000_000))
            out.add(f"{int(f)}000000")
    except ValueError:
        pass
    return out


def _add_derived_arithmetic(allowed: Set[str]) -> None:
    """Allow sums/averages/rounding of small sets of allowed numeric values."""
    nums: List[float] = []
    for a in list(allowed):
        try:
            if re.fullmatch(r"\d+(?:\.\d+)?", a.replace(",", "")):
                nums.append(float(a.replace(",", "")))
        except ValueError:
            continue
    # Cap to avoid combinatorial explosion
    nums = sorted(set(nums))[:40]
    for n in nums:
        allowed.update(_norm_num_token(f"{n:g}"))
        allowed.update(_norm_num_token(f"{round(n)}"))
        allowed.update(_norm_num_token(f"{round(n, 1)}"))
        # percentage-style (e.g. 0.097 -> 9.7)
        if 0 < n < 1:
            allowed.update(_norm_num_token(f"{n * 100:g}"))
            allowed.update(_norm_num_token(f"{round(n * 100, 1)}"))
        if 0 < n <= 100:
            allowed.update(_norm_num_token(f"{n:g}"))
            allowed.add(f"{n:g}%")
            allowed.add(f"{round(n, 1)}%")
    # pairwise sums / averages for small inventory math
    for i, a in enumerate(nums[:12]):
        for b in nums[i + 1:12]:
            s = a + b
            avg = (a + b) / 2.0
            allowed.update(_norm_num_token(f"{s:g}"))
            allowed.update(_norm_num_token(f"{round(s)}"))
            allowed.update(_norm_num_token(f"{avg:g}"))
            allowed.update(_norm_num_token(f"{round(avg, 1)}"))
    if nums:
        total = sum(nums[:20])
        allowed.update(_norm_num_token(f"{total:g}"))
        allowed.update(_norm_num_token(f"{round(total)}"))
        avg_all = total / max(1, min(len(nums), 20))
        allowed.update(_norm_num_token(f"{avg_all:g}"))
        allowed.update(_norm_num_token(f"{round(avg_all, 1)}"))


def _walk_all_text_and_numbers(obj: Any, into: Set[str], depth: int = 0) -> None:
    """Collect numbers/ids from ALL scalars including nested text fields."""
    if depth > 10:
        return
    if obj is None or isinstance(obj, bool):
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
        for m in _PCT_CLAIM_RE.finditer(obj):
            into.update(_norm_num_token(m.group("num")))
            into.add(f"{m.group('num')}%")
        for m in _PROP_ID_RE.finditer(obj):
            into.add(m.group(1).upper())
        # slug-ish tokens
        for m in re.finditer(r"\b([a-z0-9]+(?:-[a-z0-9]+){1,6})\b", obj, re.I):
            slug = m.group(1)
            if slug.lower() not in _SLUG_STOP and (
                any(c.isdigit() for c in slug) or len(slug) > 8
            ):
                into.add(slug)
                into.add(slug.lower())
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if _is_redacted_key(lk):
                # Still allow numbers INSIDE redacted text for soft-match purposes
                # (ROI in internal_notes) but do not expose the field in templates.
                if isinstance(v, str):
                    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", v):
                        into.update(_norm_num_token(m.group(0)))
                    for m in _PCT_CLAIM_RE.finditer(v):
                        into.update(_norm_num_token(m.group("num")))
                continue
            if isinstance(v, (list, tuple)) and any(
                x in lk for x in ("item", "result", "propert", "lead", "record", "row")
            ):
                into.update(_norm_num_token(str(len(v))))
            if any(x in lk for x in ("count", "total", "item_count", "found", "size", "length", "shown")):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    into.update(_norm_num_token(str(int(v) if float(v) == int(v) else v)))
                elif isinstance(v, list):
                    into.update(_norm_num_token(str(len(v))))
            _walk_all_text_and_numbers(v, into, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        into.update(_norm_num_token(str(len(obj))))
        for item in obj[:80]:
            _walk_all_text_and_numbers(item, into, depth + 1)


def _is_redacted_key(key: str) -> bool:
    lk = (key or "").lower()
    if lk in _REDACT_KEYS:
        return True
    return any(x in lk for x in ("secret", "token", "password", "api_key"))


def _normalize_tool_payloads(tool_results: Optional[Sequence[Any]]) -> List[Any]:
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
        _walk_all_text_and_numbers(payload, allowed)
        if isinstance(payload, dict):
            for key in ("property_id", "id", "doc_id", "pending_action_id", "operation_id", "lead_id"):
                val = payload.get(key)
                if val:
                    allowed.add(str(val).strip())
                    allowed.add(str(val).strip().upper())
            for list_key in ("items", "properties", "results", "leads"):
                items = payload.get(list_key)
                if isinstance(items, list):
                    allowed.update(_norm_num_token(str(len(items))))
                    for it in items:
                        if isinstance(it, dict):
                            for key in (
                                "property_id", "id", "doc_id", "name", "lead_id",
                                "contact_name", "title",
                            ):
                                if it.get(key):
                                    allowed.add(str(it[key]).strip())
                                    allowed.add(str(it[key]).strip().upper())
        if isinstance(payload, list):
            allowed.update(_norm_num_token(str(len(payload))))
            for it in payload:
                if isinstance(it, dict):
                    for key in ("property_id", "id", "doc_id", "lead_id", "name", "contact_name"):
                        if it.get(key):
                            allowed.add(str(it[key]).strip())
                            allowed.add(str(it[key]).strip().upper())
    _add_derived_arithmetic(allowed)
    return allowed


def extract_tool_counts(tool_results: Optional[Sequence[Any]]) -> Set[int]:
    counts: Set[int] = set()
    for p in _normalize_tool_payloads(tool_results):
        if isinstance(p, list):
            counts.add(len(p))
            continue
        if not isinstance(p, dict):
            continue
        for k in ("count", "total", "item_count", "shown", "leads_count"):
            v = p.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                counts.add(int(v))
        for lk in ("leads", "properties", "items", "results"):
            items = p.get(lk)
            if isinstance(items, list):
                counts.add(len(items))
    return counts


def extract_change_target_ids_and_prices(tool_results: Optional[Sequence[Any]]) -> Tuple[Set[str], Set[str]]:
    """Ids and prices of properties being changed (write / pending proposals)."""
    ids: Set[str] = set()
    prices: Set[str] = set()
    for p in _normalize_tool_payloads(tool_results):
        if not isinstance(p, dict):
            continue
        write_like = bool(
            p.get("pending_action_id")
            or p.get("diff_table")
            or p.get("change_diff")
            or p.get("requires_confirmation")
            or str(p.get("status") or "").upper() in ("PENDING", "PROPOSED")
        )
        if not write_like:
            continue
        for key in ("property_id", "target_property_id", "doc_id"):
            if p.get(key):
                ids.add(str(p[key]).strip().lower())
        cd = p.get("change_diff") if isinstance(p.get("change_diff"), dict) else {}
        for key in ("property_id", "id"):
            if cd.get(key):
                ids.add(str(cd[key]).strip().lower())
        for price_key in ("price", "new_price", "price_thb", "price_display", "after", "before"):
            for src in (p, cd, p.get("action_payload") if isinstance(p.get("action_payload"), dict) else {}):
                if not isinstance(src, dict):
                    continue
                val = src.get(price_key)
                if val is not None and str(val).strip():
                    prices.update(_norm_num_token(str(val)))
                    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(val)):
                        prices.update(_norm_num_token(m.group(0)))
    return ids, prices


def tool_results_allow_success_claim(tool_results: Optional[Sequence[Any]]) -> bool:
    payloads = _normalize_tool_payloads(tool_results)
    if not payloads:
        return False
    saw_real_success = False
    saw_failed_or_mock = False
    for p in payloads:
        if not isinstance(p, dict):
            continue
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
        if str(p.get("status") or "").upper() == "EXECUTED" and is_mock is not True:
            if success is not False and is_mock is False:
                saw_real_success = True
    if saw_failed_or_mock and not saw_real_success:
        return False
    return saw_real_success


def extract_numeric_claims(text: Optional[str]) -> Tuple[List[str], List[str]]:
    raw = text or ""
    numbers: List[str] = []
    ids: List[str] = []

    for rx in (_COUNT_CLAIM_RE, _FOUND_CLAIM_RE, _PCT_CLAIM_RE, _PRICE_CLAIM_RE, _LARGE_NUM_RE):
        for m in rx.finditer(raw):
            numbers.append(m.group("num"))

    for m in _PROP_ID_RE.finditer(raw):
        ids.append(m.group(1).upper())

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


def _is_soft_number(num: str, reply_text: str) -> bool:
    """Percentages, years, and rounded-looking decimals are soft unless they are count claims."""
    # Count claim context → never soft
    for m in _COUNT_CLAIM_RE.finditer(reply_text or ""):
        if m.group("num") == num:
            return False
    for m in _FOUND_CLAIM_RE.finditer(reply_text or ""):
        if m.group("num") == num:
            return False
    if _PCT_CLAIM_RE.search(f"{num}%") or re.search(
        rf"(?<!\d){re.escape(num)}\s*%", reply_text or ""
    ):
        return True
    if re.fullmatch(r"19\d{2}|20\d{2}", num.replace(",", "")):
        return True
    # one-decimal rounding style (9.7, 6.6)
    if re.fullmatch(r"\d+\.\d", num.replace(",", "")):
        return True
    return False


_RETRACT_PHRASE_RE = re.compile(
    r"(?:not\s+verified|unverified|demo\s+(?:data|lead)|fake\s+lead|unreal|"
    r"not\s+a\s+real\s+lead|probably\s+demo|likely\s+demo|sample\s+data|"
    r"was\s+(?:probably\s+)?demo|is\s+(?:probably\s+)?demo)",
    re.I,
)
_DEMO_FROM_EMAIL_RE = re.compile(
    r"(?:demo|fake|unreal|not\s+real|not\s+verified).{0,60}example\.com|"
    r"example\.com.{0,60}(?:demo|fake|unreal|not\s+real)",
    re.I | re.S,
)
_PLACEHOLDER_EMAIL_OK_RE = re.compile(
    r"email\s+domain\s+looks\s+like\s+a\s+placeholder|verify\s+before\s+contacting",
    re.I,
)


def extract_prior_verified_lead_names(
    conversation_history: Optional[Sequence[Any]] = None,
    prior_tool_results: Optional[Sequence[Any]] = None,
) -> Set[str]:
    """Names previously returned as leads in this conversation (case-insensitive)."""
    names: Set[str] = set()

    def _add_from_payload(p: Any) -> None:
        if isinstance(p, dict):
            leads = p.get("leads")
            if isinstance(leads, list):
                for ld in leads:
                    if isinstance(ld, dict):
                        n = (ld.get("contact_name") or ld.get("name") or "").strip()
                        if n:
                            names.add(n.lower())
            for key in ("contact_name", "name"):
                n = (p.get(key) or "").strip()
                if n and ("@" not in n):
                    names.add(n.lower())
        elif isinstance(p, list):
            for it in p:
                _add_from_payload(it)

    for p in _normalize_tool_payloads(prior_tool_results):
        _add_from_payload(p)

    for turn in conversation_history or []:
        if not isinstance(turn, dict):
            continue
        # Prefer structured tool_results stored on the turn
        tr = turn.get("tool_results")
        if tr:
            for p in _normalize_tool_payloads(tr):
                _add_from_payload(p)
        content = turn.get("content") or ""
        if turn.get("role") == "assistant" and content:
            for m in re.finditer(
                r"\*\*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\*\*",
                content,
            ):
                names.add(m.group(1).lower())
    return names


def current_turn_directly_covers_lead(
    tool_results: Optional[Sequence[Any]],
    lead_name: str,
) -> bool:
    """True only if this turn's tools returned that lead by id/name/email (not merely 0 from a filter)."""
    target = (lead_name or "").strip().lower()
    if not target:
        return False
    for p in _normalize_tool_payloads(tool_results):
        if not isinstance(p, dict):
            continue
        # Empty filtered result never "covers" a prior lead
        leads = p.get("leads")
        if isinstance(leads, list):
            if int(p.get("total") or len(leads) or 0) == 0:
                continue
            for ld in leads:
                if not isinstance(ld, dict):
                    continue
                blob = " ".join(
                    str(ld.get(k) or "").lower()
                    for k in ("contact_name", "name", "contact_email", "email", "lead_id", "id")
                )
                if target in blob:
                    return True
        # Single-lead lookup payload
        for k in ("contact_name", "name", "contact_email", "email", "lead_id"):
            if target in str(p.get(k) or "").lower():
                return True
    return False


def detect_unfair_lead_retraction(
    reply_text: Optional[str],
    tool_results: Optional[Sequence[Any]],
    *,
    prior_verified_leads: Optional[Set[str]] = None,
) -> List[str]:
    """
    Hard violations: retracting a previously verified lead using only a narrower
    empty/partial search, or calling real-looking data 'demo' solely from example.com.
    """
    text = reply_text or ""
    text_l = text.lower()
    violations: List[str] = []
    prior = {n.lower().strip() for n in (prior_verified_leads or set()) if n}

    if _RETRACT_PHRASE_RE.search(text):
        for name in prior:
            if not name or name not in text_l:
                continue
            if not current_turn_directly_covers_lead(tool_results, name):
                # Preserve original casing for the message when possible
                display = name.title()
                violations.append(f"unfair_retract:{display}")

    if _DEMO_FROM_EMAIL_RE.search(text) and not _PLACEHOLDER_EMAIL_OK_RE.search(text):
        # Calling example.com data "demo/fake" without the placeholder phrasing
        violations.append("demo_label_from_example_email")

    return violations


def verify_admin_claims(
    reply_text: Optional[str],
    tool_results: Optional[Sequence[Any]],
    *,
    prior_verified_leads: Optional[Set[str]] = None,
) -> ClaimVerifyResult:
    text = reply_text or ""
    payloads = _normalize_tool_payloads(tool_results)
    allowed = collect_allowed_tokens(tool_results) if payloads else set()
    tool_counts = extract_tool_counts(tool_results) if payloads else set()
    change_ids, change_prices = extract_change_target_ids_and_prices(tool_results)

    numbers, ids = extract_numeric_claims(text)
    unsupported_numbers: List[str] = []
    unsupported_ids: List[str] = []
    success_violations: List[str] = []
    soft_mismatches: List[str] = []
    hard_mismatches: List[str] = []

    if payloads:
        for num in numbers:
            forms = _norm_num_token(num)
            if forms & allowed:
                continue

            is_count_ctx = any(
                m.group("num") == num
                for m in list(_COUNT_CLAIM_RE.finditer(text)) + list(_FOUND_CLAIM_RE.finditer(text))
            )
            is_price_ctx = any(m.group("num") == num for m in _PRICE_CLAIM_RE.finditer(text))

            if is_count_ctx:
                try:
                    claimed = int(str(num).replace(",", ""))
                except ValueError:
                    claimed = None
                if claimed is not None and (not tool_counts or claimed not in tool_counts):
                    unsupported_numbers.append(num)
                    hard_mismatches.append(f"wrong_count:{num}")
                continue

            if is_price_ctx and change_prices and not (forms & change_prices):
                unsupported_numbers.append(num)
                hard_mismatches.append(f"wrong_change_price:{num}")
                continue

            if _is_soft_number(num, text):
                soft_mismatches.append(num)
            else:
                # Unknown non-count number → soft (do not throw away a good answer)
                soft_mismatches.append(num)

        for pid in ids:
            forms = {pid, pid.upper(), pid.lower()}
            allowed_l = {str(a).lower() for a in allowed}
            if pid.lower() in allowed_l or forms & allowed:
                continue
            if change_ids and pid.lower() not in change_ids:
                unsupported_ids.append(pid)
                hard_mismatches.append(f"wrong_change_id:{pid}")
            else:
                soft_mismatches.append(pid)

    if reply_claims_success(text):
        if not payloads:
            success_violations.append("success_claimed_without_tool_results")
            hard_mismatches.append("success_without_tools")
        elif not tool_results_allow_success_claim(tool_results):
            success_violations.append("success_claimed_without_success_true_and_is_mock_false")
            hard_mismatches.append("success_on_mock_or_failure")

    for v in detect_unfair_lead_retraction(
        text, tool_results, prior_verified_leads=prior_verified_leads
    ):
        hard_mismatches.append(v)

    has_hard = bool(success_violations) or any(
        h.startswith("wrong_count")
        or h.startswith("wrong_change")
        or h.startswith("success_")
        or h.startswith("unfair_retract")
        or h.startswith("demo_label")
        for h in hard_mismatches
    )
    ok = not has_hard

    reasons = []
    if hard_mismatches:
        reasons.append(f"hard={hard_mismatches}")
    if soft_mismatches:
        reasons.append(f"soft={soft_mismatches}")
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
        soft_mismatches=soft_mismatches,
        hard_mismatches=hard_mismatches,
        reason="; ".join(reasons) if reasons else "ok",
        intervention="none",
        final_text=text,
    )


# ---------------------------------------------------------------------------
# Deterministic templates (TASK B)
# ---------------------------------------------------------------------------

def redact_fields_for_template(obj: Any, depth: int = 0) -> Any:
    """Strip secrets / internal_notes / developer_contact from template payloads."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_redacted_key(str(k)):
                continue
            out[k] = redact_fields_for_template(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [redact_fields_for_template(x, depth + 1) for x in obj[:30]]
    return obj


def _ns(language: str) -> str:
    return _NS_PL if (language or "en").lower().startswith("pl") else _NS


def _fmt_val(v: Any, language: str = "en") -> str:
    if v is None or (isinstance(v, str) and not str(v).strip()):
        return _ns(language)
    s = str(v).strip()
    return s if s else _ns(language)


def _first_nonempty_scalars(item: Dict[str, Any], limit: int = 3) -> List[str]:
    parts = []
    for k, v in item.items():
        if _is_redacted_key(str(k)):
            continue
        if isinstance(v, (dict, list)):
            continue
        if v is None or (isinstance(v, str) and not str(v).strip()):
            continue
        parts.append(f"{str(k).replace('_', ' ').capitalize()}: {v}")
        if len(parts) >= limit:
            break
    return parts


def _looks_like_lead(item: Dict[str, Any]) -> bool:
    return bool(
        item.get("lead_id")
        or item.get("contact_email")
        or item.get("contact_name")
        or item.get("lead_score") is not None
    )


def _looks_like_property(item: Dict[str, Any]) -> bool:
    return bool(
        item.get("property_id")
        or item.get("bedrooms") is not None
        or item.get("price_display")
        or item.get("amenities")
    )


def format_lead_line(item: Dict[str, Any], language: str = "en", *, compact: bool = False) -> str:
    name = _fmt_val(item.get("contact_name") or item.get("name"), language)
    score = item.get("lead_score")
    score_s = f"{score}" if score is not None else _ns(language)
    loc = _fmt_val(item.get("preferred_location") or item.get("location"), language)
    budget = item.get("budget")
    if budget is None or (isinstance(budget, str) and not str(budget).strip()):
        budget_s = _ns(language)
    else:
        budget_s = str(budget).strip()
    email = _fmt_val(item.get("contact_email") or item.get("email"), language)
    test_tag = ""
    if item.get("possible_test_entry") or item.get("test_entry_tag"):
        test_tag = " | possible test entry"
    if compact:
        return f"- **{name}** | score {score_s} | {loc} | budget {budget_s} | {email}{test_tag}"
    phone = _fmt_val(item.get("contact_phone") or item.get("phone"), language)
    lid = _fmt_val(item.get("lead_id") or item.get("id"), language)
    created = _fmt_val(item.get("created_at") or item.get("created"), language)
    return (
        f"- `{lid}` **{name}** | score {score_s} | {loc} | budget {budget_s} | "
        f"{email} | {phone} | created {created}{test_tag}"
    )


def format_property_line(item: Dict[str, Any], language: str = "en") -> str:
    pid = _fmt_val(item.get("property_id") or item.get("id") or item.get("doc_id"), language)
    name = _fmt_val(item.get("property_name") or item.get("name") or item.get("title"), language)
    price = item.get("price_display") or item.get("price")
    price_s = f" | {price}" if price not in (None, "") else ""
    loc = item.get("location") or item.get("city") or item.get("area")
    loc_s = f" | {_fmt_val(loc, language)}" if loc else ""
    return f"- `{pid}` **{name}**{price_s}{loc_s}"


def format_knowledge_block(p: Dict[str, Any], language: str = "en") -> List[str]:
    pl = (language or "en").lower().startswith("pl")
    lines = []
    name = _fmt_val(p.get("name") or p.get("property_name") or p.get("title"), language)
    status = _fmt_val(p.get("status") or p.get("listing_status"), language)
    loc = _fmt_val(p.get("location") or p.get("city") or p.get("area"), language)
    beds = p.get("bedrooms")
    beds_s = _fmt_val(beds, language) if beds is not None else _ns(language)
    completion = _fmt_val(
        p.get("completion_date") or p.get("completion") or p.get("handover_date"),
        language,
    )
    amenities = p.get("amenities")
    if isinstance(amenities, list):
        am_s = ", ".join(str(a) for a in amenities[:12] if a) or _ns(language)
    else:
        am_s = _fmt_val(amenities, language)
    price = p.get("price_display") or p.get("price")
    price_s = _fmt_val(price, language) if price not in (None, "") else _ns(language)
    header = "Wiedza o nieruchomości:" if pl else "Property knowledge:"
    lines.append(header)
    lines.append(f"- **Name:** {name}")
    lines.append(f"- **Status:** {status}")
    lines.append(f"- **Location:** {loc}")
    lines.append(f"- **Bedrooms:** {beds_s}")
    lines.append(f"- **Completion:** {completion}")
    lines.append(f"- **Amenities:** {am_s}")
    lines.append(f"- **Price:** {price_s}")
    return lines


def _format_list_items(items: List[Any], language: str, *, compact_leads: bool = True) -> List[str]:
    lines: List[str] = []
    shown = 0
    lead_cap = 5
    for it in items:
        if not isinstance(it, dict):
            s = str(it).strip()
            if s:
                lines.append(f"- {s[:200]}")
                shown += 1
            continue
        clean = redact_fields_for_template(it)
        if not isinstance(clean, dict):
            continue
        if _looks_like_lead(clean):
            if shown >= lead_cap:
                continue
            line = format_lead_line(clean, language, compact=compact_leads)
            if line.strip() and line.strip() != "-":
                lines.append(line)
                shown += 1
            continue
        if _looks_like_property(clean) or clean.get("property_id") or clean.get("name"):
            line = format_property_line(clean, language)
            # Skip blank
            if "**" in line and not re.search(r"\*\*\s*\*\*", line):
                lines.append(line)
                shown += 1
            elif any(str(v).strip() for v in clean.values() if not isinstance(v, (dict, list))):
                lines.append(line)
                shown += 1
            continue
        scalars = _first_nonempty_scalars(clean, 3)
        if scalars:
            lines.append("- " + " | ".join(scalars))
            shown += 1
    return lines


def build_deterministic_reply_from_tools(
    tool_results: Optional[Sequence[Any]],
    *,
    language: str = "en",
    prior_reason: str = "",
    user_message: Optional[str] = None,
) -> str:
    payloads = [redact_fields_for_template(p) for p in _normalize_tool_payloads(tool_results)]
    pl = (language or "en").lower().startswith("pl")
    lines: List[str] = []
    ask_internal = bool(
        user_message
        and re.search(r"\binternal\s+notes?\b|\bnotatki\s+wewnętrzne\b", user_message, re.I)
    )

    if not payloads:
        return (
            "Brak danych z narzędzi w tej turze — nie mogę potwierdzić liczb ani sukcesu operacji."
            if pl
            else "No tool data for this turn — I cannot confirm counts or that any action succeeded."
        )

    for p in payloads:
        if not isinstance(p, dict):
            continue
        if "authoritative_total_properties" in p or p.get("firestore_count_unavailable"):
            from app.services.admin_chat_quality import format_inventory_reply
            lines.append(format_inventory_reply(p, language))
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
            lines.append(
                f"Znaleziono **{n}** pozycji w wyniku narzędzia:"
                if pl
                else f"Found **{n}** item(s) in the tool result:"
            )
            item_lines = _format_list_items(p, language)
            lines.extend(item_lines)
            if _looks_like_lead(p[0]) if p and isinstance(p[0], dict) else False:
                if n > 5:
                    lines.append(
                        "Pokaż kolejne 5?" if pl else "Show the next 5?"
                    )
            continue

        if not isinstance(p, dict):
            s = str(p).strip()
            if s and not s.startswith("{") and not s.startswith("["):
                lines.append(s[:400])
            continue

        # Leads payload
        leads = p.get("leads")
        if isinstance(leads, list):
            total = p.get("total", len(leads))
            if p.get("summary"):
                lines.append(str(p["summary"]))
            else:
                window = p.get("window_label") or p.get("date_window")
                if window:
                    lines.append(str(window))
                elif p.get("all_time") or p.get("days") in (0, None):
                    lines.append(
                        f"Wszystkie leady (**{total}** łącznie), od najnowszych:"
                        if pl
                        else f"Showing all leads (**{total}** total), newest first:"
                    )
                elif p.get("window_start") and p.get("window_end"):
                    lines.append(
                        f"Ostatnie {p.get('days', '?')} dni ({p.get('window_start')} – {p.get('window_end')}): **{total}** leadów"
                        if pl
                        else f"Last {p.get('days', '?')} days ({p.get('window_start')} to {p.get('window_end')}): **{total}** leads"
                    )
            if p.get("message") and int(total or 0) == 0:
                lines.append(str(p["message"]))
            elif int(total or 0) > 0:
                lines.append(
                    f"Łącznie leadów: **{total}** (pokazuję {min(5, len(leads))}):"
                    if pl
                    else f"Total leads: **{total}** (showing {min(5, len(leads))}):"
                )
                lines.extend(_format_list_items(leads[:5], language, compact_leads=True))
                if int(total or 0) > 5 or len(leads) > 5:
                    lines.append("Pokaż kolejne 5?" if pl else "Show the next 5?")
            if p.get("lead_saving_note"):
                lines.append(str(p["lead_saving_note"]))
            continue

        # Property knowledge (single rich property dict)
        if (
            p.get("amenities") is not None
            or p.get("completion_date")
            or p.get("bedrooms") is not None
            or (p.get("name") and p.get("status") and not p.get("properties"))
        ) and not p.get("properties") and not p.get("items"):
            lines.extend(format_knowledge_block(p, language))
            if ask_internal and isinstance(tool_results, (list, tuple)):
                # Only summarise internal notes when explicitly asked — pull from ORIGINAL (pre-redact)
                for raw in _normalize_tool_payloads(tool_results):
                    if isinstance(raw, dict) and raw.get("internal_notes"):
                        note = str(raw.get("internal_notes"))[:400]
                        lines.append(
                            f"(Internal notes summary): {note}"
                            if not pl
                            else f"(Skrót notatek wewnętrznych): {note}"
                        )
            continue

        if p.get("count") is not None or p.get("total") is not None or p.get("item_count") is not None:
            n = p.get("count", p.get("total", p.get("item_count")))
            label = "pozycji" if pl else "item(s)"
            lines.append(f"{'Łącznie' if pl else 'Total'}: **{n}** {label}.")

        items = p.get("items") or p.get("properties") or p.get("results")
        if isinstance(items, list) and items:
            n = len(items)
            lines.append(f"{'Lista' if pl else 'List'} ({n}):")
            lines.extend(_format_list_items(items[:10], language))

        if p.get("diff_table") or p.get("diff_markdown"):
            lines.append(str(p.get("diff_table") or p.get("diff_markdown")))

        if p.get("status") and p.get("pending_action_id"):
            lines.append(
                f"Pending action `{p.get('pending_action_id')}` status=`{p.get('status')}`."
            )

        if p.get("file_url") or p.get("filename"):
            fname = p.get("filename") or p.get("file_name") or "report"
            lines.append(
                f"Raport: **{fname}**" if pl else f"Report: **{fname}**"
            )
            if p.get("file_url"):
                lines.append(str(p.get("file_url")))

        if p.get("message") and not any(lines):
            msg = str(p.get("message")).strip()
            if msg:
                lines.append(msg)

        er = p.get("execution_result") if isinstance(p.get("execution_result"), dict) else p
        if isinstance(er, dict) and er.get("success") is True and er.get("is_mock") is False:
            lines.append(
                "✅ Operacja zakończona powodzeniem (success=true, is_mock=false)."
                if pl
                else "✅ Operation completed (success=true, is_mock=false)."
            )

    # Generic fallback — never JSON, never empty bullets
    if not lines:
        for p in payloads:
            if isinstance(p, dict):
                scalars = _first_nonempty_scalars(p, 5)
                if scalars:
                    lines.append("Here is what I found:" if not pl else "Oto co znalazłem:")
                    for s in scalars:
                        lines.append(f"- {s}")
                else:
                    for lk in ("items", "properties", "results", "leads"):
                        if isinstance(p.get(lk), list) and p[lk]:
                            lines.extend(_format_list_items(p[lk][:5], language))
                            break
            elif isinstance(p, list) and p:
                lines.extend(_format_list_items(p[:5], language))

    # Drop any blank bullets that slipped through
    cleaned = []
    for ln in lines:
        stripped = ln.strip()
        if not stripped or stripped in ("-", "- ``", "- ** **", "- ` `"):
            continue
        if re.match(r"^-\s*`?\s*`?\s*(\*\*\s*\*\*)?\s*$", stripped):
            continue
        cleaned.append(ln)

    if not cleaned:
        cleaned = [
            "Narzędzie zwróciło dane, ale nie było czytelnych pól do wyświetlenia."
            if pl
            else "Tool returned data but no readable fields were available to display."
        ]

    body = "\n".join(cleaned)
    # Safety: never emit internal redacted keys even if model left them in strings
    for banned in ("internal_notes", "developer_contact", "raw_firestore_data"):
        body = re.sub(rf"{banned}\s*[:=]\s*.*", f"{banned}: [redacted]", body, flags=re.I)
    return body


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
    user_message: Optional[str] = None,
    conversation_history: Optional[Sequence[Any]] = None,
    prior_verified_leads: Optional[Set[str]] = None,
) -> ClaimVerifyResult:
    """
    HARD → regenerate once → template.
    SOFT-only → keep model reply, audit log.
    """
    prior = set(prior_verified_leads or set())
    if conversation_history and not prior:
        prior = extract_prior_verified_lead_names(conversation_history)
    first = verify_admin_claims(
        reply_text, tool_results, prior_verified_leads=prior
    )
    # Soft-only: keep reply
    if first.ok and first.soft_mismatches:
        first.final_text = reply_text
        first.intervention = "soft_logged"
        _log_verifier_intervention(
            db,
            staff_user_id=staff_user_id,
            conversation_id=conversation_id,
            reason=f"soft_mismatch_kept: {first.reason}",
            intervention="soft_logged",
            unsupported={"soft_mismatches": first.soft_mismatches},
        )
        return first

    if first.ok:
        first.final_text = reply_text
        first.intervention = "none"
        return first

    # HARD path
    regenerated: Optional[str] = None
    if regenerate_fn is not None:
        instruction = (
            "GUARDRAIL: use only values from the tool results. "
            "Do not invent counts, prices, percentages, or ids. "
            "Do not claim an action succeeded unless the tool result has success=true and is_mock=false. "
            "Do NOT retract a previously returned lead as demo/unreal/not verified unless this turn's "
            "tools looked up that same lead by id/name/email. A narrower empty window must not erase prior facts. "
            "For example.com emails say 'email domain looks like a placeholder — verify before contacting' — never 'demo'. "
            f"Previous issue: {first.reason}"
        )
        try:
            regenerated = regenerate_fn(instruction)
        except Exception as e:
            logger.warning(f"Claim verifier regenerate failed: {e}")
            regenerated = None

    if regenerated and str(regenerated).strip():
        second = verify_admin_claims(
            regenerated, tool_results, prior_verified_leads=prior
        )
        if second.ok or (second.soft_mismatches and not second.hard_mismatches and not second.success_violations):
            second.final_text = regenerated
            second.intervention = "regenerated"
            _log_verifier_intervention(
                db,
                staff_user_id=staff_user_id,
                conversation_id=conversation_id,
                reason=f"regenerated_after: {first.reason}",
                intervention="regenerated",
                unsupported={
                    "hard_mismatches": first.hard_mismatches,
                    "success_violations": first.success_violations,
                },
            )
            return second

    templated = build_deterministic_reply_from_tools(
        tool_results,
        language=language,
        prior_reason=first.reason,
        user_message=user_message,
    )
    result = ClaimVerifyResult(
        ok=False,
        unsupported_numbers=first.unsupported_numbers,
        unsupported_ids=first.unsupported_ids,
        success_violations=first.success_violations,
        soft_mismatches=first.soft_mismatches,
        hard_mismatches=first.hard_mismatches,
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
            "hard_mismatches": first.hard_mismatches,
            "success_violations": first.success_violations,
            "regenerate_attempted": regenerated is not None,
        },
    )
    return result
