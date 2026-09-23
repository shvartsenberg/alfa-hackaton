"""Integration tests for POST /process mask/demask lifecycle.

These tests exercise the full HTTP stack through TestClient. The ``client``
fixture uses ``raise_server_exceptions=False`` so 500 responses can be
asserted on directly.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api.dependencies import get_policy_provider, get_process_service
from app.core.enums import MaskingStrategy, PIIType
from app.core.exceptions import ConsumerNotAllowedError, ServiceOverloadedError
from app.main import app
from app.policies.loader import FilePolicyProvider
from app.policies.models import ConsumerPolicy

ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"
NO_PII = "Просто обычный текст без персональных данных."
UNICODE = "Привет, мир! Тест на кириллицу и юникод: café, naïve, 日本語."


def _mask(client: TestClient, payload_id: str, payload: str) -> str:
    resp = client.post("/process", json={"payload": payload, "payload_id": payload_id})
    assert resp.status_code == 200
    return resp.json()["result"]


def _post(client: TestClient, payload_id: str, payload: str):
    return client.post("/process", json={"payload": payload, "payload_id": payload_id})


# --- 1. Healthcheck ---------------------------------------------------------


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- 2. Successful mask -> demask -------------------------------------------


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


def test_mask_then_demask_roundtrip(client: TestClient, unique_payload_id: str) -> None:
    masked = _mask(client, unique_payload_id, ORIGINAL)
    assert masked != ORIGINAL
    assert "test@example.com" not in masked
    restored = _mask(client, unique_payload_id, masked)
    assert restored == ORIGINAL


# --- 3. Repeated MASK returns the same result -------------------------------


def test_mask_retry_is_idempotent(client: TestClient, unique_payload_id: str) -> None:
    first = _mask(client, unique_payload_id, ORIGINAL)
    second = _mask(client, unique_payload_id, ORIGINAL)
    assert first == second


# --- 4. Repeated DEMASK returns the original --------------------------------


def test_demask_retry_is_idempotent(client: TestClient, unique_payload_id: str) -> None:
    masked = _mask(client, unique_payload_id, ORIGINAL)
    first = _mask(client, unique_payload_id, masked)
    second = _mask(client, unique_payload_id, masked)
    assert first == ORIGINAL
    assert second == ORIGINAL


# --- 5. Multiple independent payload_ids ------------------------------------


def test_multiple_payload_ids_are_isolated(
    client: TestClient, unique_payload_id: str
) -> None:
    masked_a = _mask(client, unique_payload_id, ORIGINAL)
    masked_b = _mask(client, f"{unique_payload_id}-b", "Другой текст с phone +7 111 222-33-44")
    assert masked_a != masked_b
    assert _mask(client, unique_payload_id, masked_a) == ORIGINAL
    assert _mask(client, f"{unique_payload_id}-b", masked_b) == (
        "Другой текст с phone +7 111 222-33-44"
    )


# --- 6. Mismatched payload for an existing payload_id -----------------------


def test_mismatched_payload_returns_4xx(
    client: TestClient, unique_payload_id: str
) -> None:
    _mask(client, unique_payload_id, ORIGINAL)
    resp = _post(client, unique_payload_id, "совершенно другой текст")
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_PAYLOAD"


# --- 7. Empty / missing fields -> 422 ---------------------------------------


def test_empty_payload_returns_422(client: TestClient) -> None:
    resp = client.post("/process", json={"payload": "", "payload_id": "x"})
    assert resp.status_code == 422


def test_missing_fields_returns_422(client: TestClient) -> None:
    resp = client.post("/process", json={})
    assert resp.status_code == 422


def test_missing_payload_id_returns_422(client: TestClient) -> None:
    resp = client.post("/process", json={"payload": "text"})
    assert resp.status_code == 422


# --- 8. Unknown extra fields do not change the contract ---------------------


def test_extra_fields_are_ignored(client: TestClient, unique_payload_id: str) -> None:
    # Pydantic v2 ignores unknown fields by default; the request must still work.
    resp = client.post(
        "/process",
        json={
            "payload": ORIGINAL,
            "payload_id": unique_payload_id,
            "unexpected_field": "should be ignored",
        },
    )
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"result"}


# --- 9. Unicode / Russian text ----------------------------------------------


def test_unicode_text(client: TestClient, unique_payload_id: str) -> None:
    masked = _mask(client, unique_payload_id, UNICODE)
    assert masked == UNICODE  # no PII detected -> unchanged
    restored = _mask(client, unique_payload_id, masked)
    assert restored == UNICODE


# --- 10. Payload without PII ------------------------------------------------


def test_payload_without_pii(client: TestClient, unique_payload_id: str) -> None:
    masked = _mask(client, unique_payload_id, NO_PII)
    assert masked == NO_PII


# --- 11. Large payload (no chunking needed) ---------------------------------


def test_large_payload(client: TestClient, unique_payload_id: str) -> None:
    # ~10k chars, well below the 100k-token chunking threshold.
    large = ("Слово " * 2000) + " test@example.com"
    masked = _mask(client, unique_payload_id, large)
    assert "test@example.com" not in masked
    restored = _mask(client, unique_payload_id, masked)
    assert restored == large


# --- 12. 429 + Retry-After via fake ProcessService --------------------------


class _OverloadedProcessService:
    def process(self, payload: str, payload_id: str, policy: ConsumerPolicy):
        raise ServiceOverloadedError("too many requests", retry_after=3)


def test_service_overloaded_returns_429_with_retry_after(
    client: TestClient, unique_payload_id: str
) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_process_service] = lambda: _OverloadedProcessService()
    try:
        resp = _post(client, unique_payload_id, ORIGINAL)
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") == "3"
        body = resp.json()
        assert body["error"] == "SERVICE_OVERLOADED"
        assert "too many requests" in body["message"]
    finally:
        app.dependency_overrides.clear()


# --- 13. Unexpected exception -> safe 500 -----------------------------------


class _ExplodingProcessService:
    def process(self, payload: str, payload_id: str, policy: ConsumerPolicy):
        raise RuntimeError("secret-pii-value-12345")


def test_unexpected_exception_returns_safe_500(
    client: TestClient, unique_payload_id: str
) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_process_service] = lambda: _ExplodingProcessService()
    try:
        resp = _post(client, unique_payload_id, ORIGINAL)
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "INTERNAL_ERROR"
        # No exception text, no PII, no stack trace leaks to the client.
        assert "secret-pii-value-12345" not in resp.text
        assert "Traceback" not in resp.text
        assert "RuntimeError" not in resp.text
    finally:
        app.dependency_overrides.clear()


# --- 14. Disabled consumer --------------------------------------------------


class _DisabledPolicyProvider:
    def get_policy(self, consumer_id: str) -> ConsumerPolicy:
        raise ConsumerNotAllowedError(f"consumer not allowed: {consumer_id}")


def test_disabled_consumer_returns_403(
    client: TestClient, unique_payload_id: str
) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_policy_provider] = lambda: _DisabledPolicyProvider()
    try:
        resp = _post(client, unique_payload_id, ORIGINAL)
        assert resp.status_code == 403
        assert resp.json()["error"] == "CONSUMER_NOT_ALLOWED"
    finally:
        app.dependency_overrides.clear()


# --- 15. Demasking disabled -------------------------------------------------


class _NoDemaskPolicyProvider:
    def get_policy(self, consumer_id: str) -> ConsumerPolicy:
        return ConsumerPolicy(
            consumer_id=consumer_id,
            enabled=True,
            enabled_types=[PIIType.EMAIL, PIIType.PHONE],
            masking={
                PIIType.EMAIL: MaskingStrategy.FULL_MASK,
                PIIType.PHONE: MaskingStrategy.PARTIAL_MASK,
            },
            demasking_enabled=False,
        )


def test_demasking_disabled_returns_500(
    client: TestClient, unique_payload_id: str
) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_policy_provider] = lambda: _NoDemaskPolicyProvider()
    try:
        masked = _mask(client, unique_payload_id, ORIGINAL)
        resp = _post(client, unique_payload_id, masked)
        assert resp.status_code == 500
        assert resp.json()["error"] == "PROCESSING_ERROR"
    finally:
        app.dependency_overrides.clear()


# --- 18. Behaviour after DEMASK (contract question) --------------------------


def test_original_payload_after_demask_is_rejected(
    client: TestClient, unique_payload_id: str
) -> None:
    """Documents current behaviour: after DEMASK, the original payload is rejected.

    This is a contract question for the Core owner (see docs/qa-report.md).
    The test pins the current semantics so any future change is explicit.
    """
    masked = _mask(client, unique_payload_id, ORIGINAL)
    restored = _mask(client, unique_payload_id, masked)
    assert restored == ORIGINAL

    # After DEMASK, sending the original payload again is rejected.
    resp = _post(client, unique_payload_id, ORIGINAL)
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_PAYLOAD"

# --- 19. Consumer selection via X-Consumer-ID header -------------------------


def test_default_consumer_used_without_header(client: TestClient, unique_payload_id: str) -> None:
    """Without a header, the default consumer (FULL_MASK for everything) applies."""
    resp = client.post(
        "/process",
        json={"payload": "test@example.com", "payload_id": unique_payload_id},
    )
    assert resp.status_code == 200
    # default.yaml uses FULL_MASK for EMAIL -> all alnum masked.
    assert "test@example.com" not in resp.json()["result"]


def test_demo_consumer_selected_via_header(client: TestClient, unique_payload_id: str) -> None:
    """demo.yaml uses PARTIAL_MASK for EMAIL -> keeps first char + domain."""
    resp = client.post(
        "/process",
        json={"payload": "test@example.com", "payload_id": unique_payload_id},
        headers={"X-Consumer-ID": "demo"},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert "test@example.com" not in result
    assert "@example.com" in result  # PARTIAL_MASK keeps the domain


def test_unknown_consumer_rejected(client: TestClient, unique_payload_id: str) -> None:
    resp = client.post(
        "/process",
        json={"payload": "test@example.com", "payload_id": unique_payload_id},
        headers={"X-Consumer-ID": "does-not-exist"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "CONSUMER_NOT_ALLOWED"


def test_consumer_header_is_normalized(client: TestClient, unique_payload_id: str) -> None:
    """Header value is trimmed and lowercased before use."""
    resp = client.post(
        "/process",
        json={"payload": "test@example.com", "payload_id": unique_payload_id},
        headers={"X-Consumer-ID": "  DEMO  "},
    )
    assert resp.status_code == 200
    assert "@example.com" in resp.json()["result"]


# --- 20. Consumer context rules applied via HTTP -----------------------------


def test_context_rule_applied_via_http(
    client: TestClient, unique_payload_id: str, tmp_path: Path
) -> None:
    """PIN is masked only when a BANK_CARD is present in the same text.

    The rule is loaded from a consumer YAML and must be applied end-to-end
    through POST /process, not just at the masking-engine level.
    """
    consumer = tmp_path / "pinrule.yaml"
    consumer.write_text(
        "enabled: true\n"
        "detection:\n"
        "  enabled_types:\n"
        "    - BANK_CARD\n"
        "    - PIN\n"
        "masking:\n"
        "  BANK_CARD: FULL_MASK\n"
        "  PIN: FULL_MASK\n"
        "demasking:\n"
        "  enabled: true\n"
        "context_rules:\n"
        "  - type: PIN\n"
        "    requires:\n"
        "      - BANK_CARD\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    provider = FilePolicyProvider(tmp_path)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_policy_provider] = lambda: provider
    try:
        # PIN without a card -> stays in the clear.
        resp = client.post(
            "/process",
            json={"payload": "Мой пин 1234", "payload_id": unique_payload_id},
            headers={"X-Consumer-ID": "pinrule"},
        )
        assert resp.status_code == 200
        assert "1234" in resp.json()["result"]

        # PIN with a card -> masked.
        resp2 = client.post(
            "/process",
            json={
                "payload": "Карта 4111111111111111 пин 1234",
                "payload_id": f"{unique_payload_id}-card",
            },
            headers={"X-Consumer-ID": "pinrule"},
        )
        assert resp2.status_code == 200
        result = resp2.json()["result"]
        assert "1234" not in result
        assert "4111111111111111" not in result
    finally:
        app.dependency_overrides.clear()


# --- 21. Consumer-specific masking behaviour ---------------------------------


def _mask_as(client: TestClient, consumer: str, payload_id: str, payload: str) -> str:
    resp = client.post(
        "/process",
        json={"payload": payload, "payload_id": payload_id},
        headers={"X-Consumer-ID": consumer},
    )
    assert resp.status_code == 200
    return resp.json()["result"]


def test_default_and_demo_produce_different_masks(
    client: TestClient, unique_payload_id: str
) -> None:
    """Same payload masks differently under default (FULL_MASK) vs demo (PARTIAL_MASK)."""
    payload = "email test@example.com, карта 4111 1111 1111 1111"
    default_masked = _mask_as(client, "default", unique_payload_id, payload)
    demo_masked = _mask_as(client, "demo", f"{unique_payload_id}-demo", payload)
    assert default_masked != demo_masked
    # demo: EMAIL keeps the domain, BANK_CARD keeps the first/last two digits.
    assert "@example.com" in demo_masked
    assert "41" in demo_masked
    assert "11" in demo_masked
    assert "4111 1111 1111 1111" not in demo_masked
    # default: FULL_MASK hides the domain and the whole card.
    assert "@example.com" not in default_masked
    assert "4111 1111 1111 1111" not in default_masked


def test_type_not_in_enabled_types_stays_open(
    client: TestClient, unique_payload_id: str
) -> None:
    """PASSPORT_NUMBER is not in demo's enabled_types -> stays in the clear."""
    payload = "паспорт 1234 567890, email test@example.com"
    result = _mask_as(client, "demo", unique_payload_id, payload)
    assert "1234 567890" in result  # PASSPORT_NUMBER not masked under demo
    assert "@example.com" in result  # EMAIL is PARTIAL_MASK -> domain kept


