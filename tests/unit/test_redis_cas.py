"""Unit tests for CAS retry exhaustion in the Redis restoration store."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import redis

from app.core.exceptions import ServiceOverloadedError
from app.restoration.redis_store import RedisRestorationStore

_MASKING_KEY = "IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA="


def _always_watch_error_pipeline() -> MagicMock:
    """A pipeline whose ``watch`` always raises WatchError (CAS never wins)."""
    pipe = MagicMock()
    pipe.watch.side_effect = redis.WatchError
    pipe.__enter__.return_value = pipe
    return pipe


def test_cas_exhaustion_raises_service_overloaded() -> None:
    client = MagicMock()
    client.pipeline.return_value = _always_watch_error_pipeline()
    store = RedisRestorationStore(client=client, masking_key=_MASKING_KEY)

    with pytest.raises(ServiceOverloadedError) as exc_info:
        store.transition("abc", lambda current: current)

    assert exc_info.value.retry_after == 1
    # The store must never surface a 500-class ProcessingError on contention.
    assert exc_info.value.status_code == 429


def test_cas_retries_are_bounded() -> None:
    client = MagicMock()
    client.pipeline.return_value = _always_watch_error_pipeline()
    store = RedisRestorationStore(client=client, masking_key=_MASKING_KEY)

    with pytest.raises(ServiceOverloadedError):
        store.transition("abc", lambda current: current)

    # Exactly _MAX_CAS_RETRIES attempts, then it gives up.
    assert client.pipeline.call_count == 5