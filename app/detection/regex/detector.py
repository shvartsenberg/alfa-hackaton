"""Regex-based detector for PII entities.

Each rule pairs a compiled pattern with an optional marker list and an
optional validator. A marker is a substring that must appear in a window
around the match; a validator is a boolean function applied to the match.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.core.enums import PIIType
from app.core.models import DetectionContext, PIIEntity
from app.detection.base import PIIDetector
from app.detection.validators import inn_checksum, is_birth_date, luhn

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s\-]?(?:\(\d{3}\)|\d{3})[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
_BANK_CARD_RE = re.compile(r"(?<!\d)(?:\d[\s\-]?){12,18}\d(?!\d)")
_INN_RE = re.compile(r"(?<!\d)\d{10}(?!\d)|(?<!\d)\d{12}(?!\d)")
_PASSPORT_NUMBER_RE = re.compile(r"(?<!\d)\d{4}\s?\d{6}(?!\d)")
_PASSPORT_DIVISION_CODE_RE = re.compile(r"(?<!\d)\d{3}-\d{3}(?!\d)")
_BIRTH_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{2}[./]\d{2}[./]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4})(?!\d)"
)
_CVV_RE = re.compile(r"(?<!\d)\d{3,4}(?!\d)")
_PIN_RE = re.compile(r"(?<!\d)\d{4}(?!\d)")

_MARKER_WINDOW = 40

_MARKERS: dict[PIIType, tuple[str, ...]] = {
    PIIType.PASSPORT_NUMBER: ("паспорт", "серия", "выдан"),
    PIIType.PASSPORT_DIVISION_CODE: ("код подразделения",),
    PIIType.BIRTH_DATE: ("г.р.", "дата рожд", "род."),
    PIIType.CVV: ("cvv", "cvc", "код проверки"),
    PIIType.PIN: ("pin", "пин"),
}

_VALIDATORS: dict[PIIType, Callable[[str], bool]] = {
    PIIType.BANK_CARD: luhn,
    PIIType.INN: inn_checksum,
    PIIType.BIRTH_DATE: is_birth_date,
}

_RULES: dict[PIIType, re.Pattern[str]] = {
    PIIType.EMAIL: _EMAIL_RE,
    PIIType.PHONE: _PHONE_RE,
    PIIType.BANK_CARD: _BANK_CARD_RE,
    PIIType.INN: _INN_RE,
    PIIType.PASSPORT_NUMBER: _PASSPORT_NUMBER_RE,
    PIIType.PASSPORT_DIVISION_CODE: _PASSPORT_DIVISION_CODE_RE,
    PIIType.BIRTH_DATE: _BIRTH_DATE_RE,
    PIIType.CVV: _CVV_RE,
    PIIType.PIN: _PIN_RE,
}


class RegexDetector(PIIDetector):
    """Detects PII candidates using compiled regular expressions."""

    name = "regex"

    def detect(self, text: str, context: DetectionContext) -> list[PIIEntity]:
        entities: list[PIIEntity] = []
        for pii_type, pattern in _RULES.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                validator = _VALIDATORS.get(pii_type)
                if validator is not None and not validator(value):
                    continue
                if not self._has_marker(text, match.start(), match.end(), pii_type):
                    continue
                entities.append(
                    PIIEntity(
                        type=pii_type,
                        value=value,
                        start=match.start(),
                        end=match.end(),
                        confidence=1.0,
                        detector=self.name,
                    )
                )
        return entities

    def _has_marker(
        self, text: str, start: int, end: int, pii_type: PIIType
    ) -> bool:
        markers = _MARKERS.get(pii_type)
        if markers is None:
            return True
        window = text[max(0, start - _MARKER_WINDOW) : end + _MARKER_WINDOW].lower()
        return any(marker in window for marker in markers)