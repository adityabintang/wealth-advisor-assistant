"""Advisor-facing report. The same rich renderables feed the coloured console and the .txt file."""

from __future__ import annotations

import io
from typing import Any, Iterable

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .formatting import money, pct, signed_money
from .models import AdvisoryOutput, RunResult

SEV_STYLE = {"high": "bold red", "medium": "yellow", "low": "cyan"}
PRIO_STYLE = SEV_STYLE
REVIEW_STYLE = {"approved": "green", "edited": "green", "not_required": "green", "rejected": "bold red", "pending_review": "bold yellow"}


def _header(o: AdvisoryOutput) -> Panel:
    conf_style = "green" if o.confidence >= 0.8 else "yellow" if o.confidence >= 0.6 else "red"
    t = Text()
    t.append(f"{o.client_name} ({o.client_id})", style="bold")
    t.append(f"   as of {o.as_of}   run {o.run_id}\n")
    t.append("Confidence: ")
    t.append(f"{o.confidence:.2f}", style=conf_style)
    t.append("   Degraded: ")
    t.append("YES" if o.degraded else "no", style="bold red" if o.degraded else "green")
    t.append("   CRM: ")
    t.append(o.crm_source)
    t.append("   Review: ")
    t.append(o.review.status.upper(), style=REVIEW_STYLE.get(o.review.status, ""))
    t.append(f" ({o.review.mode})")
    rp = o.risk_profile or {}
    t.append(f"\nRisk profile: {rp.get('category')}" + (" (ASSUMED)" if rp.get("assumed") else ""))
    if rp.get("max_equity_pct") is not None:
        t.append(f", max equity {pct(rp['max_equity_pct'], 0)}")
    t.append(f"   Narrative: {o.narrative_source}")
    return Panel(t, title="Wealth Advisor Assistant - Advisory Brief", box=box.ROUNDED)


def _metrics(o: AdvisoryOutput) -> RenderableType:
    m = o.metrics
    if m is None:
        return Text("Metrics unavailable (analysis step did not complete).", style="red")
    c = m.base_currency
    t = Table(title="Portfolio metrics", box=box.SIMPLE_HEAVY, show_header=False)
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    t.add_row("Total assets", money(m.total_assets, c))
    t.add_row("Cash / invested", f"{money(m.cash_balance, c)} / {money(m.invested_value, c)}")
    t.add_row("Equity weight", pct(m.equity_weight))
    t.add_row("Largest single holding", pct(m.max_single_asset_weight) + (f" ({m.top_holdings[0].asset_id})" if m.top_holdings else ""))
    t.add_row("Net cash flow 30d", signed_money(m.cashflow_30d.net, c))
    t.add_row("Net cash flow 90d", signed_money(m.cashflow_90d.net, c))
    t.add_row("Value change 90d", pct(m.value_change_90d.change_pct) if m.value_change_90d else "n/a (no history)")
    t.add_row("Unrealised P&L", f"{signed_money(m.unrealized_pnl, c)} ({pct(m.unrealized_pnl_pct)})" if m.unrealized_pnl is not None else "n/a")
    t.add_row("Transactions analysed", str(m.transaction_count))

    a = Table(title="Allocation (share of total assets)", box=box.SIMPLE_HEAVY)
    a.add_column("Asset class")
    a.add_column("Value", justify="right")
    a.add_column("Weight", justify="right")
    for k, v in sorted(m.allocation_by_asset_class.items(), key=lambda kv: -kv[1].weight):
        a.add_row(k, money(v.value, c), pct(v.weight))
    return Group(t, a)


