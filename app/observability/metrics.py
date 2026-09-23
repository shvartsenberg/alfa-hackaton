"""Prometheus observability primitives with bounded-cardinality labels.

No payload content, request identifiers, or consumer-provided values are used
as metric labels. Each ``Metrics`` instance owns a collector registry, which
keeps tests isolated and avoids duplicate collector registration.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

HTTP_LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
PAYLOAD_SIZE_BUCKETS = (64, 256, 1024, 4096, 16_384, 65_536, 262_144, 1_048_576)


class Metrics:
    """Application metrics backed by an isolated Prometheus registry."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "pii_proxy_http_requests_total",
            "HTTP requests completed by the proxy.",
            ("method", "path", "status"),
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            "pii_proxy_http_request_duration_seconds",
            "End-to-end HTTP request duration in seconds.",
            ("method", "path"),
            buckets=HTTP_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.inflight_requests = Gauge(
            "pii_proxy_inflight_requests",
            "HTTP requests currently being processed.",
            registry=self.registry,
        )
        self.operations = Counter(
            "pii_proxy_operations_total",
            "Successful domain operations.",
            ("operation",),
            registry=self.registry,
        )
        self.operation_duration = Histogram(
            "pii_proxy_operation_duration_seconds",
            "Domain operation duration in seconds.",
            ("operation",),
            buckets=HTTP_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.errors = Counter(
            "pii_proxy_errors_total",
            "Handled domain and internal errors.",
            ("error_type",),
            registry=self.registry,
        )
        self.detected_entities = Counter(
            "pii_proxy_detected_entities_total",
            "PII entities detected in successful operations.",
            registry=self.registry,
        )
        self.payload_size = Histogram(
            "pii_proxy_payload_size_bytes",
            "Incoming payload size in UTF-8 bytes.",
            buckets=PAYLOAD_SIZE_BUCKETS,
            registry=self.registry,
        )
        self.stage_duration = Histogram(
            "pii_proxy_stage_duration_seconds",
            "Duration of a processing stage (state_lookup, prepare, transition).",
            ("stage",),
            buckets=HTTP_LATENCY_BUCKETS,
            registry=self.registry,
        )

    @staticmethod
    def _safe(operation: object, method: str, *args: object, **kwargs: object) -> None:
        """Keep observability failures out of the business request path."""
        try:
            getattr(operation, method)(*args, **kwargs)
        except Exception:
            # Metrics must never turn a valid request or an error handler into 500.
            return

    @contextmanager
    def track_inflight(self) -> Iterator[None]:
        self._safe(self.inflight_requests, "inc")
        try:
            yield
        finally:
            self._safe(self.inflight_requests, "dec")

    def observe_http_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        status = str(status_code)
        self._safe(self.http_requests.labels(method=method, path=path, status=status), "inc")
        self._safe(
            self.http_request_duration.labels(method=method, path=path),
            "observe",
            duration_seconds,
        )

    def record_operation(self, operation: str, duration_seconds: float) -> None:
        self._safe(self.operations.labels(operation=operation), "inc")
        self._safe(
            self.operation_duration.labels(operation=operation),
            "observe",
            duration_seconds,
        )

    def record_error(self, error_type: str) -> None:
        self._safe(self.errors.labels(error_type=error_type), "inc")

    def record_entities(self, count: int) -> None:
        self._safe(self.detected_entities, "inc", count)

    def record_payload_size(self, size_bytes: int) -> None:
        self._safe(self.payload_size, "observe", size_bytes)

    def record_stage(self, stage: str, duration_seconds: float) -> None:
        self._safe(
            self.stage_duration.labels(stage=stage),
            "observe",
            duration_seconds,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


class MetricsRecorder:
    """Compatibility facade used by the domain service and error handlers."""

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics

    def record_request(self, operation: str, duration_ms: float) -> None:
        self._metrics.record_operation(operation, duration_ms / 1000.0)

    def record_error(self, error_type: str) -> None:
        self._metrics.record_error(error_type)

    def record_entities(self, count: int) -> None:
        self._metrics.record_entities(count)

    def record_payload_size(self, size_bytes: int) -> None:
        self._metrics.record_payload_size(size_bytes)

    def record_stage(self, stage: str, duration_ms: float) -> None:
        self._metrics.record_stage(stage, duration_ms / 1000.0)


class Timer:
    """Context manager measuring elapsed time in milliseconds."""

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self.elapsed_ms = 0.0

    def __enter__(self) -> Timer:
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._started) * 1000.0


def render_prometheus(metrics: Metrics) -> bytes:
    """Return the registry in the Prometheus text exposition format."""
    return metrics.render()


__all__ = [
    "HTTP_LATENCY_BUCKETS",
    "PAYLOAD_SIZE_BUCKETS",
    "Metrics",
    "MetricsRecorder",
    "Timer",
    "render_prometheus",
]
