"""Unit tests for each anomaly detector (positive + negative + skip paths)."""

from __future__ import annotations

from datetime import datetime, timedelta

from conftest import AS_OF, base_client, daily_txns, txn

from wealth_advisor.analytics.anomaly import DetectorInput, run_detectors
from wealth_advisor.analytics.metrics import compute_metrics
from wealth_advisor.config import Settings
from wealth_advisor.models import ClientFinancialData, RiskProfile


def _run(settings: Settings, payload: dict, risk: RiskProfile | None = None):
    data = ClientFinancialData.model_validate({k: v for k, v in payload.items() if not k.startswith("_")})
    data.transactions.sort(key=lambda t: t.date)
    metrics = compute_metrics(data)
    rp = risk or RiskProfile(category="moderate", max_equity_pct=0.6)
    anomalies, statuses = run_detectors(
        DetectorInput(data=data, metrics=metrics, risk_profile=rp, max_equity_pct=rp.max_equity_pct or 0.6, config=settings.anomaly)
    )
    return anomalies, {s.detector: s for s in statuses}


def _types(anomalies) -> list[str]:
    return [a.type for a in anomalies]


def test_clean_data_has_no_anomalies(settings: Settings) -> None:
    anomalies, statuses = _run(settings, base_client())
    assert anomalies == []
    assert all(s.status == "ran" for s in statuses.values())


def test_unusual_amount_robust_zscore(settings: Settings) -> None:
    rows = daily_txns() + [txn("BIG", AS_OF - timedelta(days=3), 25_000_000)]
    anomalies, _ = _run(settings, base_client(transactions=rows))
    hits = [a for a in anomalies if a.type == "unusual_amount"]
    assert len(hits) == 1 and hits[0].evidence["txn_ids"] == ["BIG"]
    assert hits[0].severity == "high"
    assert hits[0].evidence["multiple_of_median"] > 5


def test_duplicate_within_24h_but_not_monthly_repeats(settings: Settings) -> None:
    d = datetime(2026, 10, 1, 9, 0)
    rows = daily_txns() + [
        txn("D1", d, 1_350_000, category="utilities", cp="PDAM"),
        txn("D2", d + timedelta(minutes=3), 1_350_000, category="utilities", cp="PDAM"),
        txn("M1", datetime(2026, 8, 15), 2_500_000, category="insurance", cp="Pru"),
        txn("M2", datetime(2026, 9, 15), 2_500_000, category="insurance", cp="Pru"),
    ]
    anomalies, _ = _run(settings, base_client(transactions=rows))
    dups = [a for a in anomalies if a.type == "duplicate_transaction"]
    assert len(dups) == 1
    assert dups[0].evidence["txn_ids"] == ["D1", "D2"]


def test_frequency_spike(settings: Settings) -> None:
    spike_day = datetime(2026, 9, 20)
    rows = daily_txns() + [txn(f"S{i}", spike_day + timedelta(minutes=i), 300_000 + i * 1111, cp=f"Store{i}") for i in range(12)]
    anomalies, _ = _run(settings, base_client(transactions=rows))
    spikes = [a for a in anomalies if a.type == "frequency_spike"]
    assert len(spikes) == 1 and spikes[0].evidence["date"] == "2026-09-20"


def test_large_outflow_and_new_counterparty(settings: Settings) -> None:
    rows = daily_txns() + [txn("OUT", AS_OF - timedelta(days=5), 400_000_000, category="transfer", cp="New Crypto Ltd")]
    anomalies, _ = _run(settings, base_client(transactions=rows))
    types = _types(anomalies)
    assert "large_outflow" in types
    assert "new_counterparty_high_value" in types
    lo = next(a for a in anomalies if a.type == "large_outflow")
    assert lo.severity == "high" and lo.evidence["ratio"] > 0.30


def test_new_counterparty_requires_history(settings: Settings) -> None:
    rows = [txn(f"T{i}", AS_OF - timedelta(days=i), 500_000 + i) for i in range(10)]
    rows.append(txn("OUT", AS_OF - timedelta(days=1), 200_000_000, cp="Brand New"))
    _, statuses = _run(settings, base_client(transactions=rows))
    assert statuses["new_counterparty_high_value"].status == "skipped"


