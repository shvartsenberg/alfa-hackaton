"""In-memory restoration store with TTL support.

Not safe for horizontal scaling across multiple processes. Replace with
RedisRestorationStore for production. Business logic is unaware of this.

Uses sharded locks to reduce contention under high concurrency.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.core.exceptions import ServiceOverloadedError
from app.restoration.base import RestorationStore
from app.restoration.models import RestorationState

_SHARD_COUNT = 16


@dataclass(slots=True)
class _Shard:
    data: dict[str, tuple[float, RestorationState]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class InMemoryRestorationStore(RestorationStore):
    """Thread-safe in-memory store with TTL eviction and sharded locks."""

    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 100_000) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._shards = [_Shard() for _ in range(_SHARD_COUNT)]

    def _shard_for(self, payload_id: str) -> _Shard:
        index = hash(payload_id) % _SHARD_COUNT
        return self._shards[index]

    def get(self, payload_id: str) -> RestorationState | None:
        shard = self._shard_for(payload_id)
        with shard.lock:
            entry = shard.data.get(payload_id)
            if entry is None:
                return None
            expires_at, state = entry
            if time.monotonic() >= expires_at:
                del shard.data[payload_id]
                return None
            return state

    def save(self, payload_id: str, state: RestorationState) -> None:
        shard = self._shard_for(payload_id)
        with shard.lock:
            if payload_id not in shard.data and len(shard.data) >= self._max_entries:
                self._evict_expired_locked(shard)
                if len(shard.data) >= self._max_entries:
                    raise ServiceOverloadedError(
                        "restoration store is full", retry_after=1
                    )
            expires_at = time.monotonic() + self._ttl_seconds
            shard.data[payload_id] = (expires_at, state)

    def delete(self, payload_id: str) -> None:
        shard = self._shard_for(payload_id)
        with shard.lock:
            shard.data.pop(payload_id, None)

    @staticmethod
    def _evict_expired_locked(shard: _Shard) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in shard.data.items() if now >= exp]
        for key in expired:
            del shard.data[key]