def test_demo_roundtrip_returns_original(
    client: TestClient, unique_payload_id: str
) -> None:
    """mask -> demask under demo returns the original text byte-for-byte."""
    payload = "email test@example.com, карта 4111 1111 1111 1111"
    masked = _mask_as(client, "demo", unique_payload_id, payload)
    assert masked != payload
    restored = _mask_as(client, "demo", unique_payload_id, masked)
    assert restored == payload


def test_no_pii_masks_to_itself_and_retry_returns_200(
    client: TestClient, unique_payload_id: str
) -> None:
    """Text without PII masks to itself; a retry with the same payload_id is 200."""
    resp1 = client.post(
        "/process",
        json={"payload": NO_PII, "payload_id": unique_payload_id},
    )
    assert resp1.status_code == 200
    assert resp1.json()["result"] == NO_PII
    resp2 = client.post(
        "/process",
        json={"payload": NO_PII, "payload_id": unique_payload_id},
    )
    assert resp2.status_code == 200
    assert resp2.json()["result"] == NO_PII


def test_same_payload_masks_identically_across_payload_ids(
    client: TestClient, unique_payload_id: str
) -> None:
    """Masking is deterministic: the same payload under two payload_ids matches."""
    payload = "email test@example.com, карта 4111 1111 1111 1111"
    first = _mask(client, unique_payload_id, payload)
    second = _mask(client, f"{unique_payload_id}-b", payload)
    assert first == second


