"""Ensure no PII leaks into logs or /metrics across a full mask/demask cycle."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.observability.logging import JsonFormatter

# FIO, passport, card and email in one payload.
ORIGINAL = (
    "Клиент Иванов Иван Иванович, паспорт 4509 123456, "
    "карта 4567 8901 2345 6756, email test@example.com"
)
SECRETS = (
    "Иванов Иван Иванович",
    "4509 123456",
    "4567 8901 2345 6756",
    "test@example.com",
)


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


def test_no_pii_in_logs_or_metrics_across_mask_demask(
    client: TestClient, unique_payload_id: str
) -> None:
    with capture_application_logs() as stream:
        masked_resp = client.post(
            "/process",
            json={"payload": ORIGINAL, "payload_id": unique_payload_id},
        )
        assert masked_resp.status_code == 200
        masked = masked_resp.json()["result"]

        demask_resp = client.post(
            "/process",
            json={"payload": masked, "payload_id": unique_payload_id},
        )
        assert demask_resp.status_code == 200
        assert demask_resp.json()["result"] == ORIGINAL

        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200

    logs = stream.getvalue()
    metrics = metrics_resp.text
    combined = logs + metrics

    for secret in SECRETS:
        assert secret not in combined
    assert unique_payload_id not in combined

    # Logs are valid JSON lines and carry only the hashed identifier.
    for line in logs.splitlines():
        if not line:
            continue
        record = json.loads(line)
        assert "payload_id_hash" not in record or record["payload_id_hash"] != unique_payload_id