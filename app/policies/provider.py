"""Policy provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.policies.models import ConsumerPolicy


class PolicyProvider(ABC):
    """Resolves the policy for a given consumer."""

    @abstractmethod
    def get_policy(self, consumer_id: str) -> ConsumerPolicy:
        raise NotImplementedError