"""LLM adapter interface.

The /process hot path must never call an LLM. This abstraction exists for the
demonstration production pipeline (SecureLLMPipeline).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMAdapter(ABC):
    """Interface for generating text from a prompt."""

    @abstractmethod
    def generate(self, prompt: str) -> str:
        raise NotImplementedError