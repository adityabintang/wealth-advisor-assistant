"""MemoryTool: the only way agents/orchestrator touch long-term memory."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from ..errors import ToolFailure
from ..memory.long_term import LongTermMemory
from .base import Tool

Operation = Literal["get_history", "save_run", "get_crm", "save_crm", "record_decision"]


class MemoryInput(BaseModel):
    operation: Operation
    client_id: str
    run_id: str | None = None
    payload: dict[str, Any] | None = None


class MemoryTool(Tool[MemoryInput]):
    name = "memory"
    description = "Read/write long-term memory: client run history, anomaly history, advisor decisions, CRM cache."
    input_schema = MemoryInput

    def __init__(self, store: LongTermMemory) -> None:
        self._store = store

    def _run(self, params: MemoryInput, meta: dict[str, Any]) -> Any:
        op = params.operation
        if op == "get_history":
            return self._store.get_client_history(params.client_id)
        if op == "get_crm":
            return self._store.get_crm(params.client_id)
        if params.payload is None:
            raise ToolFailure(f"operation '{op}' requires a payload", code="invalid_input")
        if op == "save_crm":
            self._store.save_crm(params.client_id, params.payload)
        elif op == "save_run":
            self._store.save_run(params.payload)
        elif op == "record_decision":
            self._store.record_decision(params.run_id or "?", params.client_id, params.payload)
        return {"saved": True}

    def summarize_input(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return {"operation": payload.get("operation"), "client_id": payload.get("client_id")}
        return {}
