"""ProcessService: orchestrates mask/demask with explicit idempotency.

The operation (MASK vs DEMASK) is decided from the stored RestorationState,
not from a primitive "payload_id exists" check. This makes retries safe:

- First request with a new payload_id -> MASK, store state.
- Retry of the same original payload -> same MASKED result (idempotent).
- Request with the masked payload -> DEMASK, return original.
- Retry of the masked payload -> same ORIGINAL result (idempotent).

The read-modify-write of the lifecycle is performed atomically through the
store's ``transition`` method, so concurrent requests for the same payload_id
cannot corrupt the state (in-memory via a lock, Redis via compare-and-set).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import Operation, RestorationStateStatus
from app.core.exceptions import InvalidPayloadError, ProcessingError
from app.core.models import DetectionContext
from app.core.security import hash_payload
from app.detection.engine import DetectionEngine
from app.masking.engine import MaskingEngine, MaskingResult
from app.observability.metrics import MetricsRecorder, Timer
from app.policies.models import ConsumerPolicy
from app.restoration.base import RestorationStore
from app.restoration.models import RestorationState


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

    def process(
        self,
        payload: str,
        payload_id: str,
        policy: ConsumerPolicy,
    ) -> ProcessOutcome:
        if not payload or not payload_id:
            raise InvalidPayloadError("payload and payload_id are required")

        with Timer() as timer:
            state = self._restoration_store.transition(
                payload_id,
                lambda current: self._transition(current, payload, payload_id, policy),
            )
            outcome = self._outcome_from_state(state, payload, payload_id)

        outcome.duration_ms = timer.elapsed_ms
        self._metrics.record_request(outcome.operation.value, timer.elapsed_ms)
        self._metrics.record_entities(outcome.entity_count)
        self._metrics.record_payload_size(len(payload.encode("utf-8")))
        return outcome

    def _transition(
        self,
        current: RestorationState | None,
        payload: str,
        payload_id: str,
        policy: ConsumerPolicy,
    ) -> RestorationState:
        if current is None:
            return self._mask_state(payload, payload_id, policy)
        if current.state == RestorationStateStatus.MASKED:
            if hash_payload(payload) == current.original_hash:
                # Retry of the original payload -> keep the same mask.
                return current
            if payload == current.masked_text:
                # Second stage: demask the masked payload.
                return self._demask_state(current, policy)
            raise InvalidPayloadError("payload does not match stored state")
        # State is DEMASKED: only the masked payload may be re-demasked.
        if payload == current.masked_text:
            return current
        raise InvalidPayloadError("payload does not match stored state")

    def _mask_state(
        self, payload: str, payload_id: str, policy: ConsumerPolicy
    ) -> RestorationState:
        context = DetectionContext(consumer_id=policy.consumer_id)
        detection = self._detection_engine.detect(payload, context)
        entities = [
            e for e in detection.entities if e.type in policy.enabled_types
        ]
        masking: MaskingResult = self._masking_engine.mask(
            payload,
            entities,
            policy.masking,
            context_rules=policy.context_rules,
        )
        return RestorationState(
            payload_id=payload_id,
            original_hash=hash_payload(payload),
            original_text=payload,
            masked_text=masking.masked_text,
            mappings=masking.mappings,
            entity_count=len(entities),
            detected_types=sorted({e.type.value for e in entities}),
            state=RestorationStateStatus.MASKED,
        )

    def _demask_state(
        self, current: RestorationState, policy: ConsumerPolicy
    ) -> RestorationState:
        if not policy.demasking_enabled:
            raise ProcessingError("demasking is disabled for this consumer")
        current.state = RestorationStateStatus.DEMASKED
        return current

    def _outcome_from_state(
        self,
        state: RestorationState | None,
        payload: str,
        payload_id: str,
    ) -> ProcessOutcome:
        if state is None:
            raise ProcessingError("restoration state was unexpectedly removed")
        if state.state == RestorationStateStatus.MASKED:
            return self._outcome(state.masked_text, Operation.MASK, state)
        return self._outcome(state.original_text, Operation.DEMASK, state)

    def _outcome(
        self,
        result: str,
        operation: Operation,
        state: RestorationState,
    ) -> ProcessOutcome:
        return ProcessOutcome(
            result=result,
            operation=operation,
            entity_count=state.entity_count,
            detected_types=list(state.detected_types),
            duration_ms=0.0,
        )