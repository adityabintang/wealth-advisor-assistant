"""Agent base class. Agents are plain Python: no LangGraph import, no tool implementation import.

They receive a ``ToolRegistry`` and call tools by name, which keeps them swappable and testable
outside the graph.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, ClassVar

from ..config import Settings
from ..logging_setup import bind_context
from ..models import ConfidencePenalty
from ..tools import ToolRegistry, ToolResult


class Agent(ABC):
    name: ClassVar[str] = "agent"

    def __init__(self, tools: ToolRegistry, settings: Settings) -> None:
        self.tools = tools
        self.settings = settings

    def use(self, tool_name: str, **payload: Any) -> ToolResult:
        """Invoke a tool by registry name (logs are tagged with this agent)."""
        with bind_context(agent=self.name):
            return self.tools.get(tool_name).execute(payload)

    def penalty(self, key: str, reason: str, *, multiplier: float = 1.0, cap_key: str | None = None) -> ConfidencePenalty | None:
        """Build a confidence penalty from configuration (``None`` if it rounds to zero)."""
        amount = self.settings.confidence.penalty(key) * multiplier
        if cap_key:
            amount = min(amount, self.settings.confidence.penalty(cap_key, amount))
        amount = round(amount, 4)
        return ConfidencePenalty(reason=reason, amount=amount, source=self.name) if amount > 0 else None
