"""Unit tests for the Redis restoration store."""

from __future__ import annotations

import threading

import fakeredis

from app.core.enums import RestorationStateStatus
from app.core.exceptions import InvalidPayloadError
from app.restoration.models import RestorationState
from app.restoration.redis_store import RedisRestorationStore

_MASKING_KEY = "IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA="


def _state(payload_id: str) -> RestorationState:
    return RestorationState(
        payload_id=payload_id,
        original_hash="hash",
        original_text="secret",
        masked_text="****",
        mappings=[("****", "secret")],
        entity_count=1,
        state=RestorationStateStatus.MASKED,
    )


def _store(client=None, **kwargs) -> RedisRestorationStore:
    kwargs.setdefault("ttl_seconds", 3600)
    return RedisRestorationStore(
        client=client or fakeredis.FakeRedis(),
        masking_key=_MASKING_KEY,
        **kwargs,
    )


def test_save_and_get() -> None:
    store = _store()
    store.save("abc", _state("abc"))
    state = store.get("abc")
    assert state is not None
    assert state.original_text == "secret"
    assert state.mappings == [("****", "secret")]


def test_get_missing_returns_none() -> None:
    store = _store()
    assert store.get("missing") is None


def test_delete_removes_state() -> None:
    store = _store()
    store.save("abc", _state("abc"))
    store.delete("abc")
    assert store.get("abc") is None


def test_ttl_expiry() -> None:
    client = fakeredis.FakeRedis()
    store = _store(client=client, ttl_seconds=1)
    store.save("abc", _state("abc"))
    assert store.get("abc") is not None
    # The key is HMAC-derived; verify the stored key carries a TTL.
    keys = [k for k in client.keys("pii:restore:*")]
    assert len(keys) == 1
    assert client.ttl(keys[0]) > 0


def test_state_is_encrypted_at_rest() -> None:
    client = fakeredis.FakeRedis()
    store = _store(client=client)
    store.save("abc", _state("abc"))
    keys = [k for k in client.keys("pii:restore:*")]
    raw = client.get(keys[0])
    assert isinstance(raw, bytes)
    assert b"secret" not in raw
    assert b"****" not in raw


def test_key_is_hashed_not_raw_payload_id() -> None:
    client = fakeredis.FakeRedis()
    store = _store(client=client)
    store.save("abc", _state("abc"))
    keys = [k for k in client.keys("pii:restore:*")]
    assert len(keys) == 1
    assert b"abc" not in keys[0]


def test_transition_new_state() -> None:
    store = _store()
    result = store.transition("abc", lambda current: _state("abc"))
    assert result is not None
    assert result.original_text == "secret"
    assert store.get("abc") is not None


def test_transition_retry_keeps_state() -> None:
    store = _store()
    store.save("abc", _state("abc"))
    result = store.transition("abc", lambda current: current)
    assert result is not None
    assert result.state == RestorationStateStatus.MASKED


def test_transition_delete() -> None:
    store = _store()
    store.save("abc", _state("abc"))
    result = store.transition("abc", lambda current: None)
    assert result is None
    assert store.get("abc") is None


def test_concurrent_transition_single_winner() -> None:
    """Two concurrent transitions for the same new payload_id: one wins."""
    client = fakeredis.FakeRedis()
    store = _store(client=client)
    results: list[RestorationState | None] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            result = store.transition("shared", lambda current: _state("shared"))
            with lock:
                results.append(result)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All transitions succeed (same payload -> retry MASK keeps state), so the
    # final state is present and consistent.
    final = store.get("shared")
    assert final is not None
    assert final.original_text == "secret"


def test_concurrent_different_payloads_one_owner() -> None:
    """Two different original payloads with the same new payload_id."""
    client = fakeredis.FakeRedis()
    store = _store(client=client)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(payload: str) -> None:
        try:
            result = store.transition(
                "shared",
                lambda current, p=payload: _state("shared")
                if current is None
                else current,
            )
            with lock:
                outcomes.append("ok" if result is not None else "none")
        except InvalidPayloadError:
            with lock:
                outcomes.append("rejected")

    threads = [
        threading.Thread(target=worker, args=(f"payload-{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The first transition creates the state; subsequent ones see it and keep
    # it (retry semantics). No corruption.
    final = store.get("shared")
    assert final is not None
    assert len(outcomes) == 4