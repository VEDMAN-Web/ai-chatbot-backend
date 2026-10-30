"""FastAPI dependencies for the data layer.

Firestore is the only datastore. There is no SQL session any more. Handlers and services still take a
`db` argument so their signatures stay stable (and so `if db:` guards around audit/snapshot work keep
working), so `get_db` yields a `FirestoreSession` placeholder: truthy, with no-op transaction methods.
It has no `query`/`add`/`execute`, so any leftover SQL call fails loudly instead of silently doing nothing.
"""
from __future__ import annotations

from typing import Any, Generator


class FirestoreSession:
    """Stand-in for the removed SQLAlchemy session. Holds no state."""

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def refresh(self, *args: Any, **kwargs: Any) -> None:
        return None

    def expire_all(self) -> None:
        return None

    def __bool__(self) -> bool:
        return True


def get_db(target: str = "admin") -> Generator[Any, None, None]:
    yield FirestoreSession()


def get_admin_db() -> Generator[Any, None, None]:
    yield FirestoreSession()


def get_user_db() -> Generator[Any, None, None]:
    yield FirestoreSession()
