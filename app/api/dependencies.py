"""Dependency wiring for the API.

A lightweight composition root. No heavy DI framework; services are built
once at startup and injected into handlers.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import Header

from app.config.settings import get_settings
from app.core.ratelimit import SlidingWindowRateLimiter
from app.detection.base import PIIDetector
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.llm_detector import LLMClient, LLMDetector
from app.detection.regex.detector import RegexDetector
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.observability.metrics import Metrics, MetricsRecorder
from app.policies.loader import FilePolicyProvider
from app.policies.provider import PolicyProvider
from app.processing.process_service import ProcessService
from app.restoration.base import RestorationStore
from app.restoration.memory import InMemoryRestorationStore
from app.restoration.redis_store import RedisRestorationStore


class _OpenAICompatClient:
    """Minimal OpenAI-compatible chat completions client."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": "gpt-4o-mini",
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response = httpx.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=2.0,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return str(content)


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
            masking_key=settings.masking_key,
        )
    return InMemoryRestorationStore(
        ttl_seconds=settings.restoration_ttl_seconds,
        max_entries=settings.restoration_max_entries,
        masking_key=settings.masking_key,
    )


@lru_cache
def get_policy_provider() -> PolicyProvider:
    settings = get_settings()
    return FilePolicyProvider(settings.consumers_dir)


@lru_cache
def get_detection_engine() -> DetectionEngine:
    settings = get_settings()
    detectors: list[PIIDetector] = [RegexDetector()]
    if settings.detection_llm_enabled:
        client: LLMClient = _OpenAICompatClient(
            settings.llm_base_url,
            settings.llm_api_key,
        )
        detectors.append(LLMDetector(client))
    return DetectionEngine(
        detectors=detectors,
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


def get_consumer_id(
    x_consumer_id: Annotated[str | None, Header()] = None,
) -> str:
    """Return a normalized consumer id.

    The header value is trimmed and lowercased so an unvalidated value is never
    used or logged as-is. An empty value falls back to the default consumer.
    """
    if x_consumer_id is None:
        return get_settings().default_consumer_id
    normalized = x_consumer_id.strip().lower()
    return normalized or get_settings().default_consumer_id


@lru_cache
def get_rate_limiter() -> SlidingWindowRateLimiter:
    settings = get_settings()
    return SlidingWindowRateLimiter(
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )