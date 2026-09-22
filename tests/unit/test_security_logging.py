"""Unit tests for safe logging and hashing utilities."""

from __future__ import annotations

import logging

from app.core.security import hash_identifier, hash_payload


def test_hash_identifier_is_stable_and_short() -> None:
    h1 = hash_identifier("payload-123")
    h2 = hash_identifier("payload-123")
    assert h1 == h2
    assert len(h1) == 12


def test_hash_identifier_differs_for_different_inputs() -> None:
    assert hash_identifier("a") != hash_identifier("b")


def test_hash_payload_is_stable_and_fixed_length() -> None:
    digest = hash_payload("secret text")
    assert len(digest) == 32
    assert hash_payload("secret text") == digest
    assert hash_payload("other text") != digest


def test_logger_does_not_emit_payload(caplog) -> None:
    logger = logging.getLogger("pii_security_proxy.test")
    with caplog.at_level(logging.INFO):
        logger.info("processed entity_count=%s", 2)
    assert "secret" not in caplog.text
    assert "entity_count=2" in caplog.text