"""Redis-backed restoration store.

Shared across multiple workers/processes. Uses Redis TTL for expiry. Business
logic is unaware of the backend; swap via the RestorationStore interface.
"""

from __future__ import annotations

import redis

from app.restoration.base import RestorationStore
from app.restoration.models import RestorationState

_KEY_PREFIX = "pii:restore:"


class RedisRestorationStore(RestorationStore):
    """Stores restoration state in Redis with TTL."""

    def __init__(self, client: redis.Redis, ttl_seconds: int = 3600) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(payload_id: str) -> str:
        return f"{_KEY_PREFIX}{payload_id}"

    def get(self, payload_id: str) -> RestorationState | None:
        raw = self._client.get(self._key(payload_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return RestorationState.from_json(payload_id, raw)

    def save(self, payload_id: str, state: RestorationState) -> None:
        self._client.set(
            self._key(payload_id),
            state.to_json(),
            ex=self._ttl_seconds,
        )

    def delete(self, payload_id: str) -> None:
        self._client.delete(self._key(payload_id))