"""NarrativeTool: optional LLM-written summary with a deterministic template fallback (FR-15).

The LLM only rephrases facts that were already computed; it never changes findings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

from ..llm.base import LLMProvider
from ..logging_setup import log_event
from .base import Tool

_SYSTEM = (
    "You are an assistant to a professional wealth advisor. Write a concise, neutral 3-4 sentence briefing "
    "in English using ONLY the facts provided. Do not invent numbers, do not give investment advice, "
    "and mention data limitations if any are listed."
)


class NarrativeInput(BaseModel):
    facts: dict[str, Any]
    template_summary: str


class NarrativeTool(Tool[NarrativeInput]):
    name = "narrative"
    description = "Write the advisor-facing summary via the configured LLM; falls back to the template summary."
    input_schema = NarrativeInput

    def __init__(self, llm: LLMProvider | None) -> None:
        self._llm = llm

    def _run(self, params: NarrativeInput, meta: dict[str, Any]) -> dict[str, Any]:
        if self._llm is None:
            return {"text": params.template_summary, "source": "template"}
        try:
            text = self._llm.generate(_SYSTEM, "Facts (JSON):\n" + json.dumps(params.facts, default=str, indent=2))
            meta["provider"] = self._llm.name
            return {"text": text, "source": "llm"}
        except Exception as exc:  # noqa: BLE001 - LLM failure must never affect findings
            log_event("fallback", f"LLM narrative failed, using template: {exc}", level=logging.WARNING, provider=self._llm.name)
            return {"text": params.template_summary, "source": "template", "llm_error": str(exc)}

    def summarize_input(self, payload: Any) -> dict[str, Any]:
        return {"llm": self._llm.name if self._llm else None}
