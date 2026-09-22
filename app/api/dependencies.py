"""Dependency wiring for the API.

A lightweight composition root. No heavy DI framework; services are built
once at startup and injected into handlers.
"""

from __future__ import annotations

from functools import lru_cache

from app.config.settings import get_settings
from app.core.ratelimit import SlidingWindowRateLimiter
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.regex.detector import RegexDetector
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.observability.metrics import Metrics, MetricsRecorder
from app.policies.loader import FilePolicyProvider
from app.policies.provider import PolicyProvider
from app.processing.process_service import ProcessService
from app.restoration.base import RestorationStore
from app.restoration.memory import InMemoryRestorationStore
from app.restoration.redis_store import RedisRestorationStore


@lru_cache
def get_metrics() -> Metrics:
    return Metrics()


@lru_cache
def get_metrics_recorder() -> MetricsRecorder:
    return MetricsRecorder(get_metrics())


@lru_cache
def get_restoration_store() -> RestorationStore:
    settings = get_settings()
    if settings.restoration_store_backend == "redis":
        import redis

        client = redis.Redis.from_url(settings.redis_url, decode_responses=False)
        return RedisRestorationStore(
            client=client,
            ttl_seconds=settings.restoration_ttl_seconds,
        )
    return InMemoryRestorationStore(
        ttl_seconds=settings.restoration_ttl_seconds,
        max_entries=settings.restoration_max_entries,
    )


@lru_cache
def get_policy_provider() -> PolicyProvider:
    settings = get_settings()
    return FilePolicyProvider(settings.consumers_dir)


@lru_cache
def get_detection_engine() -> DetectionEngine:
    return DetectionEngine(
        detectors=[RegexDetector()],
        context_resolver=ContextResolver(),
    )


@lru_cache
def get_masking_engine() -> MaskingEngine:
    return MaskingEngine(DefaultMaskingStrategyFactory())


@lru_cache
def get_process_service() -> ProcessService:
    return ProcessService(
        detection_engine=get_detection_engine(),
        masking_engine=get_masking_engine(),
        restoration_store=get_restoration_store(),
        metrics=get_metrics_recorder(),
    )


def get_default_consumer_id() -> str:
    return get_settings().default_consumer_id


@lru_cache
def get_rate_limiter() -> SlidingWindowRateLimiter:
    settings = get_settings()
    return SlidingWindowRateLimiter(
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )