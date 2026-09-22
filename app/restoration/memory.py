"""In-memory restoration store with TTL support.

Not safe for horizontal scaling across multiple processes. Replace with
RedisRestorationStore for production. Business logic is unaware of this.
"""

from __future__ import annotations

import threading
import time

from app.core.exceptions import ServiceOverloadedError
from app.restoration.base import RestorationStore
from app.restoration.models import RestorationState


class InMemoryRestorationStore(RestorationStore):
    """Thread-safe in-memory store with TTL eviction."""

    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 100_000) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._data: dict[str, tuple[float, RestorationState]] = {}
        self._lock = threading.Lock()

    def get(self, payload_id: str) -> RestorationState | None:
        with self._lock:
            entry = self._data.get(payload_id)
            if entry is None:
                return None
            expires_at, state = entry
            if time.monotonic() >= expires_at:
                del self._data[payload_id]
                return None
            return state

    def save(self, payload_id: str, state: RestorationState) -> None:
        with self._lock:
            if payload_id not in self._data and len(self._data) >= self._max_entries:
                self._evict_expired_locked()
                if len(self._data) >= self._max_entries:
                    raise ServiceOverloadedError(
                        "restoration store is full", retry_after=1
                    )
            expires_at = time.monotonic() + self._ttl_seconds
            self._data[payload_id] = (expires_at, state)

    def delete(self, payload_id: str) -> None:
        with self._lock:
            self._data.pop(payload_id, None)

    def _evict_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._data.items() if now >= exp]
        for key in expired:
            del self._data[key]