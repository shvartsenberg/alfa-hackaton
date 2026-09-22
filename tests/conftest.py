"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.settings import Settings
from app.detection.context.resolver import ContextResolver
from app.detection.engine import DetectionEngine
from app.detection.regex.detector import RegexDetector
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.policies.loader import FilePolicyProvider
from app.policies.models import ConsumerPolicy
from app.restoration.memory import InMemoryRestorationStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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