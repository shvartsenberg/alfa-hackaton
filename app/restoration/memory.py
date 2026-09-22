"""In-memory restoration store with TTL support and encryption at rest.

Not safe for horizontal scaling across multiple processes. Replace with
RedisRestorationStore for production. Business logic is unaware of this.

Values are stored encrypted with Fernet so that original text and mappings
never appear in plaintext in memory. The key comes from the ``MASKING_KEY``
environment variable; if it is unset a random key is generated at startup
(which means state is not recoverable across restarts).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet

from app.restoration.base import RestorationStore
from app.restoration.models import RestorationState

logger = logging.getLogger("pii_security_proxy.restoration.memory")


def _to_json(state: RestorationState) -> str:
    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "value"):
            return obj.value
        raise TypeError(f"cannot serialize {type(obj)!r}")

    return json.dumps(
        {
            "payload_id": state.payload_id,
            "original_hash": state.original_hash,
            "original_text": state.original_text,
            "masked_text": state.masked_text,
            "mappings": state.mappings,
            "state": state.state.value,
            "created_at": state.created_at,
            "expires_at": state.expires_at,
        },
        default=default,
    )


def _from_json(raw: str) -> RestorationState:
    data = json.loads(raw)
    return RestorationState(
        payload_id=data["payload_id"],
        original_hash=data["original_hash"],
        original_text=data["original_text"],
        masked_text=data["masked_text"],
        mappings=data["mappings"],
        state=data["state"],
        created_at=datetime.fromisoformat(data["created_at"]),
        expires_at=(
            datetime.fromisoformat(data["expires_at"])
            if data.get("expires_at")
            else None
        ),
    )


class InMemoryRestorationStore(RestorationStore):
    """Thread-safe in-memory store with TTL eviction and Fernet encryption."""

    def __init__(
        self,
        ttl_seconds: int = 3600,
        max_entries: int = 100_000,
        masking_key: str | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._data: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()
        self._fernet = self._build_fernet(masking_key)

    @staticmethod
    def _build_fernet(masking_key: str | None) -> Fernet:
        key = masking_key or os.environ.get("MASKING_KEY")
        if key:
            return Fernet(key.encode("utf-8"))
        generated = Fernet.generate_key()
        logger.warning(
            "MASKING_KEY not set; generated a random Fernet key at startup. "
            "Restoration state will not be recoverable across restarts."
        )
        return Fernet(generated)

    def get(self, payload_id: str) -> RestorationState | None:
        with self._lock:
            entry = self._data.get(payload_id)
            if entry is None:
                return None
            expires_at, encrypted = entry
            if time.monotonic() >= expires_at:
                del self._data[payload_id]
                return None
            raw = self._fernet.decrypt(encrypted).decode("utf-8")
            return _from_json(raw)

    def save(self, payload_id: str, state: RestorationState) -> None:
        with self._lock:
            self._evict_expired_locked()
            if payload_id not in self._data and len(self._data) >= self._max_entries:
                # Still full after eviction: drop the oldest entry to bound memory.
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            expires_at = time.monotonic() + self._ttl_seconds
            encrypted = self._fernet.encrypt(_to_json(state).encode("utf-8"))
            self._data[payload_id] = (expires_at, encrypted)

    def delete(self, payload_id: str) -> None:
        with self._lock:
            self._data.pop(payload_id, None)

    def _evict_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._data.items() if now >= exp]
        for key in expired:
            del self._data[key]