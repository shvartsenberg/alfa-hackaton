"""ProcessService: orchestrates mask/demask with explicit idempotency.

The operation (MASK vs DEMASK) is decided from the stored RestorationState,
not from a primitive "payload_id exists" check. This makes retries safe:

- First request with a new payload_id -> MASK, store state.
- Retry of the same original payload -> same MASKED result (idempotent).
- Request with the masked payload -> DEMASK, return original.
- Retry of the masked payload -> same ORIGINAL result (idempotent).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.core.enums import Operation, RestorationStateStatus
from app.core.exceptions import (
    InvalidPayloadError,
    ProcessingError,
)
from app.core.models import DetectionContext
from app.core.security import hash_payload
from app.detection.engine import DetectionEngine
from app.masking.engine import MaskingEngine, MaskingResult
from app.observability.metrics import MetricsRecorder, Timer
from app.policies.models import ConsumerPolicy
from app.restoration.base import RestorationStore
from app.restoration.models import RestorationState

_PAYLOAD_LOCK_STRIPES = 256


@dataclass(slots=True)
class ProcessOutcome:
    """Result of a /process call."""

    result: str
    operation: Operation
    entity_count: int
    detected_types: list[str]
    duration_ms: float


class ProcessService:
    """Handles the mask/demask lifecycle for a payload_id."""

    def __init__(
        self,
        detection_engine: DetectionEngine,
        masking_engine: MaskingEngine,
        restoration_store: RestorationStore,
        metrics: MetricsRecorder,
    ) -> None:
        self._detection_engine = detection_engine
        self._masking_engine = masking_engine
        self._restoration_store = restoration_store
        self._metrics = metrics
        # A bounded striped-lock table makes get -> route -> save atomic for a
        # payload_id without retaining one lock for every identifier forever.
        self._payload_locks = tuple(
            threading.Lock() for _ in range(_PAYLOAD_LOCK_STRIPES)
        )

    def process(
        self,
        payload: str,
        payload_id: str,
        policy: ConsumerPolicy,
    ) -> ProcessOutcome:
        if not payload or not payload_id:
            raise InvalidPayloadError("payload and payload_id are required")

        with Timer() as timer:
            lock = self._payload_locks[hash(payload_id) % len(self._payload_locks)]
            with lock:
                existing = self._restoration_store.get(payload_id)
                if existing is None:
                    outcome = self._mask(payload, payload_id, policy)
                else:
                    outcome = self._route(existing, payload, payload_id, policy)

        outcome.duration_ms = timer.elapsed_ms
        self._metrics.record_request(outcome.operation.value, timer.elapsed_ms)
        self._metrics.record_entities(outcome.entity_count)
        self._metrics.record_payload_size(len(payload.encode("utf-8")))
        return outcome

    def _route(
        self,
        existing: RestorationState,
        payload: str,
        payload_id: str,
        policy: ConsumerPolicy,
    ) -> ProcessOutcome:
        if existing.state == RestorationStateStatus.MASKED:
            if hash_payload(payload) == existing.original_hash:
                # Retry of the original payload -> return the same mask.
                return self._outcome(existing.masked_text, Operation.MASK, existing)
            if payload == existing.masked_text:
                # Second stage: demask the masked payload.
                return self._demask(existing, payload_id, policy)
            raise InvalidPayloadError("payload does not match stored state")
        # State is DEMASKED: only the masked payload may be re-demasked.
        if payload == existing.masked_text:
            return self._outcome(existing.original_text, Operation.DEMASK, existing)
        raise InvalidPayloadError("payload does not match stored state")

    def _mask(
        self, payload: str, payload_id: str, policy: ConsumerPolicy
    ) -> ProcessOutcome:
        context = DetectionContext(consumer_id=policy.consumer_id)
        detection = self._detection_engine.detect(payload, context)
        entities = [
            e for e in detection.entities if e.type in policy.enabled_types
        ]
        masking: MaskingResult = self._masking_engine.mask(
            payload, entities, policy.masking
        )
        state = RestorationState(
            payload_id=payload_id,
            original_hash=hash_payload(payload),
            original_text=payload,
            masked_text=masking.masked_text,
            mappings=masking.mappings,
            entity_count=len(entities),
            state=RestorationStateStatus.MASKED,
        )
        self._restoration_store.save(payload_id, state)
        detected_types = sorted({e.type.value for e in entities})
        return self._outcome(
            masking.masked_text, Operation.MASK, state, detected_types
        )

    def _demask(
        self, existing: RestorationState, payload_id: str, policy: ConsumerPolicy
    ) -> ProcessOutcome:
        if not policy.demasking_enabled:
            raise ProcessingError("demasking is disabled for this consumer")
        original = existing.original_text
        existing.state = RestorationStateStatus.DEMASKED
        self._restoration_store.save(payload_id, existing)
        return self._outcome(original, Operation.DEMASK, existing)

    def _outcome(
        self,
        result: str,
        operation: Operation,
        state: RestorationState,
        detected_types: list[str] | None = None,
    ) -> ProcessOutcome:
        return ProcessOutcome(
            result=result,
            operation=operation,
            entity_count=state.entity_count,
            detected_types=detected_types or [],
            duration_ms=0.0,
        )
