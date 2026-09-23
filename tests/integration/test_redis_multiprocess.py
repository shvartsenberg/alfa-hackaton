"""Multiprocess test against a real Redis.

Opt-in via the ``redis`` marker. Skipped unless ``REDIS_URL`` is set, so CI
without a Redis service does not fail. Two separate processes share one Redis
and one MASKING_KEY: process A masks, process B demasks the same payload_id
and recovers the original.

Run with:
    REDIS_URL=redis://:password@host:6379/0 pytest -m redis
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest
import redis

from app.core.enums import Operation
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.regex.detector import RegexDetector
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.observability.metrics import Metrics, MetricsRecorder
from app.policies.loader import FilePolicyProvider
from app.processing.process_service import ProcessService
from app.restoration.redis_store import RedisRestorationStore

_MASKING_KEY = "IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA="
ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"
CONSUMERS_DIR = Path(__file__).resolve().parents[2] / "configs" / "consumers"
PAYLOAD_ID = "mp-redis-1"


def _build_service(redis_url: str) -> ProcessService:
    detection = DetectionEngine(
        detectors=[RegexDetector()],
        context_resolver=ContextResolver(),
    )
    masking = MaskingEngine(DefaultMaskingStrategyFactory())
    store = RedisRestorationStore(
        client=redis.Redis.from_url(redis_url, decode_responses=False),
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


def _mask_worker(redis_url: str, queue: multiprocessing.Queue) -> None:
    service = _build_service(redis_url)
    policy = FilePolicyProvider(CONSUMERS_DIR).get_policy("default")
    outcome = service.process(ORIGINAL, PAYLOAD_ID, policy)
    queue.put((outcome.operation.value, outcome.result))


def _demask_worker(redis_url: str, masked: str, queue: multiprocessing.Queue) -> None:
    service = _build_service(redis_url)
    policy = FilePolicyProvider(CONSUMERS_DIR).get_policy("default")
    outcome = service.process(masked, PAYLOAD_ID, policy)
    queue.put((outcome.operation.value, outcome.result))


@pytest.mark.redis
def test_mask_in_one_process_demask_in_another() -> None:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.skip("REDIS_URL not set; skipping multiprocess Redis test")

    # Clear any prior state for this payload_id.
    client = redis.Redis.from_url(redis_url, decode_responses=False)
    RedisRestorationStore(client=client, masking_key=_MASKING_KEY).delete(PAYLOAD_ID)

    mask_queue: multiprocessing.Queue = multiprocessing.Queue()
    mask_proc = multiprocessing.Process(
        target=_mask_worker, args=(redis_url, mask_queue)
    )
    mask_proc.start()
    mask_proc.join(timeout=30)
    assert mask_proc.exitcode == 0, "mask process failed"
    op, masked = mask_queue.get(timeout=5)
    assert op == Operation.MASK.value
    assert masked != ORIGINAL

    demask_queue: multiprocessing.Queue = multiprocessing.Queue()
    demask_proc = multiprocessing.Process(
        target=_demask_worker, args=(redis_url, masked, demask_queue)
    )
    demask_proc.start()
    demask_proc.join(timeout=30)
    assert demask_proc.exitcode == 0, "demask process failed"
    op, restored = demask_queue.get(timeout=5)
    assert op == Operation.DEMASK.value
    assert restored == ORIGINAL