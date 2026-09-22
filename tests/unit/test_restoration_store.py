"""Unit tests for the restoration store and TTL behaviour."""

from __future__ import annotations

import time

from app.core.enums import RestorationStateStatus
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