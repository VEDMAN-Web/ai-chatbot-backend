"""
Admin turn performance: timing logs, read-tool cache, budgets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_TOOL_CACHE: Dict[str, Tuple[float, Any]] = {}


@dataclass
class TurnTimer:
    """Collects per-turn timing for ADMIN_TURN_TIMING summary."""

    started: float = field(default_factory=time.perf_counter)
    classifier_ms: float = 0.0
    guardrail_ms: float = 0.0
    verifier_ms: float = 0.0
    llm_calls: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    budget_sec: float = 75.0

    def remaining_sec(self) -> float:
        return max(0.0, self.budget_sec - (time.perf_counter() - self.started))

    def budget_exceeded(self) -> bool:
        return self.remaining_sec() <= 0.5

    def record_llm(
        self,
        *,
        label: str,
        model: str,
        ms: float,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        self.llm_calls.append({
            "label": label,
            "model": model,
            "ms": round(ms, 1),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

    def record_tool(self, name: str, ms: float, *, cached: bool = False) -> None:
        self.tools.append({
            "name": name,
            "ms": round(ms, 1),
            "cached": cached,
        })

    def total_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000.0, 1)

    def summary_line(self) -> str:
        tools_s = ",".join(
            f"{t['name']}:{t['ms']}{'c' if t.get('cached') else ''}" for t in self.tools
        ) or "-"
        llm_n = len(self.llm_calls)
        skip = ",".join(self.skipped) if self.skipped else "-"
        return (
            f"ADMIN_TURN_TIMING total={self.total_ms()}ms "
            f"classifier={self.classifier_ms:.0f}ms "
            f"guardrail={self.guardrail_ms:.0f}ms "
            f"verifier={self.verifier_ms:.0f}ms "
            f"llm_calls={llm_n} tools=[{tools_s}] skipped=[{skip}]"
        )

    def to_debug_dict(self) -> Dict[str, Any]:
        return {
            "total_ms": self.total_ms(),
            "classifier_ms": round(self.classifier_ms, 1),
            "guardrail_ms": round(self.guardrail_ms, 1),
            "verifier_ms": round(self.verifier_ms, 1),
            "llm_calls": self.llm_calls,
            "tools": self.tools,
            "skipped": self.skipped,
        }

    def log_summary(self) -> None:
        logger.info(self.summary_line())


def usage_tokens(response: Any) -> Tuple[Optional[int], Optional[int]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    inp = getattr(usage, "input_tokens", None)
    out = getattr(usage, "output_tokens", None)
    return inp, out


def _cache_key(tool_name: str, args: Any) -> str:
    raw = json.dumps({"t": tool_name, "a": args}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tool_cache_get(tool_name: str, args: Any, ttl_sec: float) -> Optional[Any]:
    if ttl_sec <= 0:
        return None
    key = _cache_key(tool_name, args)
    now = time.time()
    with _CACHE_LOCK:
        hit = _TOOL_CACHE.get(key)
        if not hit:
            return None
        ts, val = hit
        if now - ts > ttl_sec:
            _TOOL_CACHE.pop(key, None)
            return None
        return val


def tool_cache_set(tool_name: str, args: Any, value: Any) -> None:
    key = _cache_key(tool_name, args)
    with _CACHE_LOCK:
        _TOOL_CACHE[key] = (time.time(), value)


def tool_cache_invalidate_all() -> None:
    with _CACHE_LOCK:
        _TOOL_CACHE.clear()
    logger.info("ADMIN_TOOL_CACHE cleared after write")


# Tools safe to cache for ~60s (read-only)
CACHEABLE_TOOLS = frozenset({
    "get_database_inventory_stats",
    "get_buyer_leads",
    "search_properties_full",
    "search_properties",
    "get_property_knowledge",
})


def admin_fast_model() -> str:
    from app.core.config import settings
    return getattr(settings, "ADMIN_FAST_MODEL", None) or "claude-haiku-4-5"


def admin_main_model() -> str:
    from app.core.config import settings
    main = (getattr(settings, "ADMIN_MAIN_MODEL", None) or "").strip()
    if main:
        return main
    return getattr(settings, "ANTHROPIC_MODEL", "claude-opus-5-5")


def classify_obvious_intent(message: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Rules-first classifier. Returns a result dict if obvious (skip LLM), else None.
    Compatible with AIIntentClassificationResult fields.
    """
    import re
    raw = (message or "").strip()
    if not raw:
        return {
            "primary_intent": "general_chat",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 1.0,
            "explanation": "empty",
            "skipped_llm": True,
        }
    msg = raw.lower()
    # Negative verification / "did I ask for a PDF?" — before any doc-request rule
    if any(
        w in msg
        for w in (
            "did i ask",
            "didn't ask",
            "did not ask",
            "don't create",
            "dont create",
            "no pdf",
            "stop creating",
            "why did you make",
            "nie prosiłem",
            "nie prosilem",
            "nie generuj",
            "czy prosiłem",
            "czy prosilem",
            "nie chciałem",
            "nie chcialem",
        )
    ):
        return {
            "primary_intent": "negative_feedback",
            "document_requested": False,
            "negative_feedback": True,
            "requires_verification": True,
            "confidence": 0.95,
            "explanation": "rules: negative verification",
            "skipped_llm": True,
        }
    # Confirm / reject / cancel
    if re.fullmatch(r"(yes|y|ok|okay|confirm|approve|zatwierd[zź]|tak)(\s+\d+)?", msg):
        return {
            "primary_intent": "confirm_action",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.99,
            "explanation": "rules: confirm",
            "skipped_llm": True,
        }
    if re.fullmatch(r"(no|n|cancel|reject|anuluj|odrzu[cć])", msg):
        return {
            "primary_intent": "reject_action",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.99,
            "explanation": "rules: reject",
            "skipped_llm": True,
        }
    if re.search(r"\b(undo|rollback|revert|cofnij)\b", msg):
        return {
            "primary_intent": "general_chat",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.95,
            "explanation": "rules: undo",
            "skipped_llm": True,
            "fast_path": "undo",
        }
    # Greetings
    if re.fullmatch(
        r"(hi|hello|hey|cześć|czesc|dzień dobry|dzien dobry|good (morning|afternoon|evening)|hola)[!?.]*",
        msg,
    ):
        return {
            "primary_intent": "general_chat",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.99,
            "explanation": "rules: greeting",
            "skipped_llm": True,
            "fast_path": "greeting",
        }
    # Capabilities
    if any(
        p in msg
        for p in (
            "what can you do",
            "what do you do",
            "your capabilities",
            "co potrafisz",
            "co umiesz",
        )
    ):
        return {
            "primary_intent": "general_chat",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.95,
            "explanation": "rules: capabilities",
            "skipped_llm": True,
            "fast_path": "capabilities",
        }
    # Leads
    if re.search(r"\b(leads?|buyer leads?|leadów|lead)\b", msg) and not any(
        w in msg for w in ("pdf", "report", "excel", "export")
    ):
        return {
            "primary_intent": "buyer_leads",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.95,
            "explanation": "rules: leads",
            "skipped_llm": True,
            "fast_path": "leads",
        }
    # Inventory / how many
    if any(
        w in msg
        for w in (
            "how many",
            "ile nieruchomości",
            "ile w bazie",
            "inventory",
            "total properties",
            "properties do we have",
            "properties in",
        )
    ):
        return {
            "primary_intent": "inventory_inquiry",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.95,
            "explanation": "rules: inventory",
            "skipped_llm": True,
            "fast_path": "inventory",
        }
    # Pending actions
    if "pending action" in msg or "akcja oczekująca" in msg or "pending actions" in msg:
        return {
            "primary_intent": "general_chat",
            "document_requested": False,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.9,
            "explanation": "rules: pending",
            "skipped_llm": True,
            "fast_path": "pending",
        }
    # Explicit PDF / report (exclude clarification / negation questions)
    if any(
        p in msg
        for p in (
            "pdf",
            "dossier",
            "report",
            "raport",
            "excel",
            "export",
            "download",
            "portfolio",
        )
    ) and any(
        a in msg
        for a in (
            "give",
            "make",
            "create",
            "generate",
            "want",
            "send",
            "pobierz",
            "wygeneruj",
            "zrób",
            "zrob",
            "daj",
        )
    ) and not any(
        n in msg for n in ("did i ask", "didn't", "did not", "don't", "dont", "stop ", "no pdf", "why did you")
    ):
        is_portfolio = any(
            w in msg for w in ("portfolio", "client", "offer", "dossier", "oferta")
        )
        return {
            "primary_intent": "request_portfolio" if is_portfolio else "request_report",
            "document_requested": True,
            "negative_feedback": False,
            "requires_verification": False,
            "confidence": 0.95,
            "explanation": "rules: document request",
            "skipped_llm": True,
        }
    return None


