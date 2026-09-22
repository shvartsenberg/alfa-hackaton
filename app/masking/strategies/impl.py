"""Masking strategy implementations.

Each strategy receives a ``PIIEntity`` (type + value) and returns the masked
replacement text. The exact output format depends on the PII type so that the
result matches the reference format from the task spec:

- FIO / cardholder -> initials "И. И. И."
- numeric types (passport, card, INN, driver license, phone) -> keep first 2
  and last 2 digits, mask the rest, keep separators
- division code -> digits to "*", keep the dash
- email -> first login char + "***" + "@domain"
- everything else (dates, CVV, PIN, addresses, ...) -> all letters/digits to "*"
"""

from __future__ import annotations

import re

from app.core.enums import PIIType
from app.core.models import PIIEntity
from app.masking.base import MaskingStrategy

_FULL_MASK_CHAR = "*"
# Types whose PARTIAL_MASK format is "initials": first letter of each word + dot.
_INITIALS_TYPES = frozenset({PIIType.PERSON_NAME, PIIType.CARDHOLDER_NAME})
# Types whose PARTIAL_MASK format keeps first 2 and last 2 digits.
_NUMERIC_EDGE_TYPES = frozenset(
    {
        PIIType.PASSPORT_NUMBER,
        PIIType.BANK_CARD,
        PIIType.INN,
        PIIType.DRIVER_LICENSE,
        PIIType.PHONE,
    }
)
# Types whose PARTIAL_MASK format masks all digits but keeps separators.
_DIGITS_ONLY_TYPES = frozenset({PIIType.PASSPORT_DIVISION_CODE})
_EMAIL_TYPES = frozenset({PIIType.EMAIL})

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_DIGIT_RE = re.compile(r"\d")
_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)


def _mask_all_alnum(value: str) -> str:
    """Replace every letter/digit with '*', keep separators and punctuation."""
    return _ALNUM_RE.sub(_FULL_MASK_CHAR, value)


def _mask_digits(value: str) -> str:
    """Replace every digit with '*', keep everything else."""
    return _DIGIT_RE.sub(_FULL_MASK_CHAR, value)


def _mask_numeric_edges(value: str) -> str:
    """Keep first 2 and last 2 digits, mask the rest, keep separators.

    Example: "4509 123456" -> "45** ****56", "4567 8901 2345 6756" -> "45** **** **** **56".
    """
    digits = _DIGIT_RE.findall(value)
    if len(digits) <= 4:
        return _mask_digits(value)
    first_two = "".join(digits[:2])
    last_two = "".join(digits[-2:])
    result: list[str] = []
    digit_index = 0
    total = len(digits)
    for ch in value:
        if ch.isdigit():
            if digit_index < 2 or digit_index >= total - 2:
                result.append(digits[digit_index])
            else:
                result.append(_FULL_MASK_CHAR)
            digit_index += 1
        else:
            result.append(ch)
    return "".join(result)


def _mask_initials(value: str) -> str:
    """Reduce each word to its first letter followed by a dot.

    Example: "Иванов Иван Иванович" -> "И. И. И."
    """
    words = _WORD_RE.findall(value)
    if not words:
        return value
    return " ".join(f"{word[0]}." for word in words)


def _mask_email(value: str) -> str:
    """Keep first login char, mask the rest of the login, keep the domain.

    Example: "test@example.com" -> "t***@example.com".
    """
    if "@" not in value:
        return _mask_all_alnum(value)
    login, _, domain = value.partition("@")
    if not login:
        return _FULL_MASK_CHAR + "@" + domain
    return login[0] + (_FULL_MASK_CHAR * 3) + "@" + domain


class FullMaskStrategy(MaskingStrategy):
    """Replaces all letters/digits with '*', keeps separators and punctuation."""

    def mask(self, entity: PIIEntity) -> str:
        return _mask_all_alnum(entity.value)


class PartialMaskStrategy(MaskingStrategy):
    """Applies the reference format from the task spec based on PII type."""

    def mask(self, entity: PIIEntity) -> str:
        pii_type = entity.type
        if pii_type in _INITIALS_TYPES:
            return _mask_initials(entity.value)
        if pii_type in _NUMERIC_EDGE_TYPES:
            return _mask_numeric_edges(entity.value)
        if pii_type in _DIGITS_ONLY_TYPES:
            return _mask_digits(entity.value)
        if pii_type in _EMAIL_TYPES:
            return _mask_email(entity.value)
        return _mask_all_alnum(entity.value)


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