def _anomalies(o: AdvisoryOutput) -> RenderableType:
    if not o.anomalies:
        return Text("No anomalies detected.", style="green")
    t = Table(title=f"Anomalies ({len(o.anomalies)})", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("ID")
    t.add_column("Severity")
    t.add_column("Finding")
    t.add_column("Evidence / explanation", overflow="fold")
    for an in o.anomalies:
        tag = " [recurring]" if an.recurring else ""
        t.add_row(an.anomaly_id, Text(an.severity.upper(), style=SEV_STYLE[an.severity]), an.title + tag, an.explanation)
    return t


def _recommendations(o: AdvisoryOutput) -> RenderableType:
    t = Table(title="Recommended actions", box=box.SIMPLE_HEAVY)
    t.add_column("ID")
    t.add_column("Priority")
    t.add_column("Action", overflow="fold")
    t.add_column("Linked")
    for r in o.recommendations:
        t.add_row(r.rec_id, Text(r.priority.upper(), style=PRIO_STYLE[r.priority]), r.action + (" (edited)" if r.edited_by_reviewer else ""), ", ".join(r.linked_anomaly_ids) or "-")
    return t


def _bullets(title: str, items: Iterable[str], style: str = "") -> RenderableType | None:
    items = list(items)
    if not items:
        return None
    text = Text(f"{title}\n", style="bold")
    for i in items:
        text.append(f"  - {i}\n", style=style)
    return text


def _audit(o: AdvisoryOutput) -> RenderableType:
    t = Table(title="Execution trace", box=box.SIMPLE)
    for col in ("Step", "Status", "Attempts", "Duration", "Error"):
        t.add_column(col)
    style = {"success": "green", "failed": "red", "skipped": "dim", "pending": "yellow"}
    for s in o.steps:
        t.add_row(s.name, Text(s.status, style=style.get(s.status, "")), str(s.attempts), f"{s.duration_ms:.0f} ms" if s.duration_ms else "-", s.error or "")
    return t


def build_report(o: AdvisoryOutput) -> list[RenderableType]:
    parts: list[RenderableType | None] = [
        _header(o),
        Panel(o.summary, title="Summary", box=box.ROUNDED),
        _metrics(o),
        _anomalies(o),
        _recommendations(o),
        _bullets("Degraded mode", o.degraded_reasons, "red"),
        _bullets("Assumptions", o.assumptions, "yellow"),
        _bullets(
            "Data quality",
            [f"[{d.action}] {d.section}{'/' + d.record_id if d.record_id else ''}: {d.issue}" for d in o.data_quality if d.action != "noted"][:15],
        ),
        _bullets("Confidence deductions", [f"-{p.amount:.2f} {p.reason} ({p.source})" for p in o.confidence_breakdown]),
        _bullets("Skipped / failed detectors", [f"{d.detector}: {d.status} ({d.reason})" for d in o.detectors if d.status != "ran"]),
        _bullets(
            "Trend vs previous run",
            [
                f"previous run {o.trend.previous_run_id} at {o.trend.previous_generated_at}",
                f"total assets change: {pct(o.trend.total_assets_change_pct)}",
                f"new findings: {len(o.trend.new_anomalies)}, recurring: {len(o.trend.recurring_anomalies)}, resolved: {len(o.trend.resolved_anomalies)}",
            ]
            if o.trend
            else [],
        ),
        _bullets(
            "Review",
            [f"status: {o.review.status} ({o.review.mode})", *[f"trigger: {r}" for r in o.review.trigger_reasons],
             *( [f"reviewer: {o.review.reviewer} at {o.review.decided_at}"] if o.review.reviewer else []),
             *( [f"note: {o.review.reviewer_note}"] if o.review.reviewer_note else []), *[f"change: {c}" for c in o.review.changes]],
        ),
        _audit(o),
        Text(o.disclaimer, style="dim italic"),
    ]
    return [p for p in parts if p is not None]


def build_failure(result: RunResult) -> list[RenderableType]:
    err = result.error
    body = Text()
    body.append(f"Client {result.client_id}  run {result.run_id}\n", style="bold")
    body.append(f"Status: FAILED (controlled)\n", style="bold red")
    if err:
        body.append(f"Error [{err.code}] at step '{err.step}': {err.message}\n")
    body.append("Other clients in the batch are not affected.", style="dim")
    t = Table(title="Execution trace", box=box.SIMPLE)
    for col in ("Step", "Status", "Error"):
        t.add_column(col)
    for s in result.steps:
        t.add_row(s.name, s.status, s.error or "")
    return [Panel(body, title="Run failed", border_style="red"), t]


def render(console: Console, renderables: list[RenderableType]) -> None:
    for r in renderables:
        console.print(r)


def to_text(renderables: list[RenderableType], width: int = 110) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=width, color_system=None, force_terminal=False, legacy_windows=False)
    render(console, renderables)
    return buf.getvalue()


def batch_summary(results: list[RunResult]) -> Table:
    t = Table(title="Batch summary", box=box.ROUNDED)
    for col in ("Client", "Status", "Anomalies (H/M)", "Confidence", "Degraded", "Review", "Duration"):
        t.add_column(col)
    for r in results:
        o: Any = r.output
        if o:
            hi = sum(a.severity == "high" for a in o.anomalies)
            me = sum(a.severity == "medium" for a in o.anomalies)
            t.add_row(r.client_id, Text("completed", style="green"), f"{len(o.anomalies)} ({hi}/{me})", f"{o.confidence:.2f}",
                      "yes" if o.degraded else "no", Text(o.review.status, style=REVIEW_STYLE.get(o.review.status, "")), f"{r.duration_ms:.0f} ms")
        else:
            t.add_row(r.client_id, Text("failed", style="red"), "-", "-", "-", r.error.code if r.error else "-", f"{r.duration_ms or 0:.0f} ms")
    return t
