"""Masking engine: applies strategies to PII spans safely."""

from __future__ import annotations

import logging
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

logger = logging.getLogger("pii_security_proxy.masking.engine")

# Default context rules: a PII type is only masked when at least one of the
# required types is present in the same text. Used when no rules are supplied
# by the consumer policy (context_rules is None or empty).
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
        *,
        context_rules: list[dict[str, object]] | None = None,
    ) -> MaskingResult:
        started = time.perf_counter()
        valid = self._select_spans(text, entities)
        rules = self._resolve_context_rules(context_rules)
        present_types = {e.type for e in valid}
        ordered = sorted(valid, key=lambda e: e.start)
        pieces: list[str] = []
        mappings: dict[str, str] = {}
        cursor = 0
        for entity in ordered:
            if not self._context_allows(entity.type, present_types, rules):
                continue
            strategy_enum = strategy_by_type.get(entity.type, MaskingStrategyEnum.PARTIAL_MASK)
            strategy = self._strategy_factory.get_strategy(strategy_enum)
            replacement = strategy.mask(entity)
            pieces.append(text[cursor : entity.start])
            pieces.append(replacement)
            mappings[replacement] = entity.value
            cursor = entity.end
        pieces.append(text[cursor:])
        elapsed = time.perf_counter() - started
        return MaskingResult(
            masked_text="".join(pieces),
            entities=valid,
            mappings=mappings,
            processing_time=elapsed,
        )

    def _select_spans(self, text: str, entities: list[PIIEntity]) -> list[PIIEntity]:
        """Drop invalid spans and keep the longest of overlapping ones."""
        length = len(text)
        valid: list[PIIEntity] = []
        for e in entities:
            if e.start < 0 or e.end > length or e.start >= e.end:
                logger.warning("dropping invalid span type=%s", e.type.value)
                continue
            if text[e.start : e.end] != e.value:
                logger.warning("dropping mismatched span type=%s", e.type.value)
                continue
            valid.append(e)
        valid.sort(key=lambda e: (e.start, -(e.end - e.start)))
        selected: list[PIIEntity] = []
        for entity in valid:
            if selected and entity.start < selected[-1].end:
                # Overlap: keep the longer span (already sorted longest-first
                # within the same start), skip the shorter one.
                continue
            selected.append(entity)
        return selected

    @staticmethod
    def _resolve_context_rules(
        context_rules: list[dict[str, object]] | None,
    ) -> dict[PIIType, frozenset[PIIType]]:
        """Resolve policy context rules into a type -> required-types map.

        An empty or missing rule list falls back to the default
        ``CONTEXT_REQUIRES``. A rule with ``enabled: false`` explicitly removes
        the context requirement for that type (it is always masked). Rules with
        unknown PII types are skipped with a warning instead of failing.
        """
        if not context_rules:
            return dict(CONTEXT_REQUIRES)
        rules: dict[PIIType, frozenset[PIIType]] = {}
        for rule in context_rules:
            raw_type = rule.get("type")
            if not isinstance(raw_type, str):
                logger.warning("context rule missing 'type'; skipped")
                continue
            try:
                pii_type = PIIType(raw_type)
            except ValueError:
                logger.warning("context rule has unknown PII type %r; skipped", raw_type)
                continue
            if rule.get("enabled", True) is False:
                rules[pii_type] = frozenset()
                continue
            raw_required = rule.get("requires")
            if not isinstance(raw_required, list) or not raw_required:
                logger.warning(
                    "context rule for %s has no 'requires' list; skipped", raw_type
                )
                continue
            required: set[PIIType] = set()
            valid = True
            for raw_req in raw_required:
                try:
                    required.add(PIIType(raw_req))
                except ValueError:
                    logger.warning(
                        "context rule for %s has unknown required type %r; skipped",
                        raw_type,
                        raw_req,
                    )
                    valid = False
                    break
            if valid:
                rules[pii_type] = frozenset(required)
        # If every rule was skipped, fall back to the default so a broken
        # config does not silently disable the PIN protection.
        if not rules:
            return dict(CONTEXT_REQUIRES)
        return rules

    @staticmethod
    def _context_allows(
        pii_type: PIIType,
        present_types: set[PIIType],
        rules: dict[PIIType, frozenset[PIIType]],
    ) -> bool:
        required = rules.get(pii_type)
        if not required:
            return True
        return bool(required & present_types)