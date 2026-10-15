"""Shared small models: data-quality flags, confidence penalties, step records, errors."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

StepStatus = Literal["pending", "running", "success", "failed", "skipped"]


class DataQualityIssue(BaseModel):
    """A data-quality observation. ``action`` says what the system did about it."""

    section: str  # client | accounts | holdings | transactions | history | crm
    record_id: str | None = None
    field: str | None = None
    issue: str
    action: Literal["defaulted", "normalized", "quarantined", "converted", "skipped", "noted"]


class QuarantinedRecord(BaseModel):
    section: str
    record_id: str | None = None
    reason: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ConfidencePenalty(BaseModel):
    reason: str
    amount: float
    source: str  # which step/agent applied it


class StepRecord(BaseModel):
    name: str
    status: StepStatus = "pending"
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    error: str | None = None


class ErrorInfo(BaseModel):
    code: str
    message: str
    step: str | None = None
