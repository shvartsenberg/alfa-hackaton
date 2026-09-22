"""Security tests for hashing, structured logs, and safe error responses."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.api.dependencies import get_process_service
from app.core.security import hash_identifier, hash_payload
from app.main import app
from app.observability.logging import JsonFormatter


@contextmanager
def capture_application_logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    application_logger = logging.getLogger("pii_security_proxy")
    old_handlers = application_logger.handlers[:]
    old_propagate = application_logger.propagate
    application_logger.handlers = [handler]
    application_logger.propagate = False
    try:
        yield stream
    finally:
        application_logger.handlers = old_handlers
        application_logger.propagate = old_propagate


def parsed_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_hash_identifier_is_stable_and_short() -> None:
    assert hash_identifier("payload-123") == hash_identifier("payload-123")
    assert len(hash_identifier("payload-123")) == 12
    assert hash_identifier("a") != hash_identifier("b")


def test_hash_payload_is_full_sha256() -> None:
    assert len(hash_payload("secret text")) == 64


def test_success_logs_are_structured_and_do_not_leak_pii(
    client: TestClient,
    unique_payload_id: str,
) -> None:
    payload = "SECRET-SENTINEL test@example.com +7 999 123-45-67"
    with capture_application_logs() as stream:
        response = client.post(
            "/process",
            json={"payload": payload, "payload_id": unique_payload_id},
            headers={"X-Request-ID": "safe-request-1"},
        )

    assert response.status_code == 200
    raw_logs = stream.getvalue()
    for secret in (payload, "SECRET-SENTINEL", "test@example.com", "+7 999 123-45-67"):
        assert secret not in raw_logs
    assert unique_payload_id not in raw_logs
    assert hash_identifier(unique_payload_id) in raw_logs

    records = parsed_lines(stream)
    process_record = next(record for record in records if record["event"] == "process_completed")
    assert process_record["request_id"] == "safe-request-1"
    assert process_record["operation"] == "MASK"
    assert isinstance(process_record["duration_ms"], float)
    http_record = next(record for record in records if record["event"] == "http_request_completed")
    assert http_record["status_code"] == 200
    assert http_record["path"] == "/process"


class _ExplodingProcessService:
    def process(self, payload: str, payload_id: str, policy: object) -> None:
        raise RuntimeError("SECRET-EXCEPTION test@example.com")


def test_api_500_does_not_leak_pii_to_response_or_logs(
    client: TestClient,
    unique_payload_id: str,
) -> None:
    app.dependency_overrides[get_process_service] = lambda: _ExplodingProcessService()
    with capture_application_logs() as stream:
        response = client.post(
            "/process",
            json={"payload": "test@example.com", "payload_id": unique_payload_id},
            headers={"X-Request-ID": "safe-request-500"},
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "safe-request-500"
    assert response.json() == {"error": "INTERNAL_ERROR", "message": "internal error"}
    combined = response.text + stream.getvalue()
    for secret in ("SECRET-EXCEPTION", "test@example.com", unique_payload_id, "Traceback"):
        assert secret not in combined

    error_record = next(
        record for record in parsed_lines(stream) if record["event"] == "unexpected_error"
    )
    assert error_record["exception_type"] == "RuntimeError"
    assert error_record["request_id"] == "safe-request-500"


def test_untrusted_request_id_is_replaced(client: TestClient, unique_payload_id: str) -> None:
    response = client.post(
        "/process",
        json={"payload": "ordinary text", "payload_id": unique_payload_id},
        headers={"X-Request-ID": "bad request id with spaces"},
    )
    request_id = response.headers["X-Request-ID"]
    assert request_id != "bad request id with spaces"
    assert len(request_id) == 36
