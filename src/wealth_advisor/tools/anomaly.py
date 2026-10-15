"""AnomalyDetectionTool: runs every configured detector and reports per-detector status."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..analytics.anomaly import DetectorInput, run_detectors
from ..config import Settings
from ..models import ClientFinancialData, PortfolioMetrics, RiskProfile
from .base import Tool


class AnomalyInput(BaseModel):
    client_data: ClientFinancialData
    metrics: PortfolioMetrics
    risk_profile: RiskProfile


class AnomalyDetectionTool(Tool[AnomalyInput]):
    name = "anomaly_detection"
    description = "Run rule-based and statistical anomaly detectors (unusual amounts, duplicates, spikes, outflows, concentration, risk deviation, decline)."
    input_schema = AnomalyInput

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _run(self, params: AnomalyInput, meta: dict[str, Any]) -> dict[str, Any]:
        rp = params.risk_profile
        max_equity = rp.max_equity_pct if rp.max_equity_pct is not None else self._settings.max_equity_for(rp.category)
        anomalies, statuses = run_detectors(
            DetectorInput(
                data=params.client_data,
                metrics=params.metrics,
                risk_profile=rp,
                max_equity_pct=max_equity,
                config=self._settings.anomaly,
            )
        )
        meta["findings"] = len(anomalies)
        return {
            "anomalies": [a.model_dump(mode="json") for a in anomalies],
            "detectors": [s.model_dump() for s in statuses],
        }

    def summarize_input(self, payload: Any) -> dict[str, Any]:
        data = payload.get("client_data") if isinstance(payload, dict) else None
        return {"client_id": getattr(getattr(data, "client", None), "client_id", None)}
