"""Unit tests for masking strategies and the masking engine."""

from __future__ import annotations

import pytest

from app.core.enums import MaskingStrategy, PIIType
from app.core.models import PIIEntity
from app.masking.engine import DefaultMaskingStrategyFactory, MaskingEngine
from app.masking.strategies.impl import FullMaskStrategy, PartialMaskStrategy


def _entity(pii_type: PIIType, value: str, start: int = 0) -> PIIEntity:
    return PIIEntity(
        type=pii_type,
        value=value,
        start=start,
        end=start + len(value),
    )


def _engine() -> MaskingEngine:
    return MaskingEngine(DefaultMaskingStrategyFactory())


def _mask(text: str, entities: list[PIIEntity]) -> str:
    # Tests exercise the PARTIAL_MASK reference format by default; the
    # production default is FULL_MASK (safe), which is covered separately.
    strategy_map = {e.type: MaskingStrategy.PARTIAL_MASK for e in entities}
    return _engine().mask(text, entities, strategy_map).masked_text


# --- Strategy-level tests -------------------------------------------------


def test_full_mask_replaces_alnum_keeps_separators() -> None:
    strategy = FullMaskStrategy()
    assert strategy.mask(_entity(PIIType.EMAIL, "test@example.com")) == "****@*******.***"


def test_partial_mask_initials() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.PERSON_NAME, "Иванов Иван Иванович"))
    assert result == "И. И. И."


def test_partial_mask_card_with_spaces() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.BANK_CARD, "4567 8901 2345 6756"))
    assert result == "45** **** **** **56"


def test_partial_mask_passport() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.PASSPORT_NUMBER, "4509 123456"))
    assert result == "45** ****56"


def test_partial_mask_phone() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.PHONE, "+7 999 123-45-67"))
    assert result == "+7 9** ***-**-67"


def test_partial_mask_email() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.EMAIL, "test@example.com"))
    assert result == "t***@example.com"


def test_partial_mask_division_code() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.PASSPORT_DIVISION_CODE, "123-456"))
    assert result == "***-***"


def test_partial_mask_date_masks_digits() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.BIRTH_DATE, "12.03.1990"))
    assert result == "**.**.****"


def test_partial_mask_short_value_without_digits_is_unchanged() -> None:
    strategy = PartialMaskStrategy()
    result = strategy.mask(_entity(PIIType.PHONE, "ab"))
    assert result == "ab"


# --- Engine-level tests ---------------------------------------------------


def test_reference_example_from_spec() -> None:
    from pathlib import Path

    from app.policies.loader import FilePolicyProvider

    policy = FilePolicyProvider(
        Path(__file__).resolve().parents[2] / "configs" / "consumers"
    ).get_policy("default")
    text = "Клиент Иванов Иван Иванович, паспорт 4509 123456"
    entities = [
        _entity(PIIType.PERSON_NAME, "Иванов Иван Иванович", start=7),
        _entity(PIIType.PASSPORT_NUMBER, "4509 123456", start=37),
    ]
    result = _engine().mask(text, entities, policy.masking).masked_text
    assert result == "Клиент ****** **** ********, паспорт **** ******"


def test_text_outside_spans_is_unchanged() -> None:
    text = "Привет, Иванов Иван, до встречи"
    entities = [_entity(PIIType.PERSON_NAME, "Иванов Иван", start=8)]
    assert _mask(text, entities) == "Привет, И. И., до встречи"


def test_pin_without_card_is_masked_by_default() -> None:
    text = "Пин-код 1234"
    entities = [_entity(PIIType.PIN, "1234", start=8)]
    assert _mask(text, entities) == "Пин-код ****"


def test_pin_with_card_is_masked() -> None:
    text = "Карта 4567 8901 2345 6756, пин 1234"
    entities = [
        _entity(PIIType.BANK_CARD, "4567 8901 2345 6756", start=6),
        _entity(PIIType.PIN, "1234", start=31),
    ]
    assert _mask(text, entities) == "Карта 45** **** **** **56, пин ****"


def test_overlapping_spans_keep_longest() -> None:
    text = "Иванов Иван Иванович"
    short = _entity(PIIType.PERSON_NAME, "Иванов", start=0)
    long = _entity(PIIType.PERSON_NAME, "Иванов Иван Иванович", start=0)
    assert _mask(text, [short, long]) == "И. И. И."


