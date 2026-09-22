"""Concurrency tests for the mask/demask lifecycle.

These tests are opt-in (marker ``concurrency``) because they exercise shared
state under concurrent access and are slower than the default suite.

They exercise ``ProcessService`` directly (no HTTP layer) so the shared
``InMemoryRestorationStore`` is the concurrency target.

Run with:
    pytest -m concurrency
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.core.exceptions import InvalidPayloadError
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.regex.detector import RegexDetector
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.observability.metrics import Metrics, MetricsRecorder
from app.policies.loader import FilePolicyProvider
from app.processing.process_service import ProcessService
from app.restoration.memory import InMemoryRestorationStore

ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"
CONSUMERS_DIR = Path(__file__).resolve().parents[2] / "configs" / "consumers"


def _build_service() -> ProcessService:
    detection = DetectionEngine(
        detectors=[RegexDetector()],
        context_resolver=ContextResolver(),
    )
    masking = MaskingEngine(DefaultMaskingStrategyFactory())
    store = InMemoryRestorationStore(ttl_seconds=3600, max_entries=1000)
    metrics = MetricsRecorder(Metrics())
    return ProcessService(
        detection_engine=detection,
        masking_engine=masking,
        restoration_store=store,
        metrics=metrics,
    )


def _results(futures: list[Future[str]], timeout: float = 5.0) -> list[str]:
    """Resolve every worker and surface its original exception."""
    return [future.result(timeout=timeout) for future in futures]


@pytest.mark.concurrency
def test_concurrent_independent_payload_ids() -> None:
    """Concurrent mask/demask on distinct payload_ids must not interfere."""
    service = _build_service()
    policy = FilePolicyProvider(CONSUMERS_DIR).get_policy("default")

    def roundtrip(i: int) -> bool:
        payload_id = f"conc-{i}"
        masked = service.process(ORIGINAL, payload_id, policy).result
        restored = service.process(masked, payload_id, policy).result
        return restored == ORIGINAL

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(roundtrip, range(32)))

    assert all(results)


@pytest.mark.concurrency
def test_concurrent_same_payload_id() -> None:
    """Concurrent MASK on the same payload_id must be idempotent.

    NOTE: this documents the current behaviour. The store's get->route->save
    sequence is not a single atomic operation, so under heavy contention the
    outcome may vary. This is a known limitation (see report).
    """
    service = _build_service()
    policy = FilePolicyProvider(CONSUMERS_DIR).get_policy("default")

    def mask_once() -> str:
        return service.process(ORIGINAL, "conc-same", policy).result

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(mask_once) for _ in range(16)]
        results = _results(futures)

    assert len(results) == 16
    assert len(set(results)) == 1


@pytest.mark.concurrency
def test_concurrent_demask_retries() -> None:
    """Every concurrent retry of the same masked payload restores the original."""
    service = _build_service()
    policy = FilePolicyProvider(CONSUMERS_DIR).get_policy("default")
    masked = service.process(ORIGINAL, "conc-demask", policy).result

    def demask_once() -> str:
        return service.process(masked, "conc-demask", policy).result

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(demask_once) for _ in range(16)]
        results = _results(futures)

    assert results == [ORIGINAL] * 16


@pytest.mark.concurrency
def test_concurrent_different_payloads_with_same_id_do_not_overwrite_state() -> None:
    """Exactly one competing original owns a previously unseen payload_id."""
    service = _build_service()
    policy = FilePolicyProvider(CONSUMERS_DIR).get_policy("default")
    barrier = Barrier(2)
    originals = ("first@example.com", "second@example.com")

    def compete(original: str) -> tuple[str, str | None]:
        barrier.wait(timeout=5)
        try:
            return original, service.process(original, "conc-conflict", policy).result
        except InvalidPayloadError:
            return original, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, original) for original in originals]
        results = [future.result(timeout=5) for future in futures]

    successful = [(original, masked) for original, masked in results if masked is not None]
    rejected = [(original, masked) for original, masked in results if masked is None]
    assert len(successful) == 1
    assert len(rejected) == 1
    winning_original, winning_mask = successful[0]
    assert winning_mask is not None
    assert service.process(winning_mask, "conc-conflict", policy).result == winning_original
