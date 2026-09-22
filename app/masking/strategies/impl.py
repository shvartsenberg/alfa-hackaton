"""Masking strategy implementations."""

from __future__ import annotations

from app.core.models import PIIEntity
from app.masking.base import MaskingStrategy

_FULL_MASK_CHAR = "*"
_PARTIAL_VISIBLE_HEAD = 3
_PARTIAL_VISIBLE_TAIL = 2


class FullMaskStrategy(MaskingStrategy):
    """Replaces the entire span with a fixed-length mask."""

    def mask(self, entity: PIIEntity) -> str:
        return _FULL_MASK_CHAR * len(entity.value)


class PartialMaskStrategy(MaskingStrategy):
    """Keeps a few leading and trailing characters, masks the middle."""

    def mask(self, entity: PIIEntity) -> str:
        value = entity.value
        length = len(value)
        if length <= _PARTIAL_VISIBLE_HEAD + _PARTIAL_VISIBLE_TAIL:
            return _FULL_MASK_CHAR * length
        head = value[:_PARTIAL_VISIBLE_HEAD]
        tail = value[-_PARTIAL_VISIBLE_TAIL:]
        middle_len = length - _PARTIAL_VISIBLE_HEAD - _PARTIAL_VISIBLE_TAIL
        return f"{head}{_FULL_MASK_CHAR * middle_len}{tail}"


class TokenizeStrategy(MaskingStrategy):
    """Replaces the span with a stable token placeholder.

    Extension point: a real implementation would map to a tokenization store.
    """

    def mask(self, entity: PIIEntity) -> str:
        return f"<TOKEN:{entity.type.value}>"


class SyntheticStrategy(MaskingStrategy):
    """Replaces the span with synthetic data.

    Extension point: a real implementation would generate realistic fake data.
    """

    def mask(self, entity: PIIEntity) -> str:
        return f"<SYNTHETIC:{entity.type.value}>"