def should_run_guardrail_judge(
    *,
    requires_confirmation: bool = False,
    action_status: Optional[str] = None,
    download_link: Optional[str] = None,
    tool_calls: Optional[List[Any]] = None,
    claim_hard_mismatch: bool = False,
    document_requested: bool = False,
) -> bool:
    """
    Judge ONLY when write proposed/executed, document generated,
    or deterministic claim verifier found hard mismatches.
    """
    if requires_confirmation:
        return True
    if action_status in ("executed", "proposed", "awaiting_confirmation", "stale_refused"):
        return True
    if download_link:
        return True
    names = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            names.append(str(tc.get("name") or "").lower())
    doc_tools = {"generate_report", "generate_client_offer_dossier", "generate_property_brochure"}
    if any(n in doc_tools for n in names):
        return True
    if claim_hard_mismatch:
        return True
    if document_requested:
        return True
    return False


def is_list_style_reply(tool_calls: Optional[List[Any]] = None, tool_results: Optional[List[Any]] = None) -> bool:
    names = set()
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            names.add(str(tc.get("name") or "").lower())
    if names & {"get_buyer_leads", "search_properties_full", "search_properties", "generate_report"}:
        return True
    for tr in tool_results or []:
        payload = tr.get("result") if isinstance(tr, dict) else tr
        if isinstance(payload, dict) and (
            payload.get("leads") is not None
            or payload.get("properties") is not None
            or payload.get("items") is not None
        ):
            return True
    return False
