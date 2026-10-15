"""Recommender Agent: turn findings into prioritised, evidence-linked actions + a summary narrative."""

from __future__ import annotations

from typing import Any, Callable

from ..formatting import money, pct, signed_money
from ..models import AdvisoryDraft, AnalysisReport, Anomaly, ClientContext, Recommendation
from ..tools import NARRATIVE
from .base import Agent

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_DEMOTE = {"high": "medium", "medium": "low", "low": "low"}


def _ids(a: Anomaly) -> str:
    return ", ".join(a.evidence.get("txn_ids", [])[:5])


# One rule per anomaly type: (action, rationale). Kept as data so it is easy to review and extend.
_RULES: dict[str, Callable[[Anomaly, str], tuple[str, str]]] = {
    "unusual_amount": lambda a, c: (
        f"Verify transaction {_ids(a)} ({money(a.evidence['amount'], c)} with {a.evidence['counterparty']}) with the client.",
        a.explanation,
    ),
    "duplicate_transaction": lambda a, c: (
        f"Check possible duplicate payment ({_ids(a)}) and request a reversal if unintended.",
        a.explanation,
    ),
    "frequency_spike": lambda a, c: (
        f"Review the activity spike on {a.evidence['date']} for signs of account compromise or unusual behaviour.",
        a.explanation,
    ),
    "large_outflow": lambda a, c: (
        "Discuss the large recent outflow with the client and reassess liquidity needs and goals.",
        a.explanation,
    ),
    "new_counterparty_high_value": lambda a, c: (
        f"Perform enhanced due diligence on new counterparty '{a.evidence['counterparty']}'.",
        a.explanation,
    ),
    "asset_concentration": lambda a, c: (
        (
            f"Consider reducing exposure to {a.evidence.get('asset_id')} ({pct(a.evidence['weight'])}) toward the "
            f"{pct(a.evidence['threshold'], 0)} single-asset guideline."
            if a.evidence.get("scope") == "asset"
            else f"Consider diversifying away from the {a.evidence.get('sector')} sector ({pct(a.evidence['weight'])})."
        ),
        a.explanation,
    ),
    "risk_profile_deviation": lambda a, c: (
        (
            f"Rebalance equity from {pct(a.evidence['equity_weight'])} to within the {pct(a.evidence['max_equity_pct'], 0)} "
            f"{a.evidence['risk_category']} limit, or reassess the client's risk profile."
        ),
        a.explanation,
    ),
    "value_decline": lambda a, c: (
        "Review underperforming positions and discuss the recent drawdown with the client.",
        a.explanation,
    ),
}


