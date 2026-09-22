"""Detection engine: orchestrates detectors and context resolution."""

from __future__ import annotations

import time

from app.core.models import DetectionContext, DetectionResult, PIIEntity
from app.detection.base import PIIDetector
from app.detection.context.resolver import ContextResolver
from app.detection.resolution import resolve_spans


class DetectionEngine:
    """Runs all registered detectors and resolves candidates to PII.

    Pipeline: text -> candidate detection -> context resolution.
    """

    def __init__(
        self,
        detectors: list[PIIDetector],
        context_resolver: ContextResolver | None = None,
    ) -> None:
        self._detectors = detectors
        self._context_resolver = context_resolver or ContextResolver()

    def detect(self, text: str, context: DetectionContext) -> DetectionResult:
        started = time.perf_counter()
        candidates: list[PIIEntity] = []
        for detector in self._detectors:
            candidates.extend(detector.detect(text, context))
        resolved = resolve_spans(self._context_resolver.resolve(candidates, context))
        elapsed = time.perf_counter() - started
        return DetectionResult(
            entities=resolved,
            processing_time=elapsed,
            metadata={"detector_count": len(self._detectors)},
        )