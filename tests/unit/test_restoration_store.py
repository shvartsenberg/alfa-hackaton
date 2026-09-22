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


def test_expired_entries_evicted_on_save() -> None:
    store = InMemoryRestorationStore(ttl_seconds=0)
    store.save("expired", _state("expired"))
    assert "expired" in store._data
    store.save("fresh", _state("fresh"))
    assert "expired" not in store._data
    assert "fresh" in store._data


def test_original_not_stored_in_plaintext() -> None:
    store = InMemoryRestorationStore()
    state = _state("abc")
    store.save("abc", state)
    encrypted = store._data["abc"][1]
    assert b"secret" not in encrypted
    assert b"****" not in encrypted


def test_roundtrip_returns_original() -> None:
    store = InMemoryRestorationStore()
    state = _state("abc")
    store.save("abc", state)
    restored = store.get("abc")
    assert restored is not None
    assert restored.original_text == "secret"
    assert restored.masked_text == "****"
    assert restored.state == RestorationStateStatus.MASKED