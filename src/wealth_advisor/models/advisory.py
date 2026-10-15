"""Recommender draft, HITL review and the final AdvisoryOutput schema (PRD §9)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .analysis import Anomaly, DetectorStatus, PortfolioMetrics, TrendInfo
from .common import ConfidencePenalty, DataQualityIssue, ErrorInfo, StepRecord

Priority = Literal["high", "medium", "low"]
ReviewStatus = Literal["approved", "edited", "rejected", "pending_review", "not_required"]
ReviewMode = Literal["interactive", "auto_approve", "non_interactive", "not_required"]

DISCLAIMER = (
    "Decision-support output only. This is not investment advice; the advisor makes the final decision."
)


class Recommendation(BaseModel):
    rec_id: str
    action: str
    rationale: str
    priority: Priority
    linked_anomaly_ids: list[str] = Field(default_factory=list)
    edited_by_reviewer: bool = False


class AdvisoryDraft(BaseModel):
    """Output of the Recommender Agent, before human review."""

    summary: str
    narrative_source: Literal["llm", "template", "fallback"] = "template"
    recommendations: list[Recommendation] = Field(default_factory=list)

    @classmethod
    def minimal(cls, reason: str) -> "AdvisoryDraft":
        """Safe draft used when the recommender step fails (partial result)."""
        return cls(
            summary=f"Automated recommendations unavailable ({reason}). Manual review required.",
            narrative_source="fallback",
            recommendations=[
                Recommendation(
                    rec_id="R1",
                    action="Perform a manual review of this client's data and findings before any advice.",
                    rationale=f"Recommendation step failed: {reason}",
                    priority="high",
                )
            ],
        )


class ReviewDecision(BaseModel):
    """What the reviewer (human or policy) decided at the HITL checkpoint."""

    action: Literal["approve", "edit", "reject", "pending"]
    reviewer: str = "advisor"
    mode: ReviewMode = "interactive"
    note: str | None = None
    removed_rec_ids: list[str] = Field(default_factory=list)
    modified_actions: dict[str, str] = Field(default_factory=dict)


class ReviewInfo(BaseModel):
    status: ReviewStatus
    required: bool
    trigger_reasons: list[str] = Field(default_factory=list)
    mode: ReviewMode
    reviewer: str | None = None
    reviewer_note: str | None = None
    decided_at: str | None = None
    changes: list[str] = Field(default_factory=list)


class AdvisoryOutput(BaseModel):
    """Final, schema-validated advisory output."""

    schema_version: str = "1.0"
    run_id: str
    client_id: str
    client_name: str
    generated_at: str
    as_of: date | None = None
    summary: str
    narrative_source: str
    metrics: PortfolioMetrics | None = None
    anomalies: list[Anomaly] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    confidence_breakdown: list[ConfidencePenalty] = Field(default_factory=list)
    degraded: bool
    degraded_reasons: list[str] = Field(default_factory=list)
    crm_source: str = "none"
    risk_profile: dict | None = None
    assumptions: list[str] = Field(default_factory=list)
    data_quality: list[DataQualityIssue] = Field(default_factory=list)
    detectors: list[DetectorStatus] = Field(default_factory=list)
    trend: TrendInfo | None = None
    review: ReviewInfo
    steps: list[StepRecord] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class RunResult(BaseModel):
    """What the orchestrator returns for one client: an output, or a controlled failure."""

    run_id: str
    client_id: str
    status: Literal["completed", "failed"]
    output: AdvisoryOutput | None = None
    error: ErrorInfo | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    duration_ms: float | None = None
