"""Firestore client guard shared by the repositories."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def require_firestore_client(client: Any, *, store_name: str) -> Any:
    """Firestore is the only datastore, so a missing client is a hard error."""
    if client is not None:
        return client
    msg = f"Firestore client unavailable for {store_name}."
    logger.error(msg)
    raise RuntimeError(msg)
