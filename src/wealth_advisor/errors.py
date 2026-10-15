"""Exception types used across agents and the orchestrator.

Tools never raise to agents (they return ``ToolResult(ok=False)``); agents translate
failed tool results into these exceptions so the orchestrator can decide what to do.
"""

from __future__ import annotations


class WealthAdvisorError(Exception):
    """Base error."""

    code: str = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class FatalStepError(WealthAdvisorError):
    """Unrecoverable for this client (e.g. invalid JSON). The run ends in a controlled failure."""

    code = "fatal"


class TransientStepError(WealthAdvisorError):
    """Possibly recoverable; the orchestrator may retry the step."""

    code = "transient"


class ToolNotFoundError(WealthAdvisorError):
    code = "tool_not_found"


class ToolFailure(WealthAdvisorError):
    """Raised *inside* a tool implementation; converted to ``ToolResult(ok=False)`` by the base class."""

    def __init__(self, message: str, *, code: str = "tool_error", retryable: bool = False) -> None:
        super().__init__(message, code=code)
        self.retryable = retryable


class LLMUnavailableError(WealthAdvisorError):
    code = "llm_unavailable"
