"""Unit tests for the rate limiter."""

from __future__ import annotations

from app.core.ratelimit import SlidingWindowRateLimiter


def test_allows_within_limit() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=10.0)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True


def test_rejects_over_limit() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=10.0)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_keys_are_isolated() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=10.0)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True