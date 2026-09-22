"""Shared pytest fixtures.

Isolation guarantees:
- ``@lru_cache`` dependency singletons are cleared before and after each test
  so no RestorationState or metrics leak between tests.
- A fresh ``Metrics`` instance is used per test.
- ``payload_id`` values are unique per test to avoid cross-test collisions.
- A ``TestClient`` with ``raise_server_exceptions=False`` is provided for
  asserting on 500 responses without the framework re-raising.
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies
from app.config.settings import Settings
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.regex.detector import RegexDetector
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.observability.metrics import Metrics
from app.policies.loader import FilePolicyProvider
from app.policies.models import ConsumerPolicy
from app.restoration.memory import InMemoryRestorationStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_payload_id_counter = itertools.count(1)


def _clear_dependency_caches() -> None:
    """Clear all lru_cache-backed dependency singletons."""
    for name in (
        "get_metrics",
        "get_metrics_recorder",
        "get_restoration_store",
        "get_policy_provider",
        "get_detection_engine",
        "get_masking_engine",
        "get_process_service",
    ):
        fn = getattr(dependencies, name)
        fn.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_dependencies() -> None:
    """Clear dependency caches before and after every test."""
    from app.main import app

    _clear_dependency_caches()
    app.dependency_overrides.clear()
    logging.getLogger("pii_security_proxy").propagate = False
    yield
    app.dependency_overrides.clear()
    logging.getLogger("pii_security_proxy").propagate = False
    _clear_dependency_caches()


@pytest.fixture
def consumers_dir() -> Path:
    return PROJECT_ROOT / "configs" / "consumers"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        consumers_dir=consumers_dir,
        restoration_ttl_seconds=3600,
    )


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
def detection_engine() -> DetectionEngine:
    return DetectionEngine(
        detectors=[RegexDetector()],
        context_resolver=ContextResolver(),
    )


@pytest.fixture
def masking_engine() -> MaskingEngine:
    return MaskingEngine(DefaultMaskingStrategyFactory())


@pytest.fixture
def restoration_store() -> InMemoryRestorationStore:
    return InMemoryRestorationStore(ttl_seconds=3600)


@pytest.fixture
def policy_provider(consumers_dir: Path) -> FilePolicyProvider:
    return FilePolicyProvider(consumers_dir)


@pytest.fixture
def default_policy(policy_provider: FilePolicyProvider) -> ConsumerPolicy:
    return policy_provider.get_policy("default")


@pytest.fixture
def unique_payload_id() -> str:
    """Return a unique payload_id for the current test."""
    return f"test-{next(_payload_id_counter)}"


@pytest.fixture
def client() -> TestClient:
    """TestClient that does not re-raise server exceptions.

    This lets tests assert on the 500 JSON body produced by the error handler.
    """
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_raise() -> TestClient:
    """TestClient that re-raises server exceptions (default behaviour)."""
    from app.main import app

    return TestClient(app, raise_server_exceptions=True)
