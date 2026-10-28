"""STORE_PROPERTIES_WRITE: where property listings are written (Stage F2).

  both      (default) write Postgres and Firestore, as before
  postgres  legacy SQL-first behaviour (same code path as 'both')
  firestore Firestore only. No SQL reads, writes, snapshots or restores for properties.

In firestore mode the SQL session is treated as absent for property work, so a stale SQL row can
never be read back, snapshotted or "restored" over the Firestore document.
"""
from __future__ import annotations

from app.repositories.store_mode import store_mode


def mode() -> str:
    return store_mode("STORE_PROPERTIES_WRITE", default="both")


def sql_enabled() -> bool:
    return mode() != "firestore"
