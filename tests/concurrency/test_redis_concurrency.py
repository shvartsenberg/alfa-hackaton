"""Concurrency tests for the Redis-backed store via the HTTP layer.

These tests are opt-in (marker ``concurrency``). They exercise the full
``POST /process`` path through ``TestClient`` with a ``RedisRestorationStore``
backed by ``fakeredis``, so the compare-and-set lifecycle is the concurrency
target.

Run with:
    pytest -m concurrency
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_process_service
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.regex.detector import RegexDetector
from app.main import app
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.observability.metrics import Metrics, MetricsRecorder
from app.processing.process_service import ProcessService
from app.restoration.redis_store import RedisRestorationStore

_MASKING_KEY = "IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA="
ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"
CONSUMERS_DIR = Path(__file__).resolve().parents[2] / "configs" / "consumers"


def _build_service() -> ProcessService:
    detection = DetectionEngine(
        detectors=[RegexDetector()],
        context_resolver=ContextResolver(),
    )
    masking = MaskingEngine(DefaultMaskingStrategyFactory())
    store = RedisRestorationStore(
        client=fakeredis.FakeRedis(),
        ttl_seconds=3600,
        masking_key=_MASKING_KEY,
    )
    metrics = MetricsRecorder(Metrics())
    return ProcessService(
        detection_engine=detection,
        masking_engine=masking,
        restoration_store=store,
        metrics=metrics,
    )


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[get_process_service] = lambda: _build_service()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_process_service, None)


def _post(client: TestClient, payload_id: str, payload: str) -> tuple[int, str]:
    resp = client.post("/process", json={"payload": payload, "payload_id": payload_id})
    return resp.status_code, resp.json().get("result", "")


@pytest.mark.concurrency
def test_50_parallel_same_payload_id_no_500(client: TestClient) -> None:
    """50 concurrent MASK requests for one payload_id: no 500, one consistent result."""

    def mask_once() -> tuple[int, str]:
        return _post(client, "conc-50", ORIGINAL)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: mask_once(), range(50)))

    statuses = [s for s, _ in results]
    bodies = [b for _, b in results]
    assert 500 not in statuses
    assert len(set(bodies)) == 1


@pytest.mark.concurrency
def test_parallel_mask_and_demask_do_not_corrupt_state(client: TestClient) -> None:
    """Concurrent mask and demask of one payload_id keep the state consistent."""
    masked = _post(client, "conc-md", ORIGINAL)[1]

    def mask_again() -> tuple[int, str]:
        return _post(client, "conc-md", ORIGINAL)

    def demask() -> tuple[int, str]:
        return _post(client, "conc-md", masked)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(mask_again) for _ in range(8)]
        futures += [pool.submit(demask) for _ in range(8)]
        results = [f.result(timeout=10) for f in futures]

    statuses = [s for s, _ in results]
    assert 500 not in statuses
    # Every response is either the mask or the original; never a 500.
    bodies = [b for _, b in results]
    assert all(b in {masked, ORIGINAL} for b in bodies)