class RecommenderAgent(Agent):
    name = "recommender"

    def run(self, ctx: ClientContext, report: AnalysisReport | None) -> AdvisoryDraft:
        recs = self._recommendations(ctx, report)
        template = self._template_summary(ctx, report)
        res = self.use(NARRATIVE, facts=self._facts(ctx, report), template_summary=template)
        if res.ok:
            summary, source = res.data["text"], res.data["source"]
        else:
            summary, source = template, "template"
        return AdvisoryDraft(summary=summary, narrative_source=source, recommendations=recs)

    # ------------------------------------------------------------------ recommendations
    def _recommendations(self, ctx: ClientContext, report: AnalysisReport | None) -> list[Recommendation]:
        ccy = ctx.data.base_currency
        items: list[dict[str, Any]] = []
        if report is None:
            items.append(dict(action="Perform a full manual analysis: automated analysis did not complete.",
                              rationale="Analyzer step failed; no metrics or anomaly findings are available.", priority="high", ids=[]))
        else:
            for a in report.anomalies:
                action, rationale = _RULES[a.type](a, ccy)
                priority = "high" if a.severity == "high" else "medium" if a.severity == "medium" else "low"
                if a.recurring and a.previously_acknowledged:
                    priority = _DEMOTE[priority]
                    rationale += f" Recurring since {a.first_seen} and previously acknowledged by the advisor; monitor."
                items.append(dict(action=action, rationale=rationale, priority=priority, ids=[a.anomaly_id]))

        if ctx.risk_profile.assumed:
            items.append(dict(action="Confirm the client's risk profile and update it in the CRM.",
                              rationale=ctx.assumptions[-1] if ctx.assumptions else "Risk profile assumed.", priority="high", ids=[]))
        if ctx.degraded_reasons:
            items.append(dict(action="Verify CRM context (goals, notes, relationship) manually before the client meeting.",
                              rationale="; ".join(ctx.degraded_reasons), priority="medium", ids=[]))
        if ctx.quarantined:
            items.append(dict(action=f"Fix {len(ctx.quarantined)} quarantined record(s) at the data source and re-run the analysis.",
                              rationale="; ".join(f"{q.record_id}: {q.reason}" for q in ctx.quarantined[:4]), priority="low", ids=[]))
        if report is not None and report.metrics.transaction_count < self.settings.anomaly.min_transactions_for_statistics:
            items.append(dict(action="Obtain complete transaction history; current data is insufficient for statistical checks.",
                              rationale=f"Only {report.metrics.transaction_count} transaction(s) available.", priority="medium", ids=[]))
        stale = self._stale_contact(ctx)
        if stale:
            items.append(dict(action="Schedule a review meeting with the client.", rationale=stale, priority="medium", ids=[]))
        if not items:
            items.append(dict(action="No corrective action required; maintain current strategy and keep the routine review cycle.",
                              rationale="No anomalies detected and data is complete.", priority="low", ids=[]))

        items.sort(key=lambda i: _PRIORITY_ORDER[i["priority"]])
        return [
            Recommendation(rec_id=f"R{n}", action=i["action"], rationale=i["rationale"], priority=i["priority"], linked_anomaly_ids=i["ids"])
            for n, i in enumerate(items, start=1)
        ]

    def _stale_contact(self, ctx: ClientContext) -> str | None:
        rel = ctx.crm.relationship if ctx.crm else None
        if not rel or not rel.last_contact_date:
            return None
        days = (ctx.data.as_of - rel.last_contact_date).days
        limit = self.settings.relationship.stale_contact_days
        if days > limit:
            return f"Last contact was {days} days ago ({rel.last_contact_date}); guideline is every {limit} days."
        return None

    # ------------------------------------------------------------------ narrative
    def _facts(self, ctx: ClientContext, report: AnalysisReport | None) -> dict[str, Any]:
        rel = ctx.crm.relationship if ctx.crm else None
        facts: dict[str, Any] = {
            "client": ctx.data.client.name,
            "tier": rel.tier if rel else None,
            "risk_profile": ctx.risk_profile.category,
            "risk_profile_assumed": ctx.risk_profile.assumed,
            "goals": [g.goal for g in (ctx.crm.goals if ctx.crm else [])],
            "data_limitations": ctx.degraded_reasons + [q.reason for q in ctx.quarantined][:5] + ctx.assumptions,
        }
        if report:
            m = report.metrics
            facts |= {
                "as_of": str(m.as_of),
                "total_assets": money(m.total_assets, m.base_currency),
                "allocation": {k: pct(v.weight) for k, v in m.allocation_by_asset_class.items()},
                "net_cash_flow_30d": signed_money(m.cashflow_30d.net, m.base_currency),
                "anomalies": [{"id": a.anomaly_id, "severity": a.severity, "title": a.title} for a in report.anomalies],
            }
        return facts

    def _template_summary(self, ctx: ClientContext, report: AnalysisReport | None) -> str:
        rel = ctx.crm.relationship if ctx.crm else None
        who = ctx.data.client.name
        profile = ("assumed " if ctx.risk_profile.assumed else "") + f"{ctx.risk_profile.category} risk profile"
        tier = f"{rel.tier} client, " if rel and rel.tier else ""
        parts = []
        if report is None:
            parts.append(f"{who} ({tier}{profile}): automated analysis did not complete, manual review required.")
        else:
            m = report.metrics
            ccy = m.base_currency
            alloc = sorted(m.allocation_by_asset_class.items(), key=lambda kv: -kv[1].weight)[:3]
            alloc_s = ", ".join(f"{k.replace('_', ' ')} {pct(v.weight, 0)}" for k, v in alloc) or "no allocation data"
            parts.append(f"{who} ({tier}{profile}) holds total assets of {money(m.total_assets, ccy)} as of {m.as_of} ({alloc_s}).")
            parts.append(f"Net cash flow over the last 30 days: {signed_money(m.cashflow_30d.net, ccy)}.")
            if m.value_change_90d:
                parts.append(f"Portfolio value changed {pct(m.value_change_90d.change_pct)} over 90 days.")
            if report.anomalies:
                high = sum(a.severity == "high" for a in report.anomalies)
                med = sum(a.severity == "medium" for a in report.anomalies)
                top = "; ".join(a.title for a in report.anomalies[:3])
                parts.append(f"{len(report.anomalies)} finding(s) flagged ({high} high, {med} medium): {top}.")
            else:
                parts.append("No anomalies detected.")
            if report.trend and report.trend.recurring_anomalies:
                parts.append(f"{len(report.trend.recurring_anomalies)} finding(s) also appeared in previous runs.")
        limits = []
        if ctx.degraded_reasons:
            limits.append("CRM context unavailable or cached")
        if ctx.quarantined:
            limits.append(f"{len(ctx.quarantined)} record(s) quarantined")
        if report is not None and report.metrics.transaction_count < self.settings.anomaly.min_transactions_for_statistics:
            limits.append("too few transactions for statistical checks")
        if limits:
            parts.append("Data limitations: " + "; ".join(limits) + ".")
        return " ".join(parts)
