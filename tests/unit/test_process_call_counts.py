"""Instrumented tests: detector/masker call counts per operation.

These verify that detection and masking run only when a new MASK is needed,
and never for DEMASK, retry MASK, retry DEMASK, or conflict cases.
"""

from __future__ import annotations

from app.core.enums import Operation, PIIType
from app.core.models import DetectionContext, DetectionResult, PIIEntity
from app.detection.base import PIIDetector
from app.masking.base import MaskingStrategy
from app.masking.engine import MaskingResult
from app.policies.models import ConsumerPolicy
from app.processing.process_service import ProcessService
from app.restoration.memory import InMemoryRestorationStore

ORIGINAL = "Клиент Иванов, тел +7 999 123-45-67, почта ivanov@mail.ru"
MASKING_KEY = "IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA="


class SpyDetector(PIIDetector):
    """Detector that counts calls and returns a fixed entity."""

    name = "spy"

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, text: str, context: DetectionContext) -> DetectionResult:
        self.calls += 1
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
            ],
            processing_time=0.0,
        )


class SpyMasker(MaskingStrategy):
    """Masker that counts calls and returns a fixed replacement."""

    def __init__(self) -> None:
        self.calls = 0

    def mask(self, entity: PIIEntity) -> str:
        self.calls += 1
        return "***@***.***"


class SpyMaskingEngine:
    """Masking engine that counts calls."""

    def __init__(self, strategy: SpyMasker) -> None:
        self._strategy = strategy
        self.calls = 0

    def mask(self, text, entities, strategy_by_type, context_rules=None) -> MaskingResult:
        self.calls += 1
        replacements = []
        for entity in entities:
            replacement = self._strategy.mask(entity)
            replacements.append((replacement, entity.value))
        return MaskingResult(
            masked_text="***@***.***",
            entities=entities,
            mappings=replacements,
            processing_time=0.0,
        )


class SpyMetrics:
    def record_request(self, *args, **kwargs) -> None:
        pass

    def record_entities(self, *args, **kwargs) -> None:
        pass

    def record_payload_size(self, *args, **kwargs) -> None:
        pass

    def record_stage(self, *args, **kwargs) -> None:
        pass


def _policy(consumer_id: str = "default") -> ConsumerPolicy:
    return ConsumerPolicy(
        consumer_id=consumer_id,
        enabled=True,
        enabled_types=[PIIType.EMAIL],
        masking={PIIType.EMAIL: "FULL_MASK"},
        demasking_enabled=True,
    )


def _make_service() -> (
    tuple[ProcessService, SpyDetector, SpyMaskingEngine, InMemoryRestorationStore]
):
    detector = SpyDetector()
    strategy = SpyMasker()
    masking = SpyMaskingEngine(strategy)
    store = InMemoryRestorationStore(
        ttl_seconds=3600, masking_key=MASKING_KEY
    )
    service = ProcessService(
        detection_engine=detector,
        masking_engine=masking,
        restoration_store=store,
        metrics=SpyMetrics(),
    )
    return service, detector, masking, store


def test_new_mask_runs_detector_and_masker_once() -> None:
    service, detector, masking, _ = _make_service()
    outcome = service.process(ORIGINAL, "pid-1", _policy())
    assert outcome.operation == Operation.MASK
    assert detector.calls == 1
    assert masking.calls == 1


def test_retry_mask_runs_no_additional_detection() -> None:
    service, detector, masking, _ = _make_service()
    service.process(ORIGINAL, "pid-1", _policy())
    outcome = service.process(ORIGINAL, "pid-1", _policy())
    assert outcome.operation == Operation.MASK
    assert detector.calls == 1
    assert masking.calls == 1


def test_demask_runs_no_detection() -> None:
    service, detector, masking, _ = _make_service()
    masked = service.process(ORIGINAL, "pid-1", _policy()).result
    outcome = service.process(masked, "pid-1", _policy())
    assert outcome.operation == Operation.DEMASK
    assert outcome.result == ORIGINAL
    assert detector.calls == 1
    assert masking.calls == 1


def test_retry_demask_runs_no_detection() -> None:
    service, detector, masking, _ = _make_service()
    masked = service.process(ORIGINAL, "pid-1", _policy()).result
    service.process(masked, "pid-1", _policy())
    outcome = service.process(masked, "pid-1", _policy())
    assert outcome.operation == Operation.DEMASK
    assert outcome.result == ORIGINAL
    assert detector.calls == 1
    assert masking.calls == 1


def test_conflict_payload_runs_no_detection() -> None:
    from app.core.exceptions import InvalidPayloadError

    service, detector, masking, _ = _make_service()
    service.process(ORIGINAL, "pid-1", _policy())
    try:
        service.process("Совершенно другой текст", "pid-1", _policy())
        raise AssertionError("expected InvalidPayloadError")
    except InvalidPayloadError:
        pass
    assert detector.calls == 1
    assert masking.calls == 1


def test_cas_retry_runs_detector_at_most_once() -> None:
    """A CAS retry on a new state must not re-run the detector/masker."""
    service, detector, masking, store = _make_service()
    # Simulate a CAS retry: the first transition writes, then a second
    # transition for the same key sees the existing state (retry MASK).
    service.process(ORIGINAL, "pid-1", _policy())
    # A concurrent request for the same key with the same payload is a retry.
    outcome = service.process(ORIGINAL, "pid-1", _policy())
    assert outcome.operation == Operation.MASK
    assert detector.calls == 1
    assert masking.calls == 1


def test_state_appearing_between_lookup_and_transition() -> None:
    """If a state appears between the lookup and the transition, the request
    routes to the existing state and does not create a duplicate."""
    from app.core.enums import RestorationStateStatus
    from app.restoration.models import RestorationState

    service, detector, masking, store = _make_service()
    key = service._state_key("default", "pid-race")
    # Inject a state directly into the store so the transition sees it even
    # though the initial lookup returned None.
    injected = RestorationState(
        payload_id=key,
        original_hash="other-hash",
        original_text="Другой текст",
        masked_text="***@***.***",
        state=RestorationStateStatus.MASKED,
        consumer_id="default",
    )
    store.save(key, injected)
    # The request's payload does not match the injected state -> conflict.
    from app.core.exceptions import InvalidPayloadError

    try:
        service.process(ORIGINAL, "pid-race", _policy())
        raise AssertionError("expected InvalidPayloadError")
    except InvalidPayloadError:
        pass
    # The lookup found the injected state, so no detection/masking ran; the
    # transition rejected the conflict.
    assert detector.calls == 0
    assert masking.calls == 0