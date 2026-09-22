"""Minimal metrics instrumentation layer.

A lightweight abstraction so the hot path can record counters and timings
without depending on a full Prometheus client. Swap in a real metrics backend
later without changing business code. Never put payload content in labels.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(slots=True)
class Metrics:
    """In-memory metrics registry."""

    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    histograms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self.counters[name] += value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self.histograms[name].append(value)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "counters": dict(self.counters),
                "histograms": {k: list(v) for k, v in self.histograms.items()},
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

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self.elapsed_ms = 0.0

    def __enter__(self) -> Timer:
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._started) * 1000.0