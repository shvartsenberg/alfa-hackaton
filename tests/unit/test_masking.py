"""Unit tests for masking strategies."""

from __future__ import annotations

from app.core.enums import PIIType
from app.core.models import PIIEntity
from app.masking.strategies.impl import FullMaskStrategy, PartialMaskStrategy


def _entity(value: str) -> PIIEntity:
    return PIIEntity(
        type=PIIType.EMAIL,
        value=value,
        start=0,
        end=len(value),
    )


def test_full_mask_replaces_all_chars() -> None:
    strategy = FullMaskStrategy()
    assert strategy.mask(_entity("test@example.com")) == "****************"


def test_partial_mask_keeps_edges() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity("+7 999 123-45-67"))
    assert result.startswith("+7 ")
    assert result.endswith("67")
    assert "*" in result


def test_partial_mask_short_value_is_fully_masked() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity("ab"))
    assert result == "**"