def test_invalid_spans_are_dropped() -> None:
    text = "Иванов Иван"
    bad_negative = _entity(PIIType.PERSON_NAME, "Иванов", start=-1)
    bad_past_end = _entity(PIIType.PERSON_NAME, "Иванов", start=100)
    bad_empty = _entity(PIIType.PERSON_NAME, "", start=0)
    assert _mask(text, [bad_negative, bad_past_end, bad_empty]) == "Иванов Иван"


def test_mismatched_span_is_dropped() -> None:
    text = "Иванов Иван"
    # value does not match text[start:end] -> span is dropped, text unchanged.
    bad = _entity(PIIType.PERSON_NAME, "Петров", start=0)
    assert _mask(text, [bad]) == "Иванов Иван"


def test_unknown_type_uses_default_partial_mask() -> None:
    text = "Код 123-456"
    entities = [_entity(PIIType.PASSPORT_ISSUER, "123-456", start=4)]
    # PASSPORT_ISSUER is not in the partial-mask format table -> full mask.
    assert _mask(text, entities) == "Код ***-***"


def test_mappings_recorded() -> None:
    text = "Иванов Иван"
    entities = [_entity(PIIType.PERSON_NAME, "Иванов Иван", start=0)]
    result = _engine().mask(
        text, entities, {PIIType.PERSON_NAME: MaskingStrategy.PARTIAL_MASK}
    )
    assert result.mappings == [("И. И.", "Иванов Иван")]


def _mask_with_rules(
    text: str, entities: list[PIIEntity], rules: list[dict[str, object]] | None
) -> str:
    strategy_map = {e.type: MaskingStrategy.PARTIAL_MASK for e in entities}
    return _engine().mask(text, entities, strategy_map, context_rules=rules).masked_text


def test_empty_context_rules_use_default() -> None:
    text = "Пин-код 1234"
    entities = [_entity(PIIType.PIN, "1234", start=8)]
    # Empty rules -> default (empty CONTEXT_REQUIRES) -> PIN masked always.
    assert _mask_with_rules(text, entities, []) == "Пин-код ****"


def test_context_rules_from_policy() -> None:
    text = "Карта 4567 8901 2345 6756, пин 1234"
    entities = [
        _entity(PIIType.BANK_CARD, "4567 8901 2345 6756", start=6),
        _entity(PIIType.PIN, "1234", start=31),
    ]
    rules = [{"type": "PIN", "requires": ["BANK_CARD"], "enabled": True}]
    assert _mask_with_rules(text, entities, rules) == "Карта 45** **** **** **56, пин ****"


def test_context_rule_requires_card_blocks_pin() -> None:
    text = "Пин-код 1234"
    entities = [_entity(PIIType.PIN, "1234", start=8)]
    rules = [{"type": "PIN", "requires": ["BANK_CARD"], "enabled": True}]
    # PIN requires BANK_CARD, which is absent -> PIN is not masked.
    assert _mask_with_rules(text, entities, rules) == "Пин-код 1234"


def test_context_rule_disabled_masks_always() -> None:
    text = "Пин-код 1234"
    entities = [_entity(PIIType.PIN, "1234", start=8)]
    rules = [{"type": "PIN", "requires": ["BANK_CARD"], "enabled": False}]
    assert _mask_with_rules(text, entities, rules) == "Пин-код ****"


def test_context_rule_unknown_type_is_skipped() -> None:
    text = "Пин-код 1234"
    entities = [_entity(PIIType.PIN, "1234", start=8)]
    rules = [{"type": "NOT_A_TYPE", "requires": ["BANK_CARD"], "enabled": True}]
    # Unknown rule type is skipped; no rules remain -> default applies.
    assert _mask_with_rules(text, entities, rules) == "Пин-код ****"


@pytest.mark.slow
def test_mask_large_text_is_fast() -> None:
    import time

    fragment = "Клиент Иванов Иван Иванович, карта 4567 8901 2345 6756. "
    repeats = 10_000
    text = fragment * repeats
    entities: list[PIIEntity] = []
    for i in range(repeats):
        base = i * len(fragment)
        entities.append(_entity(PIIType.PERSON_NAME, "Иванов Иван Иванович", start=base + 7))
        entities.append(_entity(PIIType.BANK_CARD, "4567 8901 2345 6756", start=base + 35))
    assert len(text) > 400_000
    start = time.perf_counter()
    _mask(text, entities)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0