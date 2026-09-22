"""Restoration storage abstraction.

Business logic must not know whether the store is in-memory, Redis, or a
database. Swap implementations behind this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.restoration.models import RestorationState


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