"""Benchmark POST /process latency at a target concurrency/RPS.

Measures p50/p95/p99 latency and achieved RPS using synchronous keep-alive
connections (each waits for the response before the next request), matching
the target load profile.

Usage:
    python scripts/benchmark.py --url http://localhost:8000 --users 200 --duration 30

Reports p50/p95/p99 latency, achieved RPS, and error rate.
"""

from __future__ import annotations

import argparse
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"


def _run_user(url: str, duration: float, latencies: list[float], errors: list[int]) -> None:
    client = httpx.Client(base_url=url, timeout=10.0)
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        payload_id = str(uuid.uuid4())
        started = time.perf_counter()
        try:
            mask = client.post(
                "/process", json={"payload": ORIGINAL, "payload_id": payload_id}
            )
            if mask.status_code != 200:
                errors.append(1)
                continue
            masked = mask.json()["result"]
            demask = client.post(
                "/process", json={"payload": masked, "payload_id": payload_id}
            )
            if demask.status_code != 200:
                errors.append(1)
                continue
            latencies.append((time.perf_counter() - started) * 1000.0)
        except httpx.HTTPError:
            errors.append(1)
    client.close()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, int(len(values) * p))
    return values[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--duration", type=float, default=30.0)
    args = parser.parse_args()

    latencies: list[float] = []
    errors: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        local_lat: list[float] = []
        local_err: list[int] = []
        _run_user(args.url, args.duration, local_lat, local_err)
        with lock:
            latencies.extend(local_lat)
            errors.extend(local_err)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.users) as pool:
        futures = [pool.submit(worker) for _ in range(args.users)]
        for f in futures:
            f.result()
    elapsed = time.monotonic() - started

    total_requests = len(latencies) + len(errors)
    rps = total_requests / elapsed
    error_rate = len(errors) / total_requests if total_requests else 0.0

    print(f"users={args.users} duration={args.duration:.0f}s")
    print(f"total_requests={total_requests} achieved_rps={rps:.1f}")
    print(f"p50={percentile(latencies, 0.50):.2f}ms "
          f"p95={percentile(latencies, 0.95):.2f}ms "
          f"p99={percentile(latencies, 0.99):.2f}ms")
    print(f"error_rate={error_rate:.4f}")


if __name__ == "__main__":
    main()