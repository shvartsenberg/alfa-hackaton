"""Consumer policy models."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import MaskingStrategy, PIIType


@dataclass(slots=True)
class ConsumerPolicy:
    """Per-consumer rules for detection, masking, and demasking."""

    consumer_id: str
    enabled: bool = True
    enabled_types: list[PIIType] = field(default_factory=list)
    masking: dict[PIIType, MaskingStrategy] = field(default_factory=dict)
    demasking_enabled: bool = True
    context_rules: list[dict[str, object]] = field(default_factory=list)

    def strategy_for(self, pii_type: PIIType) -> MaskingStrategy:
        return self.masking.get(pii_type, MaskingStrategy.FULL_MASK)