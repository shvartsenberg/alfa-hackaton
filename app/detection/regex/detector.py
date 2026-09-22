"""Regex-based detector for demonstration purposes.

Initial commit supports EMAIL and PHONE only. The structure allows adding
more regex patterns without touching the pipeline.
"""

from __future__ import annotations

import re

from app.core.enums import PIIType
from app.core.models import DetectionContext, PIIEntity
from app.detection.base import PIIDetector

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s\-]?(?:\(\d{3}\)|\d{3})[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)


class RegexDetector(PIIDetector):
    """Detects PII candidates using compiled regular expressions."""

    name = "regex"

    def __init__(self) -> None:
        self._patterns: dict[PIIType, re.Pattern[str]] = {
            PIIType.EMAIL: _EMAIL_RE,
            PIIType.PHONE: _PHONE_RE,
        }

    def detect(self, text: str, context: DetectionContext) -> list[PIIEntity]:
        entities: list[PIIEntity] = []
        for pii_type, pattern in self._patterns.items():
            for match in pattern.finditer(text):
                entities.append(
                    PIIEntity(
                        type=pii_type,
                        value=match.group(0),
                        start=match.start(),
                        end=match.end(),
                        confidence=1.0,
                        detector=self.name,
                    )
                )
        return entities