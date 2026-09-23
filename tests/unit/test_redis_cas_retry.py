"""Redis CAS retry tests.

Verify that a ``WatchError`` on the first compare-and-set attempt is retried
without re-running the detector/masker, and that exhausting retries raises
``ServiceOverloadedError`` (429).
"""

from __future__ import annotations

import pytest
import redis

from app.core.enums import PIIType
from app.core.exceptions import ServiceOverloadedError
from app.core.models import DetectionContext, DetectionResult, PIIEntity
from app.detection.base import PIIDetector
from app.masking.base import MaskingStrategy
from app.masking.engine import MaskingResult
from app.policies.models import ConsumerPolicy
from app.processing.process_service import ProcessService
from app.restoration.redis_store import RedisRestorationStore

ORIGINAL = "Клиент Иванов, почта ivanov@mail.ru"
MASKING_KEY = "IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA="


class _Detector(PIIDetector):
    name = "test"

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
            ]
        )


class _Masker(MaskingStrategy):
    def __init__(self) -> None:
        self.calls = 0

    def mask(self, entity: PIIEntity) -> str:
        self.calls += 1
        return "***@***.***"


class _MaskingEngine:
    def __init__(self, masker: _Masker) -> None:
        self._masker = masker
        self.calls = 0

    def mask(self, text, entities, strategy_by_type, context_rules=None) -> MaskingResult:
        self.calls += 1
        mappings = []
        for entity in entities:
            replacement = self._masker.mask(entity)
            mappings.append((replacement, entity.value))
        return MaskingResult(
            masked_text="***@***.***",
            entities=entities,
            mappings=mappings,
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


class _FakePipeline:
    """A Redis pipeline that raises WatchError on the first execute()."""

    def __init__(self, fail_first: bool = True) -> None:
        self._fail_first = fail_first
        self._executed = 0
        self._watched = False
        self._value: bytes | None = None
        self._ops: list[tuple[str, object]] = []

    def __enter__(self) -> _FakePipeline:
        return self

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        return None

    def watch(self, key: str) -> None:  # type: ignore[no-untyped-def]
        self._watched = True

    def get(self, key: str) -> bytes | None:  # type: ignore[no-untyped-def]
        return self._value

    def multi(self) -> None:  # type: ignore[no-untyped-def]
        pass

    def set(self, key: str, value: str, ex: int) -> None:  # type: ignore[no-untyped-def]
        self._ops.append(("set", value))

    def delete(self, key: str) -> None:  # type: ignore[no-untyped-def]
        self._ops.append(("delete", key))

    def execute(self) -> list[object]:  # type: ignore[no-untyped-def]
        self._executed += 1
        if self._fail_first and self._executed == 1:
            raise redis.WatchError("simulated concurrent modification")
        # On success, store the value so a subsequent get returns it.
        for op, value in self._ops:
            if op == "set":
                self._value = value.encode("utf-8")
        return []


class _FakeRedis:
    """A fake Redis client whose pipeline fails once then succeeds."""

    def __init__(self, fail_first: bool = True) -> None:
        self._fail_first = fail_first
        self._pipeline = _FakePipeline(fail_first)

    def pipeline(self) -> _FakePipeline:
        return self._pipeline

    def get(self, key: str) -> bytes | None:  # type: ignore[no-untyped-def]
        return self._pipeline.get(key)

    def set(self, key: str, value: str, ex: int) -> None:  # type: ignore[no-untyped-def]
        self._pipeline.set(key, value, ex)

    def delete(self, key: str) -> None:  # type: ignore[no-untyped-def]
        self._pipeline.delete(key)


def _policy() -> ConsumerPolicy:
    return ConsumerPolicy(
        consumer_id="default",
        enabled=True,
        enabled_types=[PIIType.EMAIL],
        masking={PIIType.EMAIL: "FULL_MASK"},
        demasking_enabled=True,
    )


def test_cas_retry_runs_detector_once() -> None:
    """A WatchError on the first CAS attempt is retried without re-running
    the detector/masker, and the result is stored correctly."""
    detector = _Detector()
    masker = _Masker()
    masking = _MaskingEngine(masker)
    store = RedisRestorationStore(
        client=_FakeRedis(fail_first=True),
        ttl_seconds=3600,
        masking_key=MASKING_KEY,
    )
    service = ProcessService(
        detection_engine=detector,
        masking_engine=masking,
        restoration_store=store,
        metrics=_Metrics(),
    )
    outcome = service.process(ORIGINAL, "pid", _policy())
    assert outcome.operation.value == "MASK"
    assert outcome.result == "***@***.***"
    # Detector and masker run exactly once despite the CAS retry.
    assert detector.calls == 1
    assert masking.calls == 1
    assert masker.calls == 1


def test_cas_retry_preserves_stable_mask() -> None:
    """The mask produced on the first attempt is reused on the CAS retry, so
    the callback does not generate a new random mask."""
    detector = _Detector()
    masker = _Masker()
    masking = _MaskingEngine(masker)
    store = RedisRestorationStore(
        client=_FakeRedis(fail_first=True),
        ttl_seconds=3600,
        masking_key=MASKING_KEY,
    )
    service = ProcessService(
        detection_engine=detector,
        masking_engine=masking,
        restoration_store=store,
        metrics=_Metrics(),
    )
    first = service.process(ORIGINAL, "pid", _policy()).result
    # A retry MASK returns the same stable mask.
    retry = service.process(ORIGINAL, "pid", _policy()).result
    assert retry == first == "***@***.***"


def test_cas_retry_exhaustion_raises_429() -> None:
    """Exhausting CAS retries raises ServiceOverloadedError (429)."""
    detector = _Detector()
    masker = _Masker()
    masking = _MaskingEngine(masker)

    class _AlwaysFailPipeline(_FakePipeline):
        def execute(self) -> list[object]:  # type: ignore[no-untyped-def]
            raise redis.WatchError("always fails")

    class _AlwaysFailRedis(_FakeRedis):
        def pipeline(self) -> _FakePipeline:
            return _AlwaysFailPipeline(fail_first=True)

    store = RedisRestorationStore(
        client=_AlwaysFailRedis(fail_first=True),
        ttl_seconds=3600,
        masking_key=MASKING_KEY,
    )
    service = ProcessService(
        detection_engine=detector,
        masking_engine=masking,
        restoration_store=store,
        metrics=_Metrics(),
    )
    with pytest.raises(ServiceOverloadedError):
        service.process(ORIGINAL, "pid", _policy())