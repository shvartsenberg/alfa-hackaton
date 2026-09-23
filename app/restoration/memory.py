"""In-memory restoration store with TTL support and encryption at rest.

Not safe for horizontal scaling across multiple processes. Replace with
RedisRestorationStore for production. Business logic is unaware of this.

Values are stored encrypted with Fernet so that original text and mappings
never appear in plaintext in memory. The key comes from the ``MASKING_KEY``
environment variable; if it is unset a random key is generated at startup
(which means state is not recoverable across restarts).

Entries are kept in an ``OrderedDict`` in insertion order. Because every entry
shares the same TTL, insertion order matches expiry order, so expired entries
are evicted from the front with ``popitem(last=False)`` in amortized O(1)
instead of scanning the whole store on every write.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict

from cryptography.fernet import Fernet

from app.core.exceptions import ServiceOverloadedError
from app.restoration.base import RestorationStore, TransitionFn
from app.restoration.models import RestorationState

logger = logging.getLogger("pii_security_proxy.restoration.memory")


class InMemoryRestorationStore(RestorationStore):
    """Thread-safe in-memory store with TTL eviction and Fernet encryption."""

    def __init__(
        self,
        ttl_seconds: int = 3600,
        max_entries: int = 1_000_000,
        masking_key: str | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()
        self._fernet = self._build_fernet(masking_key)

    @staticmethod
    def _build_fernet(masking_key: str | None) -> Fernet:
        key = masking_key or os.environ.get("MASKING_KEY")
        if key:
            return Fernet(key.encode("utf-8"))
        if os.environ.get("APP_ENV", "development").lower() == "production":
            # In production a missing key would make state unrecoverable across
            # restarts; fail fast at startup instead of on the first request.
            raise RuntimeError(
                "MASKING_KEY must be set when APP_ENV=production; refusing to "
                "generate a random key that would be unrecoverable across restarts"
            )
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
            return RestorationState.from_json(payload_id, raw)

    def save(self, payload_id: str, state: RestorationState) -> None:
        with self._lock:
            self._evict_expired_locked()
            if payload_id not in self._data and len(self._data) >= self._max_entries:
                # Never sacrifice a live mapping: doing so would make a
                # previously issued mask impossible to restore.
                raise ServiceOverloadedError(
                    "restoration store capacity exceeded",
                    retry_after=1,
                )
            if payload_id in self._data:
                # Refresh: treat the entry as freshly inserted for eviction order.
                self._data.move_to_end(payload_id)
            expires_at = time.monotonic() + self._ttl_seconds
            encrypted = self._fernet.encrypt(state.to_json().encode("utf-8"))
            self._data[payload_id] = (expires_at, encrypted)

    def delete(self, payload_id: str) -> None:
        with self._lock:
            self._data.pop(payload_id, None)

    def transition(
        self, payload_id: str, fn: TransitionFn
    ) -> RestorationState | None:
        with self._lock:
            self._evict_expired_locked()
            entry = self._data.get(payload_id)
            current: RestorationState | None = None
            if entry is not None:
                expires_at, encrypted = entry
                if time.monotonic() < expires_at:
                    raw = self._fernet.decrypt(encrypted).decode("utf-8")
                    current = RestorationState.from_json(payload_id, raw)
                else:
                    del self._data[payload_id]
            new_state = fn(current)
            if new_state is None:
                self._data.pop(payload_id, None)
                return None
            if payload_id not in self._data and len(self._data) >= self._max_entries:
                raise ServiceOverloadedError(
                    "restoration store capacity exceeded",
                    retry_after=1,
                )
            if payload_id in self._data:
                self._data.move_to_end(payload_id)
            expires_at = time.monotonic() + self._ttl_seconds
            encrypted = self._fernet.encrypt(new_state.to_json().encode("utf-8"))
            self._data[payload_id] = (expires_at, encrypted)
            return new_state

    def _evict_expired_locked(self) -> None:
        now = time.monotonic()
        while self._data:
            first_key, (first_exp, _) = next(iter(self._data.items()))
            if now < first_exp:
                break
            del self._data[first_key]