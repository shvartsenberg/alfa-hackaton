"""Mock LLM adapter for the demonstration pipeline."""

from __future__ import annotations

from app.llm.base import LLMAdapter


class MockLLMAdapter(LLMAdapter):
    """Returns the input unchanged, simulating a no-op LLM."""

    def generate(self, prompt: str) -> str:
        return prompt