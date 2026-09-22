"""Integration tests for the GET /metrics endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_metrics_endpoint_returns_prometheus_text(
    client: TestClient, unique_payload_id: str
) -> None:
    # Generate some traffic so counters/histograms are populated.
    client.post(
        "/process",
        json={"payload": "test@example.com", "payload_id": unique_payload_id},
    )

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "pii_proxy_http_requests_total" in body
    assert 'path="/process",status="200"' in body
    assert "pii_proxy_operation_duration_seconds_bucket" in body


def test_metrics_endpoint_does_not_leak_payload(
    client: TestClient, unique_payload_id: str
) -> None:
    client.post(
        "/process",
        json={"payload": "secret@example.com", "payload_id": unique_payload_id},
    )
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "secret@example.com" not in resp.text
    assert unique_payload_id not in resp.text


def test_validation_errors_are_counted_by_http_status(client: TestClient) -> None:
    response = client.post("/process", json={"payload": ""})
    assert response.status_code == 422

    metrics = client.get("/metrics").text
    assert 'method="POST",path="/process",status="422"' in metrics
