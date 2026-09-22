"""Restoration storage abstraction.

Business logic must not know whether the store is in-memory, Redis, or a
database. Swap implementations behind this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from app.restoration.models import RestorationState

TransitionFn = Callable[[RestorationState | None], RestorationState | None]


class RestorationStore(ABC):
    """Stores the state needed to demask a previously masked payload."""

    @abstractmethod
    def get(self, payload_id: str) -> RestorationState | None:
        raise NotImplementedError

    @abstractmethod
    def save(self, payload_id: str, state: RestorationState) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, payload_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def transition(
        self, payload_id: str, fn: TransitionFn
    ) -> RestorationState | None:
        """Atomically read the current state, apply ``fn``, and write the result.

        ``fn`` receives the current state (or ``None``) and returns the new
        state (or ``None`` to delete). The read-modify-write is atomic so that
        concurrent requests for the same ``payload_id`` cannot corrupt the
        lifecycle. Returns the resulting state.
        """
        raise NotImplementedError