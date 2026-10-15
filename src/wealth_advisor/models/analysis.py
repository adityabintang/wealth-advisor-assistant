"""Analysis artefacts: portfolio metrics, anomalies, client context, analysis report."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from .client import ClientFinancialData
from .common import ConfidencePenalty, DataQualityIssue, QuarantinedRecord
from .crm import CRMContext, RiskProfile

Severity = Literal["low", "medium", "high"]
AnomalyType = Literal[
    "unusual_amount",
    "duplicate_transaction",
    "frequency_spike",
    "large_outflow",
    "new_counterparty_high_value",
    "asset_concentration",
    "risk_profile_deviation",
    "value_decline",
]
CRMSource = Literal["live", "memory_cache", "none"]


class ClientContext(BaseModel):
    """Output of the Data Fetcher Agent."""

    client_id: str
    data: ClientFinancialData
    crm: CRMContext | None = None
    crm_source: CRMSource = "none"
    crm_fetched_at: str | None = None
    risk_profile: RiskProfile
    data_quality: list[DataQualityIssue] = Field(default_factory=list)
    quarantined: list[QuarantinedRecord] = Field(default_factory=list)
    degraded_reasons: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    penalties: list[ConfidencePenalty] = Field(default_factory=list)


class AllocationSlice(BaseModel):
    value: float
    weight: float


class CashFlow(BaseModel):
    window_days: int
    inflow: float
    outflow: float
    net: float


class ValueChange(BaseModel):
    window_days: int
    start_date: date
    end_date: date
    start_value: float
    end_value: float
    change_pct: float


class TopHolding(BaseModel):
    asset_id: str
    name: str | None
    asset_class: str
    sector: str
    value: float
    weight: float


class PortfolioMetrics(BaseModel):
    as_of: date
    base_currency: str
    total_assets: float
    cash_balance: float
    invested_value: float
    allocation_by_asset_class: dict[str, AllocationSlice]  # share of total assets
    allocation_by_sector: dict[str, AllocationSlice]  # share of invested value
    equity_weight: float
    top_holdings: list[TopHolding]
    max_single_asset_weight: float
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None
    cashflow_30d: CashFlow
    cashflow_90d: CashFlow
    value_change_90d: ValueChange | None = None
    transaction_count: int


class Anomaly(BaseModel):
    anomaly_id: str = ""
    type: AnomalyType
    severity: Severity
    title: str
    evidence: dict[str, Any]
    explanation: str
    fingerprint: str
    recurring: bool = False
    first_seen: str | None = None
    occurrences: int = 1
    previously_acknowledged: bool = False


class DetectorStatus(BaseModel):
    detector: str
    status: Literal["ran", "skipped", "failed"]
    findings: int = 0
    reason: str | None = None


class TrendInfo(BaseModel):
    previous_run_id: str
    previous_generated_at: str
    previous_total_assets: float | None = None
    total_assets_change_pct: float | None = None
    previous_confidence: float | None = None
    new_anomalies: list[str] = Field(default_factory=list)
    resolved_anomalies: list[str] = Field(default_factory=list)
    recurring_anomalies: list[str] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    """Output of the Analyzer Agent."""

    metrics: PortfolioMetrics
    anomalies: list[Anomaly] = Field(default_factory=list)
    detectors: list[DetectorStatus] = Field(default_factory=list)
    trend: TrendInfo | None = None
    warnings: list[str] = Field(default_factory=list)
    penalties: list[ConfidencePenalty] = Field(default_factory=list)
