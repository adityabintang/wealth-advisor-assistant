"""Provider-agnostic LLM interface. Agents only know ``LLMProvider``."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Minimal text-generation contract. Implementations must raise on failure (callers fall back)."""

    name: str = "llm"

    @abstractmethod
    def generate(self, system: str, prompt: str) -> str:
        """Return the model's text response."""
