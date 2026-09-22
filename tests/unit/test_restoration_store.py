"""Unit tests for the restoration store and TTL behaviour."""

from __future__ import annotations

import time

import pytest

from app.core.enums import RestorationStateStatus
from app.core.exceptions import ServiceOverloadedError
from app.restoration.memory import _SHARD_COUNT, InMemoryRestorationStore
from app.restoration.models import RestorationState


def _state(payload_id: str) -> RestorationState:
    return RestorationState(
        payload_id=payload_id,
        original_hash="hash",
        original_text="secret",
        masked_text="****",
        state=RestorationStateStatus.MASKED,
    )


def test_save_and_get() -> None:
    store = InMemoryRestorationStore()
    store.save("abc", _state("abc"))
    state = store.get("abc")
    assert state is not None
    assert state.original_text == "secret"


def test_get_missing_returns_none() -> None:
    store = InMemoryRestorationStore()
    assert store.get("missing") is None


def test_delete_removes_state() -> None:
    store = InMemoryRestorationStore()
    store.save("abc", _state("abc"))
    store.delete("abc")
    assert store.get("abc") is None


def test_ttl_expiry() -> None:
    store = InMemoryRestorationStore(ttl_seconds=1)
    store.save("abc", _state("abc"))
    assert store.get("abc") is not None
    time.sleep(1.1)
    assert store.get("abc") is None


def test_overflow_raises_service_overloaded() -> None:
    store = InMemoryRestorationStore(ttl_seconds=3600, max_entries=1)
    # Find two payload_ids that hash to the same shard.
    first = "a"
    store.save(first, _state(first))
    second = next(
        (
            f"id-{i}"
            for i in range(1000)
            if hash(f"id-{i}") % _SHARD_COUNT == hash(first) % _SHARD_COUNT
        ),
        None,
    )
    assert second is not None
    with pytest.raises(ServiceOverloadedError):
        store.save(second, _state(second))


def test_state_json_roundtrip() -> None:
    state = _state("abc")
    state.mappings = {"****": "secret"}
    state.entity_count = 1
    restored = RestorationState.from_json("abc", state.to_json())
    assert restored.payload_id == "abc"
    assert restored.original_text == "secret"
    assert restored.mappings == {"****": "secret"}
    assert restored.entity_count == 1
    assert restored.state == RestorationStateStatus.MASKED