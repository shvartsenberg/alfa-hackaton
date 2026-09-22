"""Masking engine: applies strategies to PII spans safely."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.enums import MaskingStrategy as MaskingStrategyEnum
from app.core.enums import PIIType
from app.core.models import PIIEntity
from app.masking.base import MaskingStrategy, MaskingStrategyFactory
from app.masking.strategies.impl import (
    FullMaskStrategy,
    PartialMaskStrategy,
    SyntheticStrategy,
    TokenizeStrategy,
)


@dataclass(slots=True)
class MaskingResult:
    """Result of masking a text."""

    masked_text: str
    entities: list[PIIEntity] = field(default_factory=list)
    mappings: dict[str, str] = field(default_factory=dict)
    processing_time: float = 0.0


class DefaultMaskingStrategyFactory(MaskingStrategyFactory):
    """Maps strategy enums to concrete strategy instances."""

    def __init__(self) -> None:
        self._strategies: dict[MaskingStrategyEnum, MaskingStrategy] = {
            MaskingStrategyEnum.FULL_MASK: FullMaskStrategy(),
            MaskingStrategyEnum.PARTIAL_MASK: PartialMaskStrategy(),
            MaskingStrategyEnum.TOKENIZE: TokenizeStrategy(),
            MaskingStrategyEnum.SYNTHETIC: SyntheticStrategy(),
        }

    def get_strategy(self, strategy: MaskingStrategyEnum) -> MaskingStrategy:
        return self._strategies[strategy]


class MaskingEngine:
    """Masks a text by replacing PII spans with strategy output.

    Replacements are applied right-to-left so that earlier span offsets stay
    valid while later spans are replaced first.
    """

    def __init__(self, strategy_factory: MaskingStrategyFactory) -> None:
        self._strategy_factory = strategy_factory

    def mask(
        self,
        text: str,
        entities: list[PIIEntity],
        strategy_by_type: dict[PIIType, MaskingStrategyEnum],
    ) -> MaskingResult:
        started = time.perf_counter()
        ordered = sorted(entities, key=lambda e: e.start, reverse=True)
        masked = text
        mappings: dict[str, str] = {}
        for entity in ordered:
            strategy_enum = strategy_by_type.get(entity.type, MaskingStrategyEnum.FULL_MASK)
            strategy = self._strategy_factory.get_strategy(strategy_enum)
            replacement = strategy.mask(entity)
            masked = masked[: entity.start] + replacement + masked[entity.end :]
            mappings[replacement] = entity.value
        elapsed = time.perf_counter() - started
        return MaskingResult(
            masked_text=masked,
            entities=entities,
            mappings=mappings,
            processing_time=elapsed,
        )