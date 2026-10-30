import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from app.services.audit_service import record_audit_log

logger = logging.getLogger(__name__)

# Standard Model pricing per 1M tokens (as of 2026)
PRICING_PER_1M = {
    "gpt-4o-mini": {"input": 0.150, "output": 0.600},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-5.5": {"input": 4.00, "output": 16.00},
    "gpt-6-astra": {"input": 5.00, "output": 20.00},
    "claude-3-7-sonnet-20250219": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-opus-5-5": {"input": 15.00, "output": 75.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00}
}

@dataclass
class TurnTokenMetrics:
    request_id: str
    session_id: str
    conversation_id: str
    intent: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls_count: int = 0
    latency_sec: float = 0.0
    cache_hit: bool = False
    estimated_cost_usd: float = 0.0

    def compute_cost(self):
        pricing = PRICING_PER_1M.get(self.model) or PRICING_PER_1M.get("gpt-5.5") or {"input": 4.00, "output": 16.00}
        input_cost = (self.prompt_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.completion_tokens / 1_000_000) * pricing["output"]
        self.estimated_cost_usd = round(input_cost + output_cost, 7)
        return self.estimated_cost_usd


class TokenTracker:
    def __init__(self):
        self._session_metrics: Dict[str, List[TurnTokenMetrics]] = {}

    def log_turn(
        self,
        metrics: TurnTokenMetrics,
        db: Optional[Any] = None
    ) -> None:
        metrics.compute_cost()
        sess = metrics.session_id
        if sess not in self._session_metrics:
            self._session_metrics[sess] = []
        self._session_metrics[sess].append(metrics)

        logger.info(
            f"[TokenTracker] Intent: '{metrics.intent}' | Calls: {metrics.llm_calls_count} | "
            f"Tokens: {metrics.total_tokens} (in: {metrics.prompt_tokens}, out: {metrics.completion_tokens}) | "
            f"Cost: ${metrics.estimated_cost_usd:.6f} | Time: {metrics.latency_sec:.3f}s | Cache: {metrics.cache_hit}"
        )

        # Persist to AuditLog if database session provided
        if db:
            try:
                record_audit_log(
                    db=db,
                    staff_user_id="ai_system",
                    tool_name="llm_token_observability",
                    tool_arguments={
                        "intent": metrics.intent,
                        "model": metrics.model,
                        "prompt_tokens": metrics.prompt_tokens,
                        "completion_tokens": metrics.completion_tokens,
                        "total_tokens": metrics.total_tokens,
                        "llm_calls": metrics.llm_calls_count,
                        "cache_hit": metrics.cache_hit,
                        "estimated_cost_usd": metrics.estimated_cost_usd,
                        "latency_sec": metrics.latency_sec
                    },
                    conversation_id=metrics.conversation_id,
                    status="SUCCESS"
                )
            except Exception as e:
                logger.warning(f"Could not persist token audit log: {e}")

    def get_session_metrics(self, session_id: str) -> List[TurnTokenMetrics]:
        return self._session_metrics.get(session_id, [])

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        turns = self._session_metrics.get(session_id, [])
        return {
            "session_id": session_id,
            "turns_count": len(turns),
            "total_tokens": sum(t.total_tokens for t in turns),
            "total_prompt_tokens": sum(t.prompt_tokens for t in turns),
            "total_completion_tokens": sum(t.completion_tokens for t in turns),
            "total_llm_calls": sum(t.llm_calls_count for t in turns),
            "total_estimated_cost_usd": round(sum(t.estimated_cost_usd for t in turns), 6),
            "cache_hits_count": sum(1 for t in turns if t.cache_hit)
        }

token_tracker = TokenTracker()
