"""Minimal metrics instrumentation layer.

A lightweight abstraction so the hot path can record counters and timings
without depending on a full Prometheus client. Swap in a real metrics backend
later without changing business code. Never put payload content in labels.

Uses sharded buffers to reduce lock contention under high concurrency.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

_SHARD_COUNT = 16


@dataclass(slots=True)
class _Shard:
    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    histograms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    lock: threading.Lock = field(default_factory=threading.Lock)


class Metrics:
    """Sharded in-memory metrics registry."""

    def __init__(self) -> None:
        self._shards = [_Shard() for _ in range(_SHARD_COUNT)]

    def _shard_for(self, name: str) -> _Shard:
        return self._shards[hash(name) % _SHARD_COUNT]

    def inc(self, name: str, value: int = 1) -> None:
        shard = self._shard_for(name)
        with shard.lock:
            shard.counters[name] += value

    def observe(self, name: str, value: float) -> None:
        shard = self._shard_for(name)
        with shard.lock:
            shard.histograms[name].append(value)

    def snapshot(self) -> dict[str, object]:
        counters: dict[str, int] = defaultdict(int)
        histograms: dict[str, list[float]] = defaultdict(list)
        for shard in self._shards:
            with shard.lock:
                for name, value in shard.counters.items():
                    counters[name] += value
                for name, values in shard.histograms.items():
                    histograms[name].extend(values)
        return {
            "counters": dict(counters),
            "histograms": {k: list(v) for k, v in histograms.items()},
        }


class MetricsRecorder:
    """Records request-level metrics with safe labels."""

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics

    def record_request(self, operation: str, duration_ms: float) -> None:
        self._metrics.inc("request_count")
        self._metrics.inc(f"operation_count:{operation}")
        self._metrics.observe("request_latency", duration_ms)
        self._metrics.observe(f"{operation.lower()}_latency", duration_ms)

    def record_error(self, error_type: str) -> None:
        self._metrics.inc("error_count")
        self._metrics.inc(f"error_count:{error_type}")

    def record_entities(self, count: int) -> None:
        self._metrics.inc("detected_entity_count", count)

    def record_payload_size(self, size: int) -> None:
        self._metrics.observe("payload_size", float(size))


class Timer:
    """Context manager measuring elapsed time in milliseconds."""

    __slots__ = ("_started", "elapsed_ms")

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self.elapsed_ms = 0.0

    def __enter__(self) -> Timer:
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._started) * 1000.0