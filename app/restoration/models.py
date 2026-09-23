"""Restoration state model."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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
    mappings: list[tuple[str, str]] = field(default_factory=list)
    entity_count: int = 0
    detected_types: list[str] = field(default_factory=list)
    state: RestorationStateStatus = RestorationStateStatus.MASKED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    consumer_id: str = ""

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    def to_json(self) -> str:
        """Serialize to JSON for storage backends."""
        data = asdict(self)
        data["state"] = self.state.value
        data["created_at"] = self.created_at.isoformat()
        data["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return json.dumps(data)

    @classmethod
    def from_json(cls, payload_id: str, raw: str) -> RestorationState:
        """Deserialize from JSON produced by :meth:`to_json`."""
        data = json.loads(raw)
        data["state"] = RestorationStateStatus(data["state"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        if data.get("expires_at"):
            data["expires_at"] = datetime.fromisoformat(data["expires_at"])
        data["payload_id"] = payload_id
        # Backwards compatibility: older records may lack consumer_id.
        data.setdefault("consumer_id", "")
        # JSON has no tuples; restore mappings as (replacement, original) pairs.
        data["mappings"] = [tuple(pair) for pair in data.get("mappings", [])]
        return cls(**data)