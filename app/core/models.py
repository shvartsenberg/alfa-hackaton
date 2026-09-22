"""Core domain models shared across the application."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import PIIType


@dataclass(slots=True)
class PIIEntity:
    """A detected personal data entity within a text span.

    NOTE: the ``value`` field is sensitive. It must never be logged or
    emitted to metrics. Use ``type`` and span positions for observability.
    """

    type: PIIType
    value: str
    start: int
    end: int
    confidence: float = 1.0
    detector: str = "unknown"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class DetectionResult:
    """Result of running the detection engine over a text."""

    entities: list[PIIEntity] = field(default_factory=list)
    processing_time: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class DetectionContext:
    """Contextual information passed to detectors."""

    consumer_id: str = "default"
    language: str = "ru"
    metadata: dict[str, object] = field(default_factory=dict)