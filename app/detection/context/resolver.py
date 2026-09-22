"""Context resolution for candidate PII entities.

A detected candidate is not necessarily personal data. The ContextResolver
applies configurable rules to decide whether a candidate should be treated
as protected PII. Rules are loaded from policy configuration, not hardcoded
here or in detectors.
"""

from __future__ import annotations

from app.core.models import DetectionContext, PIIEntity


class ContextResolver:
    """Resolves whether candidate entities are protected personal data.

    Extension point: implement rules such as "PIN alone is not masked, but
    PIN next to a BANK_CARD is". Rules should be driven by configuration.
    """

    def __init__(self, rules: list[dict[str, object]] | None = None) -> None:
        self._rules = rules or []

    def resolve(
        self, entities: list[PIIEntity], context: DetectionContext
    ) -> list[PIIEntity]:
        """Return the subset of entities considered protected PII.

        Initial commit: no rules are applied, so all candidates pass through.
        Future rules may drop or reclassify entities based on context.
        """
        return entities