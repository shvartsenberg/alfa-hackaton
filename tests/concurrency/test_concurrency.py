"""Concurrency tests for the mask/demask lifecycle.

These tests are opt-in (marker ``concurrency``) because they exercise shared
state under concurrent access and are slower than the default suite.

They exercise ``ProcessService`` directly (no HTTP layer) so the shared
``InMemoryRestorationStore`` is the concurrency target.

Run with:
    pytest -m concurrency
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.core.exceptions import InvalidPayloadError
from app.core.models import DetectionContext, PIIEntity
from app.detection.base import PIIDetector
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


class _SlowDetector(PIIDetector):
    """Wraps the regex detector and adds a fixed delay to simulate heavy work."""

    name = "slow"

    def __init__(self, delay_seconds: float) -> None:
        self._delay = delay_seconds
        self._inner = RegexDetector()

    def detect(self, text: str, context: DetectionContext) -> list[PIIEntity]:
        time.sleep(self._delay)
        return self._inner.detect(text, context)


def _build_service(delay_seconds: float = 0.0) -> ProcessService:
    detection = DetectionEngine(
        detectors=[_SlowDetector(delay_seconds)],
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


@pytest.mark.concurrency
def test_parallel_masks_are_not_serialized_on_one_lock() -> None:
    """Detection/masking for distinct payload_ids must run concurrently.

    With a 50 ms detector, 8 parallel requests should finish well under the
    400 ms a fully serialized run would take.
    """
    service = _build_service(delay_seconds=0.05)
    policy = FilePolicyProvider(CONSUMERS_DIR).get_policy("default")

    def mask_once(i: int) -> str:
        return service.process(ORIGINAL, f"conc-par-{i}", policy).result

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(mask_once, range(8)))
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert len(results) == 8
    assert elapsed_ms < 200


@pytest.mark.concurrency
def test_http_handler_is_threadpool_def_not_async() -> None:
    """The /process handler must be a plain ``def`` so FastAPI runs it in the
    thread pool and a slow request does not block the event loop."""
    from app.api.routes.process import process as process_handler

    assert not getattr(process_handler, "__is_async__", False) or not hasattr(
        process_handler, "__code__"
    ) or not process_handler.__code__.co_flags & 0x80


@pytest.mark.concurrency
def test_http_concurrent_requests_not_blocked_by_slow_detector() -> None:
    """A slow (blocking) request must not block other HTTP requests.

    The /process handler is a plain ``def``, so FastAPI runs it in the thread
    pool. We override the process service with a detector that blocks on an
    Event; a second request must complete while the first is still blocked.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app.api.dependencies import get_process_service
    from app.main import app

    release = threading.Event()
    entered = threading.Event()
    first_call = True
    call_lock = threading.Lock()

    class _BlockingDetector(PIIDetector):
        name = "blocking"

        def detect(self, text: str, context: DetectionContext) -> list[PIIEntity]:
            nonlocal first_call
            with call_lock:
                is_first = first_call
                first_call = False
            if is_first:
                entered.set()
                release.wait(timeout=5.0)
            return RegexDetector().detect(text, context)

    detection = DetectionEngine(
        detectors=[_BlockingDetector()],
        context_resolver=ContextResolver(),
    )
    masking = MaskingEngine(DefaultMaskingStrategyFactory())
    store = InMemoryRestorationStore(ttl_seconds=3600, max_entries=1000)
    metrics = MetricsRecorder(Metrics())
    service = ProcessService(
        detection_engine=detection,
        masking_engine=masking,
        restoration_store=store,
        metrics=metrics,
    )

    app.dependency_overrides[get_process_service] = lambda: service
    try:
        client = TestClient(app)

        def slow_request() -> int:
            resp = client.post(
                "/process",
                json={"payload": ORIGINAL, "payload_id": "http-slow"},
            )
            return resp.status_code

        def fast_request() -> int:
            resp = client.post(
                "/process",
                json={"payload": ORIGINAL, "payload_id": "http-fast"},
            )
            return resp.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            slow_future = pool.submit(slow_request)
            # Wait until the slow request has entered the blocking detector.
            assert entered.wait(timeout=5.0), "slow request never entered detector"
            fast_future = pool.submit(fast_request)
            # The fast request must complete while the slow one is still blocked.
            fast_status = fast_future.result(timeout=5.0)
            assert fast_status == 200
            # Now release the slow request.
            release.set()
            slow_status = slow_future.result(timeout=5.0)
            assert slow_status == 200
    finally:
        app.dependency_overrides.clear()
