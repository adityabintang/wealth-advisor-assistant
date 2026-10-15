"""Analyzer Agent: portfolio metrics + anomaly detection + comparison with long-term memory (FR-5, FR-6, FR-12)."""

from __future__ import annotations

import logging
from typing import Any

from ..errors import WealthAdvisorError
from ..logging_setup import log_event
from ..models import AnalysisReport, Anomaly, ClientContext, DetectorStatus, PortfolioMetrics, TrendInfo
from ..tools import ANOMALY, MEMORY, METRICS
from .base import Agent


class AnalyzerAgent(Agent):
    name = "analyzer"

    def run(self, ctx: ClientContext) -> AnalysisReport:
        res = self.use(METRICS, client_data=ctx.data)
        if not res.ok:
            assert res.error is not None
            raise WealthAdvisorError(f"metrics failed: {res.error.message}", code=res.error.code)
        metrics = PortfolioMetrics.model_validate(res.data)

        warnings: list[str] = []
        penalties = []
        res = self.use(ANOMALY, client_data=ctx.data, metrics=metrics, risk_profile=ctx.risk_profile)
        if res.ok:
            anomalies = [Anomaly.model_validate(a) for a in res.data["anomalies"]]
            detectors = [DetectorStatus.model_validate(d) for d in res.data["detectors"]]
        else:
            assert res.error is not None
            anomalies, detectors = [], [DetectorStatus(detector="all", status="failed", reason=res.error.message)]
            warnings.append(f"anomaly detection unavailable: {res.error.message}")

        penalties.extend(self._data_penalties(metrics, detectors))
        for a in anomalies:
            log_event(
                "anomaly_detected",
                f"{a.anomaly_id} [{a.severity}] {a.title}",
                level=logging.WARNING if a.severity == "high" else logging.INFO,
                anomaly_type=a.type,
                severity=a.severity,
                evidence_ids=a.evidence.get("txn_ids"),
            )

        trend = self._compare_with_history(ctx.client_id, metrics, anomalies)
        return AnalysisReport(
            metrics=metrics,
            anomalies=anomalies,
            detectors=detectors,
            trend=trend,
            warnings=warnings,
            penalties=[p for p in penalties if p is not None],
        )

    def _data_penalties(self, metrics: PortfolioMetrics, detectors: list[DetectorStatus]) -> list:
        out = []
        n = metrics.transaction_count
        stats_skipped = any(d.status == "skipped" and d.detector in ("unusual_amount", "frequency_spike") for d in detectors)
        if n == 0:
            out.append(self.penalty("no_transactions", "no transactions available"))
        elif stats_skipped and n < self.settings.anomaly.min_transactions_for_statistics:
            out.append(self.penalty("insufficient_transactions", f"only {n} transactions; statistical detectors skipped"))
        if metrics.value_change_90d is None:
            out.append(self.penalty("missing_history", "no 90-day portfolio history"))
        failed = [d.detector for d in detectors if d.status == "failed"]
        if failed:
            out.append(self.penalty("detector_failed", f"detector(s) failed: {', '.join(failed)}"))
        return out

    def _compare_with_history(self, client_id: str, metrics: PortfolioMetrics, anomalies: list[Anomaly]) -> TrendInfo | None:
        res = self.use(MEMORY, operation="get_history", client_id=client_id)
        if not res.ok or not res.data or not res.data.get("runs"):
            return None
        history: dict[str, Any] = res.data
        known = history["anomalies"]
        for a in anomalies:
            rec = known.get(a.fingerprint)
            if rec:
                a.recurring = True
                a.first_seen = rec["first_seen"]
                a.occurrences = int(rec["occurrences"]) + 1
                a.previously_acknowledged = bool(rec["acknowledged"])
        prev = history["runs"][0]
        prev_fps = set(prev["anomaly_fingerprints"])
        cur_fps = {a.fingerprint for a in anomalies}
        prev_total = prev.get("total_assets")
        change = (metrics.total_assets - prev_total) / prev_total if prev_total else None
        trend = TrendInfo(
            previous_run_id=prev["run_id"],
            previous_generated_at=prev["generated_at"],
            previous_total_assets=prev_total,
            total_assets_change_pct=round(change, 6) if change is not None else None,
            previous_confidence=prev.get("confidence"),
            new_anomalies=sorted(cur_fps - prev_fps),
            resolved_anomalies=sorted(prev_fps - cur_fps),
            recurring_anomalies=sorted(cur_fps & set(known)),
        )
        log_event(
            "memory_compare",
            f"compared with run {prev['run_id']}: {len(trend.new_anomalies)} new, {len(trend.recurring_anomalies)} recurring, "
            f"{len(trend.resolved_anomalies)} resolved",
        )
        return trend
