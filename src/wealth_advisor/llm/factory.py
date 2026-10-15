"""Build an ``LLMProvider`` from configuration (LLM_PROVIDER / LLM_MODEL). No hard-coded default provider."""

from __future__ import annotations

import logging
from typing import Any

from ..config import LLMSettings
from ..errors import LLMUnavailableError
from ..logging_setup import log_event
from .base import LLMProvider


class LangChainChatProvider(LLMProvider):
    """Wraps any LangChain chat model created via ``init_chat_model`` (OpenAI, Anthropic, Gemini, Ollama, ...)."""

    def __init__(self, settings: LLMSettings) -> None:
        try:
            from langchain.chat_models import init_chat_model  # optional dependency
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise LLMUnavailableError("langchain is not installed; run `uv sync --extra llm --extra <provider>`") from exc
        self.name = f"{settings.provider}:{settings.model}"
        kwargs: dict[str, Any] = {"temperature": settings.temperature, "timeout": settings.timeout_seconds}
        try:
            self._model = init_chat_model(settings.model, model_provider=settings.provider, **kwargs)
        except Exception as exc:  # noqa: BLE001 - missing provider package / bad config
            raise LLMUnavailableError(f"cannot initialise {self.name}: {exc}") from exc

    def generate(self, system: str, prompt: str) -> str:
        try:
            response = self._model.invoke([("system", system), ("human", prompt)])
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailableError(f"{self.name} call failed: {exc}") from exc
        content = getattr(response, "content", response)
        if isinstance(content, list):  # some providers return content blocks
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        text = str(content).strip()
        if not text:
            raise LLMUnavailableError(f"{self.name} returned an empty response")
        return text


def create_llm_provider(settings: LLMSettings) -> LLMProvider | None:
    """Return a provider, or ``None`` when not configured/unavailable (callers use the template)."""
    if not settings.enabled:
        log_event("decision", "LLM not configured; narrative will use template", llm_enabled=False)
        return None
    try:
        provider = LangChainChatProvider(settings)
    except LLMUnavailableError as exc:
        log_event("decision", f"LLM unavailable, falling back to template: {exc}", level=logging.WARNING)
        return None
    log_event("decision", f"LLM narrative enabled ({provider.name})", llm_enabled=True)
    return provider
