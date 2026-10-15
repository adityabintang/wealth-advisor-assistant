"""Short-term (per-run) memory = the LangGraph state shared between agent nodes (PRD §13).

The state lives only for one run and is checkpointed in-memory by LangGraph (``InMemorySaver``),
which also makes the HITL interrupt/resume possible. Values are stored as JSON-compatible dicts
so checkpoints serialise cleanly; list fields use additive reducers so each node only appends.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Reducer: shallow-merge step records keyed by step name."""
    return {**(left or {}), **(right or {})}


class WorkflowState(TypedDict, total=False):
    # identity
    run_id: str
    client_id: str
    source_path: str | None
    started_at: str
    # per-step status: {step_name: StepRecord-dict}
    steps: Annotated[dict[str, dict[str, Any]], merge_dicts]
    # agent outputs (JSON dicts of the Pydantic models)
    context: dict[str, Any] | None
    report: dict[str, Any] | None
    draft: dict[str, Any] | None
    review: dict[str, Any] | None
    output: dict[str, Any] | None
    # shared scratchpad, append-only
    penalties: Annotated[list[dict[str, Any]], operator.add]
    degraded_reasons: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    decisions: Annotated[list[dict[str, Any]], operator.add]
    # control
    fatal_error: dict[str, Any] | None
    review_reasons: list[str]