def test_different_payload_under_used_payload_id_returns_4xx(
    client: TestClient, unique_payload_id: str
) -> None:
    """A different payload under an already-used payload_id is rejected, not 200."""
    _mask(client, unique_payload_id, ORIGINAL)
    resp = _post(client, unique_payload_id, "совершенно другой текст")
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_PAYLOAD"


# --- 22. Restoration state is scoped by consumer -----------------------------


def test_foreign_consumer_cannot_demask_another_consumers_mask(
    client: TestClient, unique_payload_id: str
) -> None:
    """Consumer B cannot demask a mask produced by consumer A for the same payload_id."""
    masked = _mask_as(client, "demo", unique_payload_id, ORIGINAL)
    assert masked != ORIGINAL
    # B has no state under its own key, so the masked text is treated as a fresh
    # payload to mask, not demasked back to the original.
    resp = client.post(
        "/process",
        json={"payload": masked, "payload_id": unique_payload_id},
        headers={"X-Consumer-ID": "default"},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] != ORIGINAL


def test_consumers_share_payload_id_independently(
    client: TestClient, unique_payload_id: str
) -> None:
    """Two consumers can reuse one payload_id without a false 422."""
    masked_a = _mask_as(client, "demo", unique_payload_id, ORIGINAL)
    restored_a = _mask_as(client, "demo", unique_payload_id, masked_a)
    assert restored_a == ORIGINAL

    masked_b = _mask_as(client, "default", unique_payload_id, ORIGINAL)
    restored_b = _mask_as(client, "default", unique_payload_id, masked_b)
    assert restored_b == ORIGINAL
