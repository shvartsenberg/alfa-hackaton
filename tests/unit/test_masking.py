"""Unit tests for masking strategies and the masking engine."""

from __future__ import annotations

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
    return _engine().mask(text, entities, {}).masked_text


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
    text = "Клиент Иванов Иван Иванович, паспорт 4509 123456"
    entities = [
        _entity(PIIType.PERSON_NAME, "Иванов Иван Иванович", start=7),
        _entity(PIIType.PASSPORT_NUMBER, "4509 123456", start=37),
    ]
    assert _mask(text, entities) == "Клиент И. И. И., паспорт 45** ****56"


def test_text_outside_spans_is_unchanged() -> None:
    text = "Привет, Иванов Иван, до встречи"
    entities = [_entity(PIIType.PERSON_NAME, "Иванов Иван", start=8)]
    assert _mask(text, entities) == "Привет, И. И., до встречи"


def test_pin_without_card_is_not_masked() -> None:
    text = "Пин-код 1234"
    entities = [_entity(PIIType.PIN, "1234", start=9)]
    assert _mask(text, entities) == "Пин-код 1234"


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


def test_unknown_type_uses_default_partial_mask() -> None:
    text = "Код 123-456"
    entities = [_entity(PIIType.PASSPORT_ISSUER, "123-456", start=4)]
    # PASSPORT_ISSUER is not in the partial-mask format table -> full mask.
    assert _mask(text, entities) == "Код ***-***"


def test_mappings_recorded() -> None:
    text = "Иванов Иван"
    entities = [_entity(PIIType.PERSON_NAME, "Иванов Иван", start=0)]
    result = _engine().mask(text, entities, {})
    assert result.mappings == {"И. И.": "Иванов Иван"}