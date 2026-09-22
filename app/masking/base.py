"""Base interfaces for the masking layer."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.enums import MaskingStrategy as MaskingStrategyEnum
from app.core.models import PIIEntity


class MaskingStrategy(ABC):
    """Interface for a strategy that masks a single PII span."""

    @abstractmethod
    def mask(self, entity: PIIEntity) -> str:
        """Return the replacement text for the given entity span."""
        raise NotImplementedError


class MaskingStrategyFactory(ABC):
    """Resolves a concrete masking strategy from a strategy enum."""

    @abstractmethod
    def get_strategy(self, strategy: MaskingStrategyEnum) -> MaskingStrategy:
        raise NotImplementedError