"""In-memory sliding-window rate limiter.

Simple per-key rate limiting used to trigger 429 Too Many Requests. Not
distributed-safe; replace with a Redis-backed limiter for horizontal scaling.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Limits requests per key within a fixed window."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Return True if the request for ``key`` is allowed."""
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(key, deque())
            while window and now - window[0] >= self._window_seconds:
                window.popleft()
            if len(window) >= self._max_requests:
                return False
            window.append(now)
            return True