"""In-memory sliding-window rate limiter.

Keys are bounded and old windows are dropped. A Redis backend can replace
``SlidingWindowLimiter`` later without changing ``enforce_rate_limit``.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Sequence

from fastapi import HTTPException, Request, status

from app.core.config import is_test_environment, settings


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 10000):
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = {}
        self._max_keys = max(100, int(max_keys))

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def check(self, key: str, limit: int, window_seconds: float, *, now: Optional[float] = None) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds). A hit is recorded only when allowed."""
        moment = time.monotonic() if now is None else float(now)
        window = max(1.0, float(window_seconds))
        cap = max(1, int(limit))
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self._max_keys:
                    self._evict_locked(moment)
                bucket = deque()
                self._hits[key] = bucket
            cutoff = moment - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket and key in self._hits and self._hits[key] is not bucket:
                pass
            if len(bucket) >= cap:
                retry = int(bucket[0] + window - moment) + 1
                return False, max(1, retry)
            bucket.append(moment)
            if not bucket:
                self._hits.pop(key, None)
            return True, 0

    def _evict_locked(self, now: float) -> None:
        stale = [key for key, bucket in self._hits.items() if not bucket or (now - bucket[-1]) > 3600]
        for key in stale:
            self._hits.pop(key, None)
        if len(self._hits) < self._max_keys:
            return
        ordered = sorted(self._hits.items(), key=lambda item: item[1][0] if item[1] else 0)
        drop = max(1, len(ordered) // 2)
        for key, _bucket in ordered[:drop]:
            self._hits.pop(key, None)


_LIMITER = SlidingWindowLimiter()


def get_rate_limiter() -> SlidingWindowLimiter:
    return _LIMITER


def reset_rate_limiter() -> None:
    _LIMITER.reset()


def limits_active() -> bool:
    """Pytest skips limits unless a test opts in, so the suite cannot lock itself out."""
    if os.environ.get("ENFORCE_RATE_LIMITS") == "1":
        return True
    if is_test_environment():
        return False
    return True


def injected_request(request: Request) -> Request:
    """FastAPI dependency so endpoints can take Request after other defaults."""
    return request


def client_ip(request: Optional[Request]) -> str:
    """Use X-Forwarded-For only when TRUST_PROXY_HEADERS is true."""
    if request is None:
        return "unknown"
    if bool(getattr(settings, "TRUST_PROXY_HEADERS", False)):
        forwarded = (request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return str(host or "unknown")


def enforce_rate_limit(
    request: Optional[Request],
    *,
    scope: str,
    identities: Sequence[Optional[str]] = (),
    limit: int,
    window_seconds: float,
    message: str = "Too many requests. Try again later.",
) -> None:
    if not limits_active():
        return
    keys = [f"{scope}:ip:{client_ip(request)}"]
    for identity in identities:
        clean = (identity or "").strip().lower()
        if clean:
            keys.append(f"{scope}:id:{clean}")
    retry_after = 0
    blocked = False
    for key in keys:
        allowed, retry = _LIMITER.check(key, limit, window_seconds)
        if not allowed:
            blocked = True
            retry_after = max(retry_after, retry)
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"message": message},
            headers={"Retry-After": str(max(1, retry_after))},
        )
