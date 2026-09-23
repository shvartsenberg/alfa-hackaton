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

import hashlib
from dataclasses import dataclass, field

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
    stage_timings: dict[str, float] = field(default_factory=dict)


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

        key = self._state_key(policy.consumer_id, payload_id)
        stage_timings: dict[str, float] = {}
        with Timer() as timer:
            # Detection + masking are expensive. They are only needed when the
            # state is absent (a new MASK). For DEMASK, retry MASK, retry
            # DEMASK and conflict cases the state already exists, so we skip
            # detection/masking entirely.
            #
            # We do a cheap lookup first to decide whether to prepare. The
            # prepared result is memoized in a closure so a CAS retry inside
            # the store's transition does not re-run the detector/masker.
            prepared: RestorationState | None = None
            with Timer() as lookup_timer:
                state_exists = self._restoration_store.get(key) is not None
            stage_timings["state_lookup_ms"] = lookup_timer.elapsed_ms
            if not state_exists:
                with Timer() as prepare_timer:
                    prepared = self._prepare_mask(payload, key, policy)
                stage_timings["prepare_ms"] = prepare_timer.elapsed_ms

            with Timer() as transition_timer:
                state = self._restoration_store.transition(
                    key,
                    lambda current: self._transition(
                        current, payload, key, policy, prepared
                    ),
                )
            stage_timings["transition_ms"] = transition_timer.elapsed_ms
            outcome = self._outcome_from_state(state, payload, key)

        outcome.duration_ms = timer.elapsed_ms
        outcome.stage_timings = stage_timings
        self._metrics.record_request(outcome.operation.value, timer.elapsed_ms)
        self._metrics.record_entities(outcome.entity_count)
        self._metrics.record_payload_size(len(payload.encode("utf-8")))
        for stage, duration_ms in stage_timings.items():
            self._metrics.record_stage(stage, duration_ms)
        return outcome

    @staticmethod
    def _state_key(consumer_id: str, payload_id: str) -> str:
        """Build an unambiguous storage key from consumer and payload ids.

        A simple ``consumer:payload`` concatenation is ambiguous when either
        value contains ``:``. We hash the two values with a NUL separator so
        the composite key is unique regardless of the characters in the inputs.
        The store additionally HMACs this key before writing to Redis.
        """
        digest = hashlib.sha256(
            f"{consumer_id}\0{payload_id}".encode()
        ).hexdigest()
        return f"{consumer_id}:{digest}"

    def _transition(
        self,
        current: RestorationState | None,
        payload: str,
        payload_id: str,
        policy: ConsumerPolicy,
        prepared: RestorationState | None,
    ) -> RestorationState:
        if current is None:
            if prepared is None:
                # Rare race: the state was created and removed between the
                # initial lookup and the transition. Prepare now (once).
                prepared = self._prepare_mask(payload, payload_id, policy)
            return prepared
        # The state belongs to a different consumer (or to no one, e.g. a
        # legacy record without consumer_id): never expose it.
        if current.consumer_id != policy.consumer_id:
            raise InvalidPayloadError("payload does not match stored state")
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

    def _prepare_mask(
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
            consumer_id=policy.consumer_id,
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