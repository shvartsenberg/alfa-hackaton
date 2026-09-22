"""Unit tests for the regex detector."""

from __future__ import annotations

from app.core.enums import PIIType
from app.core.models import DetectionContext
from app.detection.regex.detector import RegexDetector


def test_detects_email() -> None:
    detector = RegexDetector()
    entities = detector.detect(
        "Contact test@example.com please", DetectionContext()
    )
    assert len(entities) == 1
    assert entities[0].type == PIIType.EMAIL
    assert entities[0].value == "test@example.com"
    assert entities[0].start == 8
    assert entities[0].end == 24


def test_detects_phone() -> None:
    detector = RegexDetector()
    entities = detector.detect(
        "Call +7 999 123-45-67 now", DetectionContext()
    )
    assert len(entities) == 1
    assert entities[0].type == PIIType.PHONE
    assert entities[0].value == "+7 999 123-45-67"


def test_detects_multiple_entities() -> None:
    detector = RegexDetector()
    entities = detector.detect(
        "Email a@b.com and phone +7 999 123-45-67", DetectionContext()
    )
    assert len(entities) == 2
    assert {e.type for e in entities} == {PIIType.EMAIL, PIIType.PHONE}


def test_no_false_positive_on_plain_text() -> None:
    detector = RegexDetector()
    entities = detector.detect("Hello world, nothing here", DetectionContext())
    assert entities == []