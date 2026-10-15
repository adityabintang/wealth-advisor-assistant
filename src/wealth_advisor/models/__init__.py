"""Pydantic v2 schemas for every artefact exchanged between agents."""

from .advisory import (
    DISCLAIMER,
    AdvisoryDraft,
    AdvisoryOutput,
    Recommendation,
    ReviewDecision,
    ReviewInfo,
    RunResult,
)
from .analysis import (
    AllocationSlice,
    AnalysisReport,
    Anomaly,
    CashFlow,
    ClientContext,
    DetectorStatus,
    PortfolioMetrics,
    TopHolding,
    TrendInfo,
    ValueChange,
)
from .client import Account, ClientFinancialData, ClientInfo, Holding, PortfolioSnapshot, Transaction
from .common import ConfidencePenalty, DataQualityIssue, ErrorInfo, QuarantinedRecord, StepRecord
from .crm import CRMContext, Goal, Note, Relationship, RiskProfile

__all__ = [
    "DISCLAIMER",
    "Account",
    "AdvisoryDraft",
    "AdvisoryOutput",
    "AllocationSlice",
    "AnalysisReport",
    "Anomaly",
    "CRMContext",
    "CashFlow",
    "ClientContext",
    "ClientFinancialData",
    "ClientInfo",
    "ConfidencePenalty",
    "DataQualityIssue",
    "DetectorStatus",
    "ErrorInfo",
    "Goal",
    "Holding",
    "Note",
    "PortfolioMetrics",
    "PortfolioSnapshot",
    "QuarantinedRecord",
    "Recommendation",
    "Relationship",
    "ReviewDecision",
    "ReviewInfo",
    "RiskProfile",
    "RunResult",
    "StepRecord",
    "TopHolding",
    "Transaction",
    "TrendInfo",
    "ValueChange",
]
