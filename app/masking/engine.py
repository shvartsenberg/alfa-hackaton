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

# Context rules: a PII type is only masked when at least one of the required
# types is present in the same text. Configurable in one place so new rules
# can be added without touching the pipeline.
CONTEXT_REQUIRES: dict[PIIType, frozenset[PIIType]] = {
    PIIType.PIN: frozenset({PIIType.BANK_CARD}),
}


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
    valid while later spans are replaced first. Invalid spans are dropped and
    overlapping spans keep the longest one.
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
        valid = self._select_spans(text, entities)
        present_types = {e.type for e in valid}
        ordered = sorted(valid, key=lambda e: e.start, reverse=True)
        masked = text
        mappings: dict[str, str] = {}
        for entity in ordered:
            if not self._context_allows(entity.type, present_types):
                continue
            strategy_enum = strategy_by_type.get(entity.type, MaskingStrategyEnum.PARTIAL_MASK)
            strategy = self._strategy_factory.get_strategy(strategy_enum)
            replacement = strategy.mask(entity)
            masked = masked[: entity.start] + replacement + masked[entity.end :]
            mappings[replacement] = entity.value
        elapsed = time.perf_counter() - started
        return MaskingResult(
            masked_text=masked,
            entities=valid,
            mappings=mappings,
            processing_time=elapsed,
        )

    def _select_spans(self, text: str, entities: list[PIIEntity]) -> list[PIIEntity]:
        """Drop invalid spans and keep the longest of overlapping ones."""
        length = len(text)
        valid = [
            e
            for e in entities
            if e.start >= 0 and e.end <= length and e.start < e.end
        ]
        valid.sort(key=lambda e: (e.start, -(e.end - e.start)))
        selected: list[PIIEntity] = []
        for entity in valid:
            if selected and entity.start < selected[-1].end:
                # Overlap: keep the longer span (already sorted longest-first
                # within the same start), skip the shorter one.
                continue
            selected.append(entity)
        return selected

    def _context_allows(self, pii_type: PIIType, present_types: set[PIIType]) -> bool:
        required = CONTEXT_REQUIRES.get(pii_type)
        if not required:
            return True
        return bool(required & present_types)