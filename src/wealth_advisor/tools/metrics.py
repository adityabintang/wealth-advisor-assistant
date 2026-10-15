"""PortfolioMetricsTool: allocation, concentration, cash flow and value change."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..analytics.metrics import compute_metrics
from ..config import Settings
from ..models import ClientFinancialData
from .base import Tool


class MetricsInput(BaseModel):
    client_data: ClientFinancialData


class PortfolioMetricsTool(Tool[MetricsInput]):
    name = "portfolio_metrics"
    description = "Compute total assets, allocation by asset class/sector, concentration, 30/90-day cash flow and value change."
    input_schema = MetricsInput

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _run(self, params: MetricsInput, meta: dict[str, Any]) -> dict[str, Any]:
        metrics = compute_metrics(params.client_data, self._settings.anomaly.value_decline.window_days)
        return metrics.model_dump(mode="json")

    def summarize_input(self, payload: Any) -> dict[str, Any]:
        data = payload.get("client_data") if isinstance(payload, dict) else None
        return {"client_id": getattr(getattr(data, "client", None), "client_id", None)}
