"""Secure LLM pipeline: demonstration production flow.

Consumer -> detect -> mask -> LLM -> demask -> Consumer.

No public endpoint is required for this on the initial commit; it exists as
an architectural demonstration of how the proxy sits between a consumer and
an LLM.
"""

from __future__ import annotations

from app.core.models import DetectionContext
from app.detection.engine import DetectionEngine
from app.llm.base import LLMAdapter
from app.masking.engine import MaskingEngine
from app.policies.models import ConsumerPolicy


class SecureLLMPipeline:
    """Runs a masked prompt through an LLM and restores the response."""

    def __init__(
        self,
        detection_engine: DetectionEngine,
        masking_engine: MaskingEngine,
        llm: LLMAdapter,
    ) -> None:
        self._detection_engine = detection_engine
        self._masking_engine = masking_engine
        self._llm = llm

    def run(self, text: str, policy: ConsumerPolicy) -> str:
        context = DetectionContext(consumer_id=policy.consumer_id)
        detection = self._detection_engine.detect(text, context)
        entities = [e for e in detection.entities if e.type in policy.enabled_types]
        masking = self._masking_engine.mask(text, entities, policy.masking)
        llm_output = self._llm.generate(masking.masked_text)
        restored = llm_output
        for replacement, original in masking.mappings:
            restored = restored.replace(replacement, original)
        return restored