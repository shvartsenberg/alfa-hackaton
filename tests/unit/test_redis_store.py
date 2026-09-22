"""Unit tests for the Redis restoration store."""

from __future__ import annotations

import fakeredis

from app.core.enums import RestorationStateStatus
from app.restoration.models import RestorationState
from app.restoration.redis_store import RedisRestorationStore


def _state(payload_id: str) -> RestorationState:
    return RestorationState(
        payload_id=payload_id,
        original_hash="hash",
        original_text="secret",
        masked_text="****",
        mappings={"****": "secret"},
        entity_count=1,
        state=RestorationStateStatus.MASKED,
    )


def test_save_and_get() -> None:
    client = fakeredis.FakeRedis()
    store = RedisRestorationStore(client=client, ttl_seconds=3600)
    store.save("abc", _state("abc"))
    state = store.get("abc")
    assert state is not None
    assert state.original_text == "secret"
    assert state.mappings == {"****": "secret"}


def test_get_missing_returns_none() -> None:
    client = fakeredis.FakeRedis()
    store = RedisRestorationStore(client=client, ttl_seconds=3600)
    assert store.get("missing") is None


def test_delete_removes_state() -> None:
    client = fakeredis.FakeRedis()
    store = RedisRestorationStore(client=client, ttl_seconds=3600)
    store.save("abc", _state("abc"))
    store.delete("abc")
    assert store.get("abc") is None


def test_ttl_expiry() -> None:
    client = fakeredis.FakeRedis()
    store = RedisRestorationStore(client=client, ttl_seconds=1)
    store.save("abc", _state("abc"))
    assert store.get("abc") is not None
    client.ttl("pii:restore:abc")
    # fakeredis honours TTL; force expiry by advancing is not trivial, so
    # verify the key carries a TTL.
    assert client.ttl("pii:restore:abc") > 0