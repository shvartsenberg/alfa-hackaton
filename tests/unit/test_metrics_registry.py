"""Unit tests for the isolated Prometheus registry."""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from app.observability.metrics import Metrics, MetricsRecorder, render_prometheus


def _sample(
    metrics: Metrics,
    name: str,
    labels: dict[str, str] | None = None,
) -> float | None:
    return metrics.registry.get_sample_value(name, labels or {})


def test_histogram_is_cumulative_and_uses_seconds() -> None:
    metrics = Metrics(CollectorRegistry())
    recorder = MetricsRecorder(metrics)
    recorder.record_request("MASK", 5.0)
    recorder.record_request("MASK", 10.0)

    labels = {"operation": "MASK"}
    assert _sample(metrics, "pii_proxy_operation_duration_seconds_count", labels) == 2
    assert _sample(metrics, "pii_proxy_operation_duration_seconds_sum", labels) == 0.015


def test_operation_and_error_counters_accumulate() -> None:
    metrics = Metrics(CollectorRegistry())
    recorder = MetricsRecorder(metrics)
    recorder.record_request("MASK", 1.0)
    recorder.record_request("DEMASK", 2.0)
    recorder.record_error("INVALID_PAYLOAD")

    assert _sample(metrics, "pii_proxy_operations_total", {"operation": "MASK"}) == 1
    assert _sample(metrics, "pii_proxy_operations_total", {"operation": "DEMASK"}) == 1
    assert _sample(metrics, "pii_proxy_errors_total", {"error_type": "INVALID_PAYLOAD"}) == 1


def test_http_metrics_include_status_and_normalized_path() -> None:
    metrics = Metrics(CollectorRegistry())
    metrics.observe_http_request("POST", "/process", 422, 0.025)

    labels = {"method": "POST", "path": "/process", "status": "422"}
    assert _sample(metrics, "pii_proxy_http_requests_total", labels) == 1
    assert (
        _sample(
            metrics,
            "pii_proxy_http_request_duration_seconds_count",
            {"method": "POST", "path": "/process"},
        )
        == 1
    )


def test_payload_size_uses_dedicated_byte_buckets() -> None:
    metrics = Metrics(CollectorRegistry())
    metrics.record_payload_size(4096)
    assert _sample(metrics, "pii_proxy_payload_size_bytes_count") == 1
    assert _sample(metrics, "pii_proxy_payload_size_bytes_bucket", {"le": "4096.0"}) == 1


def test_stage_timings_recorded_per_stage() -> None:
    metrics = Metrics(CollectorRegistry())
    recorder = MetricsRecorder(metrics)
    recorder.record_stage("state_lookup", 1.0)
    recorder.record_stage("prepare", 5.0)
    recorder.record_stage("transition", 2.0)

    assert (
        _sample(
            metrics,
            "pii_proxy_stage_duration_seconds_count",
            {"stage": "state_lookup"},
        )
        == 1
    )
    assert (
        _sample(
            metrics,
            "pii_proxy_stage_duration_seconds_sum",
            {"stage": "prepare"},
        )
        == 0.005
    )
    assert (
        _sample(
            metrics,
            "pii_proxy_stage_duration_seconds_count",
            {"stage": "transition"},
        )
        == 1
    )


def test_render_prometheus_contains_standard_metric_names() -> None:
    metrics = Metrics(CollectorRegistry())
    metrics.observe_http_request("GET", "/health", 200, 0.001)
    text = render_prometheus(metrics).decode("utf-8")

    assert "pii_proxy_http_requests_total" in text
    assert "pii_proxy_http_request_duration_seconds_bucket" in text
    assert "pii_proxy_inflight_requests" in text