def test_concentration_asset_and_sector_with_exclusions(settings: Settings) -> None:
    holdings = [
        {"asset_id": "TECH1", "asset_class": "equity", "sector": "Technology", "market_value": 500_000_000, "currency": "IDR"},
        {"asset_id": "TECH2", "asset_class": "equity", "sector": "Technology", "market_value": 100_000_000, "currency": "IDR"},
        {"asset_id": "GOV", "asset_class": "fixed_income", "sector": "Government", "market_value": 400_000_000, "currency": "IDR"},
    ]
    anomalies, _ = _run(settings, base_client(holdings=holdings), RiskProfile(category="aggressive", max_equity_pct=0.9))
    conc = [a for a in anomalies if a.type == "asset_concentration"]
    scopes = {(a.evidence["scope"], a.evidence.get("asset_id") or a.evidence.get("sector")) for a in conc}
    assert ("asset", "TECH1") in scopes and ("sector", "Technology") in scopes
    assert not any(a.evidence.get("asset_id") == "GOV" or a.evidence.get("sector") == "Government" for a in conc)


def test_risk_profile_deviation_severity_depends_on_assumption(settings: Settings) -> None:
    confirmed, _ = _run(settings, base_client(), RiskProfile(category="conservative", max_equity_pct=0.10))
    assumed, _ = _run(settings, base_client(), RiskProfile(category="conservative", max_equity_pct=0.10, assumed=True))
    c = next(a for a in confirmed if a.type == "risk_profile_deviation")
    a = next(a for a in assumed if a.type == "risk_profile_deviation")
    assert c.severity == "high" and a.severity == "medium"


def test_value_decline(settings: Settings) -> None:
    hist = [
        {"date": (AS_OF - timedelta(days=90)).isoformat(), "total_value": 1_000_000_000},
        {"date": AS_OF.isoformat(), "total_value": 850_000_000},
    ]
    anomalies, _ = _run(settings, base_client(history=hist))
    vd = [a for a in anomalies if a.type == "value_decline"]
    assert len(vd) == 1 and vd[0].evidence["change_pct"] < -0.10


def test_statistical_detectors_skipped_with_few_transactions(settings: Settings) -> None:
    rows = [txn(f"T{i}", AS_OF - timedelta(days=i * 3), 500_000) for i in range(5)]
    _, statuses = _run(settings, base_client(transactions=rows))
    assert statuses["unusual_amount"].status == "skipped"
    assert statuses["frequency_spike"].status == "skipped"
    assert statuses["duplicate_transaction"].status == "ran"  # rule-based still runs


def test_no_transactions_skips_txn_detectors_but_runs_portfolio_checks(settings: Settings) -> None:
    _, statuses = _run(settings, base_client(transactions=[]))
    for name in ("unusual_amount", "frequency_spike", "duplicate_transaction", "large_outflow", "new_counterparty_high_value"):
        assert statuses[name].status == "skipped"
    for name in ("asset_concentration", "risk_profile_deviation", "value_decline"):
        assert statuses[name].status == "ran"


def test_failing_detector_is_isolated(settings: Settings) -> None:
    from wealth_advisor.analytics import anomaly as mod

    def boom(_inp):
        raise RuntimeError("bug")

    data = ClientFinancialData.model_validate(base_client())
    metrics = compute_metrics(data)
    specs = (mod.DetectorSpec("boom", boom, False, False),) + mod.DETECTORS
    anomalies, statuses = mod.run_detectors(
        DetectorInput(data=data, metrics=metrics, risk_profile=RiskProfile(category="moderate"), max_equity_pct=0.6, config=settings.anomaly), specs
    )
    st = {s.detector: s for s in statuses}
    assert st["boom"].status == "failed"
    assert st["asset_concentration"].status == "ran"


def test_every_anomaly_has_evidence_and_explanation(settings: Settings) -> None:
    rows = daily_txns() + [txn("OUT", AS_OF - timedelta(days=5), 400_000_000, cp="New Co"), txn("BIG", AS_OF - timedelta(days=2), 30_000_000)]
    anomalies, _ = _run(settings, base_client(transactions=rows))
    assert anomalies
    for a in anomalies:
        assert a.evidence and a.explanation and a.fingerprint and a.anomaly_id.startswith("A")
