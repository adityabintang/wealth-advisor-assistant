"""Portfolio metrics (PRD FR-5). Pure functions, no I/O."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta

from ..models import AllocationSlice, CashFlow, ClientFinancialData, PortfolioMetrics, TopHolding, ValueChange

_LIABILITY_ACCOUNT_TYPES = {"credit", "loan", "credit_card", "mortgage"}


def _weight(value: float, total: float) -> float:
    return round(value / total, 6) if total > 0 else 0.0


def cash_flow(data: ClientFinancialData, window_days: int) -> CashFlow:
    end = datetime.combine(data.as_of, time.max)
    start = end - timedelta(days=window_days)
    inflow = outflow = 0.0
    for t in data.transactions:
        if start < t.date <= end:
            if t.direction == "in":
                inflow += t.amount
            else:
                outflow += t.amount
    return CashFlow(window_days=window_days, inflow=round(inflow, 2), outflow=round(outflow, 2), net=round(inflow - outflow, 2))


def value_change(data: ClientFinancialData, window_days: int) -> ValueChange | None:
    start_limit = data.as_of - timedelta(days=window_days)
    points = [h for h in data.history if start_limit <= h.date <= data.as_of]
    if len(points) < 2 or points[0].total_value <= 0:
        return None
    first, last = points[0], points[-1]
    return ValueChange(
        window_days=window_days,
        start_date=first.date,
        end_date=last.date,
        start_value=first.total_value,
        end_value=last.total_value,
        change_pct=round((last.total_value - first.total_value) / first.total_value, 6),
    )


def compute_metrics(data: ClientFinancialData, value_window_days: int = 90) -> PortfolioMetrics:
    cash_balance = sum(max(a.balance, 0.0) for a in data.accounts if a.type.lower() not in _LIABILITY_ACCOUNT_TYPES)
    invested = sum(h.market_value for h in data.holdings)
    total = cash_balance + invested

    by_class: dict[str, float] = defaultdict(float)
    by_class["cash"] += cash_balance
    by_sector: dict[str, float] = defaultdict(float)
    by_asset: dict[str, float] = defaultdict(float)
    for h in data.holdings:
        by_class[h.asset_class] += h.market_value
        by_sector[h.sector] += h.market_value
        by_asset[h.asset_id] += h.market_value

    meta = {h.asset_id: h for h in data.holdings}
    ranked = sorted(by_asset.items(), key=lambda kv: kv[1], reverse=True)
    top = [
        TopHolding(
            asset_id=aid,
            name=meta[aid].name,
            asset_class=meta[aid].asset_class,
            sector=meta[aid].sector,
            value=round(v, 2),
            weight=_weight(v, invested),
        )
        for aid, v in ranked[:5]
    ]

    with_cost = [h for h in data.holdings if h.cost_basis is not None]
    cost = sum(h.cost_basis or 0.0 for h in with_cost)
    pnl = sum(h.market_value - (h.cost_basis or 0.0) for h in with_cost) if with_cost else None

    return PortfolioMetrics(
        as_of=data.as_of,
        base_currency=data.base_currency,
        total_assets=round(total, 2),
        cash_balance=round(cash_balance, 2),
        invested_value=round(invested, 2),
        allocation_by_asset_class={
            k: AllocationSlice(value=round(v, 2), weight=_weight(v, total)) for k, v in sorted(by_class.items()) if v > 0
        },
        allocation_by_sector={
            k: AllocationSlice(value=round(v, 2), weight=_weight(v, invested))
            for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1])
        },
        equity_weight=_weight(by_class.get("equity", 0.0), total),
        top_holdings=top,
        max_single_asset_weight=top[0].weight if top else 0.0,
        unrealized_pnl=round(pnl, 2) if pnl is not None else None,
        unrealized_pnl_pct=round(pnl / cost, 6) if pnl is not None and cost > 0 else None,
        cashflow_30d=cash_flow(data, 30),
        cashflow_90d=cash_flow(data, 90),
        value_change_90d=value_change(data, value_window_days),
        transaction_count=len(data.transactions),
    )
