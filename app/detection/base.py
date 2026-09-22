"""Base interfaces for the detection layer."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.models import DetectionContext, PIIEntity


class PIIDetector(ABC):
    """Interface for a detector that finds candidate PII spans in text.

    Detectors produce *candidates*. Whether a candidate is actually personal
    data is resolved later by the context resolver and policy engine.
    """

    name: str = "base"

    @abstractmethod
    def detect(self, text: str, context: DetectionContext) -> list[PIIEntity]:
        """Return candidate PII entities found in ``text``."""
        raise NotImplementedError


class ChunkingStrategy(ABC):
    """Extension point for splitting large texts before detection.

    Not implemented in the initial commit. Future work: chunking for texts up
    to 100k tokens, then merging spans across chunk boundaries.
    """

    @abstractmethod
    def chunk(self, text: str) -> list[str]:
        raise NotImplementedError