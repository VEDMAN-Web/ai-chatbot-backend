"""Read-only switch for listing changes (ADMIN_LISTING_WRITES_ENABLED)."""
from __future__ import annotations

import re

from app.core.config import settings

LISTING_WRITES_DISABLED_MSG = (
    "Listing changes are switched off on this server (read-only mode). Nothing was changed."
)

# Confirmed chat actions that change listings
LISTING_WRITE_TOOLS = frozenset({
    "create_or_update_listing",
    "ingest_property_from_drive",
    "ingest_property_from_url",
    "archive_listing",
    "undo_operation",
    "bulk_create_from_document",
    "execute_batch_property_action",
    "manage_property_images",
    "approve_drive_ingestion",
})

# REST routes that change listings (with or without the /api/v1 prefix)
_WRITE_PATH = re.compile(
    r"^(?:/api/v1)?(?:"
    r"/admin/properties(?:/.*)?"
    r"|/admin/property-images/upload"
    r"|/admin/operations/[^/]+/undo"
    r"|/admin/drive-ingestion/jobs/[^/]+/(?:approve|rollback)"
    r"|/properties/ingest-(?:document|url)"
    r")/?$"
)
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def listing_writes_enabled() -> bool:
    return bool(getattr(settings, "ADMIN_LISTING_WRITES_ENABLED", True))


def is_listing_write_request(method: str, path: str) -> bool:
    return (method or "").upper() in _WRITE_METHODS and bool(_WRITE_PATH.match(path or ""))
