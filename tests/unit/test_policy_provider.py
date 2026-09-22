"""Unit tests for the policy provider."""

from __future__ import annotations

import pytest

from app.core.enums import MaskingStrategy, PIIType
from app.core.exceptions import ConsumerNotAllowedError
from app.policies.loader import FilePolicyProvider


def test_loads_default_policy(policy_provider: FilePolicyProvider) -> None:
    policy = policy_provider.get_policy("default")
    assert policy.enabled is True
    assert PIIType.EMAIL in policy.enabled_types
    assert policy.strategy_for(PIIType.EMAIL) == MaskingStrategy.FULL_MASK
    assert policy.strategy_for(PIIType.PHONE) == MaskingStrategy.PARTIAL_MASK


def test_loads_demo_policy(policy_provider: FilePolicyProvider) -> None:
    policy = policy_provider.get_policy("demo")
    assert policy.enabled is True
    assert policy.strategy_for(PIIType.EMAIL) == MaskingStrategy.PARTIAL_MASK


def test_unknown_consumer_raises(policy_provider: FilePolicyProvider) -> None:
    with pytest.raises(ConsumerNotAllowedError):
        policy_provider.get_policy("does_not_exist")