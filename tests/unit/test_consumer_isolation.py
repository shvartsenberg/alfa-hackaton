"""Consumer isolation tests.

Verify that one consumer cannot demask another consumer's state, that the
same payload_id can be used independently by different consumers, and that
consumer ids with special characters are handled safely.
"""

from __future__ import annotations

import pytest

from app.core.enums import PIIType
from app.core.exceptions import InvalidPayloadError
from app.core.models import DetectionContext, DetectionResult, PIIEntity
from app.detection.base import PIIDetector
from app.masking.base import MaskingStrategy
from app.masking.engine import MaskingResult
from app.policies.models import ConsumerPolicy
from app.processing.process_service import ProcessService
from app.restoration.memory import InMemoryRestorationStore

ORIGINAL = "Клиент Иванов, почта ivanov@mail.ru"
MASKING_KEY = "IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA="


class _Detector(PIIDetector):
    name = "test"

    def detect(self, text: str, context: DetectionContext) -> DetectionResult:
        start = text.find("ivanov@mail.ru")
        return DetectionResult(
            entities=[
                PIIEntity(
                    type=PIIType.EMAIL,
                    value="ivanov@mail.ru",
                    start=start,
                    end=start + len("ivanov@mail.ru"),
                    detector=self.name,
                )
            ]
        )


class _Masker(MaskingStrategy):
    def mask(self, entity: PIIEntity) -> str:
        return "***@***.***"


class _MaskingEngine:
    def mask(self, text, entities, strategy_by_type, context_rules=None) -> MaskingResult:
        return MaskingResult(
            masked_text="***@***.***",
            entities=entities,
            mappings=[("***@***.***", "ivanov@mail.ru")],
        )


class _Metrics:
    def record_request(self, *args, **kwargs) -> None:
        pass

    def record_entities(self, *args, **kwargs) -> None:
        pass

    def record_payload_size(self, *args, **kwargs) -> None:
        pass

    def record_stage(self, *args, **kwargs) -> None:
        pass


def _policy(consumer_id: str) -> ConsumerPolicy:
    return ConsumerPolicy(
        consumer_id=consumer_id,
        enabled=True,
        enabled_types=[PIIType.EMAIL],
        masking={PIIType.EMAIL: "FULL_MASK"},
        demasking_enabled=True,
    )


def _service() -> ProcessService:
    store = InMemoryRestorationStore(ttl_seconds=3600, masking_key=MASKING_KEY)
    return ProcessService(
        detection_engine=_Detector(),
        masking_engine=_MaskingEngine(),
        restoration_store=store,
        metrics=_Metrics(),
    )


def test_cross_consumer_demask_rejected() -> None:
    service = _service()
    masked = service.process(ORIGINAL, "pid", _policy("consumer-a")).result
    # Consumer B has no state for this payload_id (different storage key), so
    # its demask attempt is a new MASK, not a demask of A's original.
    outcome = service.process(masked, "pid", _policy("consumer-b"))
    assert outcome.operation.value == "MASK"
    assert outcome.result != ORIGINAL


def test_same_payload_id_independent_per_consumer() -> None:
    service = _service()
    masked_a = service.process(ORIGINAL, "pid", _policy("consumer-a")).result
    masked_b = service.process(ORIGINAL, "pid", _policy("consumer-b")).result
    # Both consumers can mask the same payload_id independently.
    assert masked_a == masked_b
    # Each demasks to the original under its own consumer.
    assert service.process(masked_a, "pid", _policy("consumer-a")).result == ORIGINAL
    assert service.process(masked_b, "pid", _policy("consumer-b")).result == ORIGINAL


def test_consumer_id_with_colon() -> None:
    service = _service()
    masked = service.process(ORIGINAL, "pid", _policy("a:b")).result
    assert service.process(masked, "pid", _policy("a:b")).result == ORIGINAL


def test_consumer_id_with_spaces_and_case() -> None:
    service = _service()
    masked = service.process(ORIGINAL, "pid", _policy("  Consumer A  ")).result
    assert service.process(masked, "pid", _policy("  Consumer A  ")).result == ORIGINAL


def test_unknown_consumer_has_no_state() -> None:
    service = _service()
    # A consumer that never masked has no state; a demask attempt is a new MASK.
    outcome = service.process(ORIGINAL, "pid", _policy("consumer-x"))
    assert outcome.operation.value == "MASK"


def test_old_record_without_consumer_id_handled_safely() -> None:
    """A legacy record without consumer_id must not be exposed to a consumer."""
    from app.core.enums import RestorationStateStatus
    from app.restoration.models import RestorationState

    store = InMemoryRestorationStore(ttl_seconds=3600, masking_key=MASKING_KEY)
    service = ProcessService(
        detection_engine=_Detector(),
        masking_engine=_MaskingEngine(),
        restoration_store=store,
        metrics=_Metrics(),
    )
    # Save a legacy record (empty consumer_id) at the key the service uses.
    key = service._state_key("consumer-a", "legacy")
    legacy = RestorationState(
        payload_id=key,
        original_hash="hash",
        original_text=ORIGINAL,
        masked_text="***@***.***",
        state=RestorationStateStatus.MASKED,
        consumer_id="",
    )
    store.save(key, legacy)
    # A consumer with a non-empty id must not be able to demask a legacy
    # record that has no consumer_id (it is treated as belonging to no one).
    with pytest.raises(InvalidPayloadError):
        service.process("***@***.***", "legacy", _policy("consumer-a"))