"""YAML-based policy loader and provider."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core.enums import MaskingStrategy, PIIType
from app.core.exceptions import ConsumerNotAllowedError
from app.policies.models import ConsumerPolicy
from app.policies.provider import PolicyProvider


class FilePolicyProvider(PolicyProvider):
    """Loads consumer policies from YAML files in a directory."""

    def __init__(self, consumers_dir: Path) -> None:
        self._consumers_dir = consumers_dir
        self._policies: dict[str, ConsumerPolicy] = {}
        self._load_all()

    def _load_all(self) -> None:
        for path in sorted(self._consumers_dir.glob("*.yaml")):
            policy = self._load_file(path)
            self._policies[policy.consumer_id] = policy

    def _load_file(self, path: Path) -> ConsumerPolicy:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        consumer_id = path.stem
        detection = raw.get("detection", {})
        masking_raw = raw.get("masking", {})
        demasking = raw.get("demasking", {})
        return ConsumerPolicy(
            consumer_id=consumer_id,
            enabled=bool(raw.get("enabled", True)),
            enabled_types=[PIIType(t) for t in detection.get("enabled_types", [])],
            masking={
                PIIType(k): MaskingStrategy(v) for k, v in masking_raw.items()
            },
            demasking_enabled=bool(demasking.get("enabled", True)),
            context_rules=raw.get("context_rules", []),
        )

    def get_policy(self, consumer_id: str) -> ConsumerPolicy:
        policy = self._policies.get(consumer_id)
        if policy is None or not policy.enabled:
            raise ConsumerNotAllowedError(f"consumer not allowed: {consumer_id}")
        return policy