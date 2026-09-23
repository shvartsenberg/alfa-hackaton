"""Redis-backed restoration store.

Shared across multiple workers/processes. Uses Redis TTL for expiry and an
optimistic compare-and-set (WATCH/MULTI) for atomic lifecycle transitions.
Business logic is unaware of the backend; swap via the RestorationStore
interface.

Security:
- The full restoration state (including ``original_text`` and mappings) is
  encrypted with Fernet before being written to Redis.
- The Redis key is derived from an HMAC of ``payload_id`` so the raw
  identifier is never used as a key.
- In Redis mode the ``MASKING_KEY`` must be set; a missing key fails fast
  instead of silently generating a random one (which would make state
  unrecoverable across workers).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import time

import redis
from cryptography.fernet import Fernet

from app.core.exceptions import ServiceOverloadedError
from app.restoration.base import RestorationStore, TransitionFn
from app.restoration.models import RestorationState

logger = logging.getLogger("pii_security_proxy.restoration.redis")

_KEY_PREFIX = "pii:restore:"
_MAX_CAS_RETRIES = 5
# Base backoff (seconds) between CAS retries; doubled per attempt with jitter.
_CAS_BACKOFF_BASE = 0.005


class RedisRestorationStore(RestorationStore):
    """Stores encrypted restoration state in Redis with TTL and atomic CAS."""

    def __init__(
        self,
        client: redis.Redis,
        ttl_seconds: int = 3600,
        masking_key: str | None = None,
        key_hmac_secret: str | None = None,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._fernet = self._build_fernet(masking_key)
        self._hmac_secret = (
            key_hmac_secret or masking_key or os.environ.get("MASKING_KEY") or ""
        ).encode("utf-8")
        if not self._hmac_secret:
            raise RuntimeError(
                "MASKING_KEY must be set when using the Redis restoration store"
            )

    @staticmethod
    def _build_fernet(masking_key: str | None) -> Fernet:
        key = masking_key or os.environ.get("MASKING_KEY")
        if not key:
            raise RuntimeError(
                "MASKING_KEY must be set when using the Redis restoration store; "
                "refusing to generate a random key that would be unrecoverable "
                "across workers"
            )
        return Fernet(key.encode("utf-8"))

    def _key(self, payload_id: str) -> str:
        digest = hmac.new(
            self._hmac_secret, payload_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{_KEY_PREFIX}{digest}"

    def _decode(self, payload_id: str, raw: bytes | str) -> RestorationState:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        plaintext = self._fernet.decrypt(raw.encode("utf-8")).decode("utf-8")
        return RestorationState.from_json(payload_id, plaintext)

    def _encode(self, state: RestorationState) -> str:
        return self._fernet.encrypt(state.to_json().encode("utf-8")).decode("utf-8")

    def get(self, payload_id: str) -> RestorationState | None:
        raw = self._client.get(self._key(payload_id))
        if raw is None:
            return None
        return self._decode(payload_id, raw)

    def save(self, payload_id: str, state: RestorationState) -> None:
        # Plain overwrite; used only for direct writes, not the lifecycle.
        self._client.set(
            self._key(payload_id),
            self._encode(state),
            ex=self._ttl_seconds,
        )

    def delete(self, payload_id: str) -> None:
        self._client.delete(self._key(payload_id))

    def transition(
        self, payload_id: str, fn: TransitionFn
    ) -> RestorationState | None:
        key = self._key(payload_id)
        for attempt in range(_MAX_CAS_RETRIES):
            with self._client.pipeline() as pipe:
                try:
                    pipe.watch(key)  # type: ignore[no-untyped-call]
                    raw = pipe.get(key)
                    current: RestorationState | None = None
                    if raw is not None:
                        current = self._decode(payload_id, raw)
                    new_state = fn(current)
                    pipe.multi()
                    if new_state is None:
                        pipe.delete(key)
                    else:
                        pipe.set(key, self._encode(new_state), ex=self._ttl_seconds)
                    pipe.execute()
                    return new_state
                except redis.WatchError:
                    # The key changed while we were computing; back off with
                    # jitter and retry so concurrent writers do not spin.
                    time.sleep(random.uniform(0, _CAS_BACKOFF_BASE * (2**attempt)))
                    continue
        # Exhausted retries under contention: signal the client to retry later
        # (429 with Retry-After) instead of failing with a 500.
        raise ServiceOverloadedError(
            "concurrent modification of restoration state",
            retry_after=1,
        )