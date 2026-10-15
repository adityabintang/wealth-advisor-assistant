"""Uniform Tool interface (PRD §8).

Every tool exposes ``name``, ``description``, ``input_schema`` and ``execute(input) -> ToolResult``.
``execute`` is a template method: it validates input, times the call, logs ``tool_called``
and converts *any* exception into ``ToolResult(ok=False)`` — tools never raise to agents.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, Field, ValidationError

from ..errors import ToolFailure
from ..logging_setup import log_event

InputT = TypeVar("InputT", bound=BaseModel)


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolMeta(BaseModel):
    tool: str
    duration_ms: float = 0.0
    retries: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: ToolError | None = None
    meta: ToolMeta

    model_config = {"arbitrary_types_allowed": True}


class Tool(ABC, Generic[InputT]):
    """Base class for all tools. Subclasses implement ``_run``."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]

    def execute(self, payload: dict[str, Any] | BaseModel | None = None) -> ToolResult:
        start = time.perf_counter()
        meta: dict[str, Any] = {"retries": 0}
        try:
            params = self._validate(payload)
            data = self._run(params, meta)
            result = ToolResult(ok=True, data=data, meta=self._meta(start, meta))
        except ToolFailure as exc:
            result = ToolResult(
                ok=False,
                error=ToolError(code=exc.code, message=exc.message, retryable=exc.retryable),
                meta=self._meta(start, meta),
            )
        except ValidationError as exc:
            result = ToolResult(
                ok=False,
                error=ToolError(code="invalid_input", message=_short_validation(exc)),
                meta=self._meta(start, meta),
            )
        except Exception as exc:  # noqa: BLE001 - tools must never raise to agents
            result = ToolResult(
                ok=False,
                error=ToolError(code="unexpected_error", message=f"{type(exc).__name__}: {exc}"),
                meta=self._meta(start, meta),
            )
        self._log(payload, result)
        return result

    # ------------------------------------------------------------------ helpers
    def _validate(self, payload: dict[str, Any] | BaseModel | None) -> InputT:
        if isinstance(payload, self.input_schema):
            return payload  # type: ignore[return-value]
        if isinstance(payload, BaseModel):
            payload = dict(payload)
        return self.input_schema.model_validate(payload or {})  # type: ignore[return-value]

    @abstractmethod
    def _run(self, params: InputT, meta: dict[str, Any]) -> Any:
        """Do the work. Raise ``ToolFailure`` for expected failures. ``meta`` may be updated."""

    def summarize_input(self, payload: Any) -> dict[str, Any]:
        """Compact, log-safe view of the input (override for large payloads)."""
        if isinstance(payload, dict):
            return {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool)) or v is None}
        return {}

    def _meta(self, start: float, meta: dict[str, Any]) -> ToolMeta:
        retries = int(meta.pop("retries", 0))
        return ToolMeta(
            tool=self.name,
            duration_ms=(time.perf_counter() - start) * 1000,
            retries=retries,
            extra=meta,
        )

    def _log(self, payload: Any, result: ToolResult) -> None:
        log_event(
            "tool_called",
            f"{self.name} -> {'ok' if result.ok else 'error:' + (result.error.code if result.error else '?')}",
            level=logging.DEBUG if result.ok else logging.WARNING,
            duration_ms=result.meta.duration_ms,
            tool=self.name,
            input=self.summarize_input(payload),
            ok=result.ok,
            retries=result.meta.retries,
            error=result.error.model_dump() if result.error else None,
        )


def _short_validation(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
    return "; ".join(parts)
