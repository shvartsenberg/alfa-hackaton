"""Integration tests for POST /process mask/demask lifecycle."""

from __future__ import annotations

from fastapi.testclient import TestClient

ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"


def _mask(client: TestClient, payload_id: str, payload: str) -> str:
    resp = client.post("/process", json={"payload": payload, "payload_id": payload_id})
    assert resp.status_code == 200
    return resp.json()["result"]


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readiness(client: TestClient) -> None:
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_response_has_request_id_header(client: TestClient) -> None:
    resp = client.post(
        "/process", json={"payload": ORIGINAL, "payload_id": "reqid-1"}
    )
    assert resp.status_code == 200
    assert "X-Request-ID" in resp.headers


def test_mask_then_demask_roundtrip(client: TestClient) -> None:
    masked = _mask(client, "roundtrip-1", ORIGINAL)
    assert masked != ORIGINAL
    assert "test@example.com" not in masked
    restored = _mask(client, "roundtrip-1", masked)
    assert restored == ORIGINAL


def test_mask_retry_is_idempotent(client: TestClient) -> None:
    first = _mask(client, "retry-mask", ORIGINAL)
    second = _mask(client, "retry-mask", ORIGINAL)
    assert first == second


def test_demask_retry_is_idempotent(client: TestClient) -> None:
    masked = _mask(client, "retry-demask", ORIGINAL)
    first = _mask(client, "retry-demask", masked)
    second = _mask(client, "retry-demask", masked)
    assert first == ORIGINAL
    assert second == ORIGINAL


def test_multiple_payload_ids_are_isolated(client: TestClient) -> None:
    masked_a = _mask(client, "iso-a", ORIGINAL)
    masked_b = _mask(client, "iso-b", "Другой текст с phone +7 111 222-33-44")
    assert masked_a != masked_b
    assert _mask(client, "iso-a", masked_a) == ORIGINAL
    assert _mask(client, "iso-b", masked_b) == "Другой текст с phone +7 111 222-33-44"


def test_invalid_request_returns_422(client: TestClient) -> None:
    resp = client.post("/process", json={"payload": ""})
    assert resp.status_code == 422


def test_original_mask_demask_original(client: TestClient) -> None:
    masked = _mask(client, "cycle", ORIGINAL)
    restored = _mask(client, "cycle", masked)
    assert restored == ORIGINAL