"""STORE_* mode helpers for Stage F1 dual-store reads/writes."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

VALID_STORE_MODES = frozenset({"postgres", "firestore", "both"})


def normalize_store_mode(value: Optional[str], default: str = "both") -> str:
    mode = (value or default or "both").strip().lower()
    if mode not in VALID_STORE_MODES:
        logger.warning("Unknown store mode %r — using %s", value, default)
        return default
    return mode


def store_mode(setting_name: str, default: str = "both") -> str:
    from app.core.config import settings

    return normalize_store_mode(getattr(settings, setting_name, None), default=default)


def writes_postgres(mode: str) -> bool:
    return mode in ("postgres", "both")


def writes_firestore(mode: str) -> bool:
    return mode in ("firestore", "both")


def reads_firestore_first(mode: str) -> bool:
    return mode in ("firestore", "both")


def reads_postgres(mode: str) -> bool:
    return mode in ("postgres", "both")


def log_store_modes_at_startup() -> None:
    """Emit one INFO line per STORE_* flag (values only, never secrets)."""
    pending = store_mode("STORE_PENDING_ACTIONS")
    audit = store_mode("STORE_AUDIT")
    leads = store_mode("STORE_LEADS_READ")
    logger.info(
        "STORE modes: pending_actions=%s audit=%s leads_read=%s",
        pending,
        audit,
        leads,
    )
