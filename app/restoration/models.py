"""Restoration state model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.enums import RestorationStateStatus


@dataclass(slots=True)
class RestorationState:
    """State required to restore an original text from its mask.

    The ``original_text`` and ``mappings`` fields are sensitive. They must
    never be logged or emitted to metrics.
    """

    payload_id: str
    original_hash: str
    original_text: str
    masked_text: str
    mappings: dict[str, str] = field(default_factory=dict)
    entity_count: int = 0
    state: RestorationStateStatus = RestorationStateStatus.MASKED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at