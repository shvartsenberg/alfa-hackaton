"""Unit tests for the restoration store and TTL behaviour."""

from __future__ import annotations

import time

import pytest

from app.core.enums import RestorationStateStatus
from app.core.exceptions import ServiceOverloadedError
from app.restoration.memory import InMemoryRestorationStore
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


def test_full_store_rejects_new_state_without_losing_live_mapping() -> None:
    """A full store must preserve every live reversible mapping."""
    store = InMemoryRestorationStore(ttl_seconds=3600, max_entries=3)
    for i in range(3):
        store.save(f"k{i}", _state(f"k{i}"))
    assert len(store._data) == 3

    with pytest.raises(ServiceOverloadedError):
        store.save("k3", _state("k3"))

    assert len(store._data) == 3
    assert store.get("k0") is not None
    assert store.get("k3") is None
