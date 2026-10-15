"""Rule-based + statistical anomaly detectors (PRD §10).

Every detector is a pure function ``(DetectorInput) -> list[Anomaly]``. Each finding carries
evidence (txn ids or computed numbers) and a short explanation. Statistical detectors are
skipped when there are too few transactions; rule-based ones always run.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from ..config import AnomalyConfig
from ..models import Anomaly, ClientFinancialData, DetectorStatus, PortfolioMetrics, RiskProfile, Transaction

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class DetectorInput:
    data: ClientFinancialData
    metrics: PortfolioMetrics
    risk_profile: RiskProfile
    max_equity_pct: float
    config: AnomalyConfig


class SkipDetector(Exception):
    """Raised when a detector cannot run on the available data."""


def _fmt(amount: float, ccy: str) -> str:
    return f"{ccy} {amount:,.0f}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# --------------------------------------------------------------------------- statistical
def detect_unusual_amount(inp: DetectorInput) -> list[Anomaly]:
    cfg = inp.config.unusual_amount
    ccy = inp.data.base_currency
    groups: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for t in inp.data.transactions:
        groups[(t.direction, t.category.lower())].append(t)

    evaluable = {k: v for k, v in groups.items() if len(v) >= cfg.min_samples_per_category}
    if not evaluable:
        raise SkipDetector(f"no category has >= {cfg.min_samples_per_category} transactions")

    found: list[Anomaly] = []
    for (direction, category), txns in evaluable.items():
        amounts = [t.amount for t in txns]
        med = statistics.median(amounts)
        mad = statistics.median(abs(a - med) for a in amounts)
        for t in txns:
            if t.amount <= med or med <= 0:
                continue
            z = 0.6745 * (t.amount - med) / mad if mad > 0 else None
            multiple = t.amount / med
            if not ((z is not None and z > cfg.robust_z_threshold) or multiple > cfg.median_multiple):
                continue
            high = (z is not None and z > cfg.high_severity_robust_z) or multiple > cfg.high_severity_median_multiple
            found.append(
                Anomaly(
                    type="unusual_amount",
                    severity="high" if high else "medium",
                    title=f"Unusually large '{category}' {'outflow' if direction == 'out' else 'inflow'}",
                    evidence={
                        "txn_ids": [t.txn_id],
                        "amount": t.amount,
                        "date": t.date.date().isoformat(),
                        "counterparty": t.counterparty,
                        "category": category,
                        "category_median": round(med, 2),
                        "mad": round(mad, 2),
                        "robust_z": round(z, 2) if z is not None else None,
                        "multiple_of_median": round(multiple, 2),
                        "sample_size": len(txns),
                    },
                    explanation=(
                        f"{t.txn_id}: {_fmt(t.amount, ccy)} to/from {t.counterparty} is {multiple:.1f}x the category "
                        f"median ({_fmt(med, ccy)})"
                        + (f", robust z-score {z:.1f}" if z is not None else "")
                        + f" (thresholds: z>{cfg.robust_z_threshold} or >{cfg.median_multiple:g}x median)."
                    ),
                    fingerprint=f"unusual_amount:{t.txn_id}",
                )
            )
    return found


def detect_frequency_spike(inp: DetectorInput) -> list[Anomaly]:
    cfg = inp.config.frequency_spike
    txns = inp.data.transactions
    days = [t.date.date() for t in txns]
    start, end = min(days), max(days)
    span = (end - start).days + 1
    if span < cfg.min_days_observed:
        raise SkipDetector(f"only {span} day(s) of history (< {cfg.min_days_observed})")
    counts = Counter(days)
    series = [counts.get(start + timedelta(days=i), 0) for i in range(span)]
    mean = statistics.fmean(series)
    std = statistics.pstdev(series)
    threshold = mean + cfg.sigma * std
    found: list[Anomaly] = []
    for day, count in sorted(counts.items()):
        if count > threshold and count >= cfg.min_daily_count:
            ids = [t.txn_id for t in txns if t.date.date() == day]
            found.append(
                Anomaly(
                    type="frequency_spike",
                    severity="medium",
                    title=f"Transaction frequency spike on {day.isoformat()}",
                    evidence={
                        "date": day.isoformat(),
                        "count": count,
                        "daily_mean": round(mean, 3),
                        "daily_std": round(std, 3),
                        "threshold": round(threshold, 3),
                        "txn_ids": ids[:25],
                    },
                    explanation=(
                        f"{count} transactions on {day.isoformat()} vs a daily mean of {mean:.2f} "
                        f"(threshold mean + {cfg.sigma:g} sigma = {threshold:.2f})."
                    ),
                    fingerprint=f"frequency_spike:{day.isoformat()}",
                )
            )
    return found


# --------------------------------------------------------------------------- rule-based
def detect_duplicates(inp: DetectorInput) -> list[Anomaly]:
    cfg = inp.config.duplicate
    ccy = inp.data.base_currency
    window = timedelta(hours=cfg.window_hours)
    groups: dict[tuple[str, float, str], list[Transaction]] = defaultdict(list)
    for t in inp.data.transactions:
        groups[(t.direction, round(t.amount, 2), t.counterparty.strip().lower())].append(t)

    found: list[Anomaly] = []
    for (direction, amount, cp), txns in groups.items():
        if len(txns) < cfg.min_occurrences:
            continue
        txns = sorted(txns, key=lambda t: t.date)
        cluster = [txns[0]]
        clusters: list[list[Transaction]] = []
        for t in txns[1:]:
            if t.date - cluster[-1].date <= window:
                cluster.append(t)
            else:
                clusters.append(cluster)
                cluster = [t]
        clusters.append(cluster)
        for c in clusters:
            if len(c) < cfg.min_occurrences:
                continue
            ids = [t.txn_id for t in c]
            found.append(
                Anomaly(
                    type="duplicate_transaction",
                    severity="medium",
                    title=f"Possible duplicate {'payment' if direction == 'out' else 'credit'} to {c[0].counterparty}",
                    evidence={
                        "txn_ids": ids,
                        "amount": amount,
                        "counterparty": c[0].counterparty,
                        "dates": [t.date.isoformat() for t in c],
                        "window_hours": cfg.window_hours,
                    },
                    explanation=(
                        f"{len(c)} transactions of {_fmt(amount, ccy)} with {c[0].counterparty} within "
                        f"{cfg.window_hours:g}h ({', '.join(ids)})."
                    ),
                    fingerprint=f"duplicate:{cp}:{amount:.0f}:{ids[0]}",
                )
            )
    return found


def detect_large_outflow(inp: DetectorInput) -> list[Anomaly]:
    cfg = inp.config.large_outflow
    m = inp.metrics
    ccy = inp.data.base_currency
    if m.total_assets <= 0:
        raise SkipDetector("total assets is zero")
    end = datetime.combine(inp.data.as_of, time.max)
    start = end - timedelta(days=cfg.window_days)
    window = [t for t in inp.data.transactions if start < t.date <= end]
    inflow = sum(t.amount for t in window if t.direction == "in")
    outflow = sum(t.amount for t in window if t.direction == "out")
    net_out = outflow - inflow
    ratio = net_out / m.total_assets
    if ratio <= cfg.ratio_of_total_assets:
        return []
    top = sorted((t for t in window if t.direction == "out"), key=lambda t: t.amount, reverse=True)[:3]
    return [
        Anomaly(
            type="large_outflow",
            severity="high",
            title=f"Large net outflow in the last {cfg.window_days} days",
            evidence={
                "window_days": cfg.window_days,
                "inflow": round(inflow, 2),
                "outflow": round(outflow, 2),
                "net_outflow": round(net_out, 2),
                "total_assets": m.total_assets,
                "ratio": round(ratio, 4),
                "threshold": cfg.ratio_of_total_assets,
                "txn_ids": [t.txn_id for t in top],
            },
            explanation=(
                f"Net outflow of {_fmt(net_out, ccy)} over {cfg.window_days} days equals {_pct(ratio)} of total assets "
                f"(threshold {_pct(cfg.ratio_of_total_assets)}). Largest: {', '.join(t.txn_id for t in top)}."
            ),
            fingerprint=f"large_outflow:{cfg.window_days}d",
        )
    ]


def detect_new_counterparty(inp: DetectorInput) -> list[Anomaly]:
    cfg = inp.config.new_counterparty
    ccy = inp.data.base_currency
    txns = inp.data.transactions
    if not txns:
        raise SkipDetector("no transactions")
    first_date = txns[0].date
    history_days = (datetime.combine(inp.data.as_of, time.min) - first_date).days
    if history_days < cfg.min_history_days:
        raise SkipDetector(f"only {history_days} day(s) of transaction history (< {cfg.min_history_days})")

    window_start = datetime.combine(inp.data.as_of, time.min) - timedelta(days=cfg.lookback_days)
    first_seen: dict[str, Transaction] = {}
    by_cp: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:  # sorted by date
        key = t.counterparty.strip().lower()
        first_seen.setdefault(key, t)
        by_cp[key].append(t)

    found: list[Anomaly] = []
    for key, first in first_seen.items():
        if key == "unknown" or first.date < window_start:
            continue
        if (first.date - first_date).days < cfg.min_history_days:
            continue
        big = [t for t in by_cp[key] if t.direction == "out" and t.amount >= cfg.min_amount]
        if not big:
            continue
        total = sum(t.amount for t in big)
        found.append(
            Anomaly(
                type="new_counterparty_high_value",
                severity="medium",
                title=f"High-value payment to new counterparty '{first.counterparty}'",
                evidence={
                    "counterparty": first.counterparty,
                    "first_seen": first.date.date().isoformat(),
                    "txn_ids": [t.txn_id for t in big],
                    "total_amount": round(total, 2),
                    "threshold": cfg.min_amount,
                },
                explanation=(
                    f"'{first.counterparty}' first appeared on {first.date.date().isoformat()} and received "
                    f"{_fmt(total, ccy)} (threshold {_fmt(cfg.min_amount, ccy)})."
                ),
                fingerprint=f"new_counterparty:{key}",
            )
        )
    return found


def detect_concentration(inp: DetectorInput) -> list[Anomaly]:
    cfg = inp.config.concentration
    m = inp.metrics
    if m.invested_value <= 0:
        raise SkipDetector("no invested holdings")
    by_asset: dict[str, float] = defaultdict(float)
    info = {}
    asset_excluded = {s.lower() for s in cfg.single_asset_exclusion_sectors}
    for h in inp.data.holdings:
        if h.asset_class == "cash" or h.sector.lower() in asset_excluded:
            continue  # cash, sovereign bonds and diversified funds are not single-issuer risk
        by_asset[h.asset_id] += h.market_value
        info[h.asset_id] = h
    found: list[Anomaly] = []
    for aid, value in by_asset.items():
        weight = value / m.invested_value
        if weight > cfg.single_asset_pct:
            h = info[aid]
            found.append(
                Anomaly(
                    type="asset_concentration",
                    severity="medium",
                    title=f"Single-asset concentration: {h.name or aid}",
                    evidence={"scope": "asset", "asset_id": aid, "value": round(value, 2), "weight": round(weight, 4), "threshold": cfg.single_asset_pct},
                    explanation=f"{h.name or aid} is {_pct(weight)} of invested assets (threshold {_pct(cfg.single_asset_pct)}).",
                    fingerprint=f"concentration:asset:{aid}",
                )
            )
    excluded = {s.lower() for s in cfg.sector_exclusions}
    for sector, slice_ in m.allocation_by_sector.items():
        if sector.lower() in excluded:
            continue
        if slice_.weight > cfg.sector_pct:
            found.append(
                Anomaly(
                    type="asset_concentration",
                    severity="medium",
                    title=f"Sector concentration: {sector}",
                    evidence={"scope": "sector", "sector": sector, "value": slice_.value, "weight": slice_.weight, "threshold": cfg.sector_pct},
                    explanation=f"Sector '{sector}' is {_pct(slice_.weight)} of invested assets (threshold {_pct(cfg.sector_pct)}).",
                    fingerprint=f"concentration:sector:{sector.lower()}",
                )
            )
    return found


def detect_risk_profile_deviation(inp: DetectorInput) -> list[Anomaly]:
    m = inp.metrics
    if m.total_assets <= 0:
        raise SkipDetector("total assets is zero")
    limit = inp.max_equity_pct
    if m.equity_weight <= limit:
        return []
    assumed = inp.risk_profile.assumed
    return [
        Anomaly(
            type="risk_profile_deviation",
            severity="medium" if assumed else "high",
            title=f"Equity allocation exceeds {inp.risk_profile.category} profile limit",
            evidence={
                "equity_weight": m.equity_weight,
                "max_equity_pct": limit,
                "excess_pct": round(m.equity_weight - limit, 4),
                "risk_category": inp.risk_profile.category,
                "profile_assumed": assumed,
            },
            explanation=(
                f"Equity is {_pct(m.equity_weight)} of total assets vs a {_pct(limit)} limit for a "
                f"{inp.risk_profile.category} profile"
                + (" (profile ASSUMED, not confirmed by CRM; severity lowered to medium)." if assumed else ".")
            ),
            fingerprint="risk_profile_deviation:equity",
        )
    ]


def detect_value_decline(inp: DetectorInput) -> list[Anomaly]:
    cfg = inp.config.value_decline
    vc = inp.metrics.value_change_90d
    if vc is None:
        raise SkipDetector("portfolio history unavailable (need >= 2 snapshots)")
    if vc.change_pct >= cfg.threshold_pct:
        return []
    ccy = inp.data.base_currency
    return [
        Anomaly(
            type="value_decline",
            severity="medium",
            title=f"Portfolio value down {_pct(-vc.change_pct)} over {cfg.window_days} days",
            evidence=vc.model_dump(mode="json") | {"threshold_pct": cfg.threshold_pct},
            explanation=(
                f"Portfolio value fell from {_fmt(vc.start_value, ccy)} ({vc.start_date}) to {_fmt(vc.end_value, ccy)} "
                f"({vc.end_date}), {_pct(vc.change_pct)} (threshold {_pct(cfg.threshold_pct)})."
            ),
            fingerprint=f"value_decline:{cfg.window_days}d",
        )
    ]


# --------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class DetectorSpec:
    name: str
    fn: Callable[[DetectorInput], list[Anomaly]]
    statistical: bool
    needs_transactions: bool


DETECTORS: tuple[DetectorSpec, ...] = (
    DetectorSpec("unusual_amount", detect_unusual_amount, statistical=True, needs_transactions=True),
    DetectorSpec("frequency_spike", detect_frequency_spike, statistical=True, needs_transactions=True),
    DetectorSpec("duplicate_transaction", detect_duplicates, statistical=False, needs_transactions=True),
    DetectorSpec("large_outflow", detect_large_outflow, statistical=False, needs_transactions=True),
    DetectorSpec("new_counterparty_high_value", detect_new_counterparty, statistical=False, needs_transactions=True),
    DetectorSpec("asset_concentration", detect_concentration, statistical=False, needs_transactions=False),
    DetectorSpec("risk_profile_deviation", detect_risk_profile_deviation, statistical=False, needs_transactions=False),
    DetectorSpec("value_decline", detect_value_decline, statistical=False, needs_transactions=False),
)


def run_detectors(inp: DetectorInput, detectors: tuple[DetectorSpec, ...] = DETECTORS) -> tuple[list[Anomaly], list[DetectorStatus]]:
    """Run every detector in isolation: one failing detector never breaks the others."""
    n_txn = len(inp.data.transactions)
    min_stat = inp.config.min_transactions_for_statistics
    anomalies: list[Anomaly] = []
    statuses: list[DetectorStatus] = []
    for spec in detectors:
        if spec.needs_transactions and n_txn == 0:
            statuses.append(DetectorStatus(detector=spec.name, status="skipped", reason="no transactions"))
            continue
        if spec.statistical and n_txn < min_stat:
            statuses.append(
                DetectorStatus(detector=spec.name, status="skipped", reason=f"{n_txn} transactions < {min_stat} required for statistics")
            )
            continue
        try:
            found = spec.fn(inp)
        except SkipDetector as exc:
            statuses.append(DetectorStatus(detector=spec.name, status="skipped", reason=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - isolate detector bugs
            statuses.append(DetectorStatus(detector=spec.name, status="failed", reason=f"{type(exc).__name__}: {exc}"))
            continue
        anomalies.extend(found)
        statuses.append(DetectorStatus(detector=spec.name, status="ran", findings=len(found)))

    anomalies.sort(key=lambda a: (_SEVERITY_ORDER[a.severity], a.type, a.fingerprint))
    for i, a in enumerate(anomalies, start=1):
        a.anomaly_id = f"A{i}"
    return anomalies, statuses
