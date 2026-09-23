"""Benchmark POST /process latency at a target concurrency.

Measures p50/p95/p99 and average latency per HTTP request, achieved RPS, and
the full set of report metrics (mask/demask attempts, successful mask/demask,
HTTP 2xx/429, functional and transport errors) using synchronous keep-alive
connections (each waits for the response before the next request).

Latency is recorded per HTTP request (each mask and each demask separately),
not per roundtrip, so the percentiles reflect single-request latency. Every
POST counts toward ``total_requests`` and toward either ``mask_attempts`` or
``demask_attempts``, so ``total_requests == mask_attempts + demask_attempts``
always holds. ``successful_mask``/``successful_demask`` are subsets (only valid
200 results). The process exits non-zero on functional/transport errors or when
no requests were made; a valid 429 (with Retry-After) is not an error.

Usage:
    python scripts/benchmark.py --url http://localhost:8000 --users 200 --duration 30

Reports p50/p95/p99 latency, achieved RPS, and error rate.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

ORIGINAL = (
    "Клиент Иванов Иван Иванович, паспорт 4509 123456, ИНН 7707083893, "
    "карта 4111 1111 1111 1111, телефон +7 999 123-45-67, "
    "email test@example.com, дата рождения 15.03.1990"
)
# Original values that must be absent from any masked result.
MASKED_SENTINELS = (
    "test@example.com",
    "+7 999 123-45-67",
    "4111 1111 1111 1111",
    "7707083893",
    "4509 123456",
    "Иванов Иван Иванович",
)


class _Counters:
    """Thread-safe counters for the report metrics.

    ``total_requests`` counts every HTTP POST attempt. ``mask_attempts`` and
    ``demask_attempts`` partition it (each POST is either a mask or a demask
    attempt), so ``total_requests == mask_attempts + demask_attempts`` always
    holds. ``successful_mask``/``successful_demask`` count only the attempts
    that returned a valid 200 result, so they are a subset of the attempts.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latencies: list[float] = []
        self.total_requests = 0
        self.mask_attempts = 0
        self.demask_attempts = 0
        self.successful_mask = 0
        self.successful_demask = 0
        self.http_2xx = 0
        self.http_429 = 0
        self.functional_errors = 0
        self.transport_errors = 0

    def record_latency(self, ms: float) -> None:
        with self.lock:
            self.latencies.append(ms)

    def bump(self, key: str) -> None:
        with self.lock:
            setattr(self, key, getattr(self, key) + 1)


def _post(client: httpx.Client, payload_id: str, payload: str, counters: _Counters) -> str | None:
    """Issue one POST, record latency and status, return the result string."""
    started = time.perf_counter()
    try:
        response = client.post(
            "/process", json={"payload": payload, "payload_id": payload_id}
        )
    except httpx.HTTPError:
        counters.bump("transport_errors")
        counters.bump("total_requests")
        return None
    counters.record_latency((time.perf_counter() - started) * 1000.0)
    counters.bump("total_requests")
    if response.status_code == 429:
        counters.bump("http_429")
        # A 429 must carry Retry-After; without it the overload signal is
        # malformed and counts as a functional error.
        if response.headers.get("Retry-After") is None:
            counters.bump("functional_errors")
        return None
    if response.status_code != 200:
        counters.bump("functional_errors")
        return None
    counters.bump("http_2xx")
    try:
        result = response.json()["result"]
    except (ValueError, KeyError, TypeError):
        counters.bump("functional_errors")
        return None
    if not isinstance(result, str):
        counters.bump("functional_errors")
        return None
    return result


def _run_user(url: str, duration: float, counters: _Counters) -> None:
    client = httpx.Client(base_url=url, timeout=10.0)
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        payload_id = str(uuid.uuid4())
        counters.bump("mask_attempts")
        masked = _post(client, payload_id, ORIGINAL, counters)
        if masked is None:
            continue
        counters.bump("successful_mask")
        leaked = [sentinel for sentinel in MASKED_SENTINELS if sentinel in masked]
        if leaked:
            counters.bump("functional_errors")
            continue
        counters.bump("demask_attempts")
        demasked = _post(client, payload_id, masked, counters)
        if demasked is None:
            continue
        counters.bump("successful_demask")
        if demasked != ORIGINAL:
            counters.bump("functional_errors")
    client.close()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, int(len(values) * p))
    return values[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--duration", type=float, default=30.0)
    args = parser.parse_args(argv)

    counters = _Counters()

    def worker() -> None:
        _run_user(args.url, args.duration, counters)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.users) as pool:
        futures = [pool.submit(worker) for _ in range(args.users)]
        for f in futures:
            f.result()
    elapsed = time.monotonic() - started

    total_requests = counters.total_requests
    rps = total_requests / elapsed if elapsed else 0.0
    error_rate = (
        (counters.functional_errors + counters.transport_errors) / total_requests
        if total_requests
        else 0.0
    )
    latencies = counters.latencies
    avg = sum(latencies) / len(latencies) if latencies else 0.0

    print(f"users={args.users} duration={args.duration:.0f}s")
    print(f"achieved_rps={rps:.1f}")
    print(f"total_requests={total_requests}")
    print(
        f"mask_attempts={counters.mask_attempts} "
        f"demask_attempts={counters.demask_attempts} "
        f"(successful_mask={counters.successful_mask} "
        f"successful_demask={counters.successful_demask})"
    )
    print(f"http_2xx={counters.http_2xx} http_429={counters.http_429}")
    print(
        f"functional_errors={counters.functional_errors} "
        f"transport_errors={counters.transport_errors}"
    )
    print(
        f"avg={avg:.2f}ms p50={percentile(latencies, 0.50):.2f}ms "
        f"p95={percentile(latencies, 0.95):.2f}ms p99={percentile(latencies, 0.99):.2f}ms"
    )
    print(f"error_rate={error_rate:.4f}")

    # Reconciliation: every HTTP POST is either a mask or a demask attempt.
    if counters.mask_attempts + counters.demask_attempts != total_requests:
        print(
            "ERROR: counter mismatch: mask_attempts + demask_attempts != total_requests",
            file=sys.stderr,
        )
        return 3
    # Non-zero exit on functional/transport errors or no requests at all.
    if total_requests == 0:
        print("ERROR: no requests were made", file=sys.stderr)
        return 2
    if counters.functional_errors or counters.transport_errors:
        print(
            "ERROR: functional/transport errors detected "
            f"(functional={counters.functional_errors} transport={counters.transport_errors})",
            file=sys.stderr,
        )
        return 1
    # A run with only valid 429s and no successful MASK/DEMASK does not prove
    # the service works. Require at least one successful mask and one successful
    # demask (a valid 429 is not a functional error, but it is not success).
    if counters.successful_mask == 0 or counters.successful_demask == 0:
        print(
            "ERROR: no successful mask/demask roundtrip "
            f"(successful_mask={counters.successful_mask} "
            f"successful_demask={counters.successful_demask})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())