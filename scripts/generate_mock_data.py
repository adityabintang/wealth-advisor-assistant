"""Generate the deterministic mock dataset in data/ (seeded, re-runnable).

    uv run python scripts/generate_mock_data.py

Scenarios (PRD §18):
  C001_normal              clean data, no anomalies              -> happy path, no HITL
  C002_anomalies           8 injected anomaly types              -> detection + HITL
  C003_incomplete          missing fields, bad rows, < 10 txns   -> partial analysis, low confidence
  C004_crm_down            CRM times out (failure injection)     -> retry, fallback, degraded mode
  C005_broken_json         truncated JSON                        -> controlled failure, batch continues
  C006_no_transactions     empty transaction list, no history    -> partial analysis
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ROOT / "data" / "clients"
AS_OF = date(2026, 10, 10)
START = AS_OF - timedelta(days=91)


def _dt(d: date, hour: int = 10, minute: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute).isoformat()


class TxnFactory:
    def __init__(self, prefix: str, rng: random.Random) -> None:
        self.prefix = prefix
        self.rng = rng
        self.n = 0
        self.rows: list[dict] = []

    def add(self, d: date, amount: float, direction: str, category: str, cp: str, account: str, hour: int | None = None, minute: int | None = None) -> str:
        self.n += 1
        tid = f"{self.prefix}-T{self.n:04d}"
        self.rows.append(
            {
                "txn_id": tid,
                "date": _dt(d, hour if hour is not None else self.rng.randint(8, 20), minute if minute is not None else self.rng.randint(0, 59)),
                "amount": round(amount, -3),
                "direction": direction,
                "category": category,
                "counterparty": cp,
                "account_id": account,
                "currency": "IDR",
            }
        )
        return tid

    def every(self, period: int, offset: int, lo: float, hi: float, direction: str, category: str, cps: list[str], account: str) -> None:
        d = START + timedelta(days=offset)
        i = 0
        while d <= AS_OF:
            self.add(d, self.rng.uniform(lo, hi), direction, category, cps[i % len(cps)], account)
            d += timedelta(days=period + self.rng.choice([0, 0, 1]))
            i += 1

    def monthly(self, day: int, lo: float, hi: float, direction: str, category: str, cp: str, account: str) -> None:
        for month in (7, 8, 9, 10):
            d = date(2026, month, day)
            if START < d <= AS_OF:
                self.add(d, self.rng.uniform(lo, hi), direction, category, cp, account)


def regular_life(f: TxnFactory, acc: str, salary: float, employer: str) -> None:
    f.monthly(25, salary, salary, "in", "salary", employer, acc)
    f.monthly(5, 1_100_000, 1_400_000, "out", "utilities", "PLN", acc)
    f.monthly(7, 420_000, 480_000, "out", "utilities", "IndiHome", acc)
    f.monthly(10, 15_000_000, 15_000_000, "out", "investment", "Mandiri Sekuritas", acc)
    f.monthly(15, 2_500_000, 2_500_000, "out", "insurance", "Prudential", acc)
    f.every(3, 0, 600_000, 1_200_000, "out", "groceries", ["Superindo", "Ranch Market", "Hypermart"], acc)
    f.every(4, 1, 250_000, 450_000, "out", "dining", ["Sate Khas Senayan", "Kopi Kenangan", "Bakmi GM"], acc)
    f.every(6, 2, 200_000, 900_000, "out", "online_shopping", ["Tokopedia", "Shopee", "Blibli"], acc)
    f.every(5, 3, 50_000, 150_000, "out", "transport", ["Gojek", "Grab"], acc)


def history(start_value: float, end_value: float, rng: random.Random, noise: float = 0.006) -> list[dict]:
    points = []
    n = 13
    for i in range(n + 1):
        d = AS_OF - timedelta(days=90 - 7 * i) if i < n else AS_OF
        base = start_value + (end_value - start_value) * i / n
        val = end_value if i == n else (start_value if i == 0 else base * (1 + rng.uniform(-noise, noise)))
        points.append({"date": d.isoformat(), "total_value": round(val, -5)})
    return points


def write(name: str, payload: dict) -> None:
    (CLIENTS / f"{name}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- C001 normal
def c001() -> None:
    rng = random.Random(1001)
    f = TxnFactory("C001", rng)
    regular_life(f, "ACC-C001-0001", 85_000_000, "PT Nusantara Teknologi")
    holdings = [
        ("BBCA", "Bank Central Asia", "equity", "Banking", 150_000_000, 130_000_000),
        ("TLKM", "Telkom Indonesia", "equity", "Telecommunication", 90_000_000, 95_000_000),
        ("ASII", "Astra International", "equity", "Consumer Cyclical", 80_000_000, 72_000_000),
        ("XIIT", "IDX30 Index ETF", "equity", "Diversified", 120_000_000, 110_000_000),
        ("FR0091", "Indonesia Gov Bond FR0091", "fixed_income", "Government", 200_000_000, 198_000_000),
        ("CBFUND", "Corporate Bond Fund", "fixed_income", "Corporate Credit", 100_000_000, 97_000_000),
        ("MMF01", "Money Market Fund", "cash", "Money Market", 60_000_000, 60_000_000),
        ("GOLD", "Antam Gold", "commodity", "Commodities", 50_000_000, 42_000_000),
    ]
    write(
        "C001_normal",
        {
            "as_of": AS_OF.isoformat(),
            "client": {"client_id": "C001", "name": "Budi Santoso", "date_of_birth": "1981-04-12", "base_currency": "IDR", "segment": "affluent"},
            "accounts": [
                {"account_id": "ACC-C001-0001", "type": "checking", "balance": 75_000_000, "currency": "IDR"},
                {"account_id": "ACC-C001-0002", "type": "savings", "balance": 250_000_000, "currency": "IDR"},
            ],
            "holdings": [
                {"asset_id": a, "name": n, "asset_class": c, "sector": s, "quantity": round(v / 1000), "market_value": v, "cost_basis": cb, "currency": "IDR"}
                for a, n, c, s, v, cb in holdings
            ],
            "transactions": f.rows,
            "history": history(825_000_000, 850_000_000, rng),
        },
    )


# ----------------------------------------------------------------------------- C002 anomalies
def c002() -> None:
    rng = random.Random(2002)
    f = TxnFactory("C002", rng)
    acc = "ACC-C002-0001"
    regular_life(f, acc, 120_000_000, "PT Samudra Energi")
    injected = []

    # 1) unusual amount (dining is normally 250k-450k)
    tid = f.add(date(2026, 9, 18), 18_500_000, "out", "dining", "Henshin Rooftop", acc, 21, 30)
    injected.append({"type": "unusual_amount", "evidence": [tid]})
    # 2) duplicate payment (same amount + counterparty within minutes)
    t1 = f.add(date(2026, 10, 3), 1_350_000, "out", "utilities", "PDAM Jakarta", acc, 9, 12)
    t2 = f.add(date(2026, 10, 3), 1_350_000, "out", "utilities", "PDAM Jakarta", acc, 9, 14)
    injected.append({"type": "duplicate_transaction", "evidence": [t1, t2]})
    # 3) frequency spike: 14 small online purchases in one day (distinct amounts)
    amounts = rng.sample(range(150, 850, 7), 14)
    spike_ids = [
        f.add(date(2026, 9, 27), a * 1000, "out", "online_shopping", ["Tokopedia", "Shopee", "Blibli", "Lazada"][i % 4], acc, 10 + i // 2, (i * 7) % 60)
        for i, a in enumerate(amounts)
    ]
    injected.append({"type": "frequency_spike", "evidence": spike_ids[:3]})
    # 4) + 5) large outflow to a brand-new counterparty
    tid = f.add(date(2026, 10, 1), 650_000_000, "out", "transfer", "PT Aurora Digital Asset", acc, 14, 5)
    injected.append({"type": "large_outflow", "evidence": [tid]})
    injected.append({"type": "new_counterparty_high_value", "evidence": [tid]})
    # 6) concentration, 7) risk-profile deviation (holdings), 8) value decline (history)
    injected += [
        {"type": "asset_concentration", "evidence": ["GOTO", "Technology"]},
        {"type": "risk_profile_deviation", "evidence": ["equity > 60% (moderate)"]},
        {"type": "value_decline", "evidence": ["history -15%"]},
    ]
    holdings = [
        ("GOTO", "GoTo Gojek Tokopedia", "equity", "Technology", 520_000_000, 610_000_000),
        ("BUKA", "Bukalapak", "equity", "Technology", 120_000_000, 150_000_000),
        ("BBRI", "Bank Rakyat Indonesia", "equity", "Banking", 200_000_000, 190_000_000),
        ("FR0098", "Indonesia Gov Bond FR0098", "fixed_income", "Government", 100_000_000, 100_000_000),
        ("MMF02", "Money Market Fund", "cash", "Money Market", 60_000_000, 60_000_000),
    ]
    write(
        "C002_anomalies",
        {
            "_injected_anomalies": injected,
            "as_of": AS_OF.isoformat(),
            "client": {"client_id": "C002", "name": "Siti Rahmawati", "date_of_birth": "1976-11-02", "base_currency": "IDR", "segment": "high_net_worth"},
            "accounts": [
                {"account_id": acc, "type": "checking", "balance": 120_000_000, "currency": "IDR"},
                {"account_id": "ACC-C002-0002", "type": "savings", "balance": 180_000_000, "currency": "IDR"},
            ],
            "holdings": [
                {"asset_id": a, "name": n, "asset_class": c, "sector": s, "quantity": round(v / 1000), "market_value": v, "cost_basis": cb, "currency": "IDR"}
                for a, n, c, s, v, cb in holdings
            ],
            "transactions": f.rows,
            "history": history(1_180_000_000, 1_000_000_000, rng),
        },
    )


# ----------------------------------------------------------------------------- C003 incomplete / messy
def c003() -> None:
    write(
        "C003_incomplete",
        {
            "as_of": AS_OF.isoformat(),
            "client": {"client_id": "C003", "name": "Andi Wijaya", "base_currency": "IDR"},
            "accounts": [
                {"account_id": "ACC-C003-0001", "type": "savings", "balance": 90_000_000},
            ],
            "holdings": [
                {"asset_id": "SCHRODER-EQ", "name": "Schroder Dana Prestasi", "asset_class": "equity", "sector": "Diversified", "market_value": 60_000_000, "cost_basis": 55_000_000, "currency": "IDR"},
                {"asset_id": "ORI023", "name": "ORI023 Retail Bond", "asset_class": "bond", "sector": "Government", "market_value": 150_000_000, "cost_basis": 150_000_000, "currency": "IDR"},
                {"asset_id": "VOO", "name": "Vanguard S&P 500 ETF", "asset_class": "equity", "market_value": 5_000, "currency": "USD"},
            ],
            "transactions": [
                {"txn_id": "C003-T0001", "date": "2026-09-25", "amount": 45_000_000, "direction": "in", "category": "salary", "counterparty": "CV Maju Jaya", "currency": "IDR"},
                {"txn_id": "C003-T0002", "date": "2026-09-27", "amount": 850_000, "direction": "out", "category": "groceries", "counterparty": "Alfamart", "currency": "IDR"},
                {"txn_id": "C003-T0003", "date": "2026-09-30", "amount": -2_500_000, "category": "utilities", "counterparty": "PLN", "currency": "IDR"},
                {"txn_id": "C003-T0004", "date": "2026-13-45", "amount": 1_200_000, "direction": "out", "category": "dining", "counterparty": "Unknown Resto", "currency": "IDR"},
                {"txn_id": "C003-T0005", "date": "2026-10-02", "amount": 12_000, "direction": "out", "category": "shopping", "counterparty": "Rakuten", "currency": "JPY"},
                {"txn_id": "C003-T0006", "date": "2026-10-04", "amount": 300, "direction": "out", "category": "dining", "counterparty": "Marina Bay Grill", "currency": "USD"},
                {"txn_id": "C003-T0007", "date": "2026-10-06", "amount": 400_000, "direction": "out", "currency": "IDR"},
            ],
        },
    )


# ----------------------------------------------------------------------------- C004 CRM down
def c004() -> None:
    rng = random.Random(4004)
    f = TxnFactory("C004", rng)
    regular_life(f, "ACC-C004-0001", 65_000_000, "RS Medika Utama")
    holdings = [
        ("BMRI", "Bank Mandiri", "equity", "Banking", 110_000_000, 98_000_000),
        ("UNVR", "Unilever Indonesia", "equity", "Consumer Non-Cyclical", 70_000_000, 85_000_000),
        ("XIIT", "IDX30 Index ETF", "equity", "Diversified", 150_000_000, 140_000_000),
        ("FR0087", "Indonesia Gov Bond FR0087", "fixed_income", "Government", 180_000_000, 178_000_000),
        ("MMF03", "Money Market Fund", "cash", "Money Market", 40_000_000, 40_000_000),
    ]
    write(
        "C004_crm_down",
        {
            "as_of": AS_OF.isoformat(),
            "client": {"client_id": "C004", "name": "Rina Kusuma", "date_of_birth": "1988-02-20", "base_currency": "IDR", "segment": "affluent"},
            "accounts": [
                {"account_id": "ACC-C004-0001", "type": "checking", "balance": 55_000_000, "currency": "IDR"},
                {"account_id": "ACC-C004-0002", "type": "savings", "balance": 150_000_000, "currency": "IDR"},
            ],
            "holdings": [
                {"asset_id": a, "name": n, "asset_class": c, "sector": s, "quantity": round(v / 1000), "market_value": v, "cost_basis": cb, "currency": "IDR"}
                for a, n, c, s, v, cb in holdings
            ],
            "transactions": f.rows,
            "history": history(540_000_000, 550_000_000, rng),
        },
    )


# ----------------------------------------------------------------------------- C005 broken JSON
def c005() -> None:
    text = (
        '{\n  "as_of": "2026-10-10",\n  "client": {"client_id": "C005", "name": "Hendra Gunawan", "base_currency": "IDR"},\n'
        '  "accounts": [\n    {"account_id": "ACC-C005-0001", "type": "savings", "balance": 300000000, "currency": "IDR"},\n'
        '  "transactions": [\n    {"txn_id": "C005-T0001", "date": "2026-09-01", "amount": 5000000, "direction": "out"\n'
    )
    (CLIENTS / "C005_broken_json.json").write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------------- C006 no transactions
def c006() -> None:
    write(
        "C006_no_transactions",
        {
            "as_of": AS_OF.isoformat(),
            "client": {"client_id": "C006", "name": "Dewi Lestari", "age": 67, "base_currency": "IDR", "segment": "retiree"},
            "accounts": [{"account_id": "ACC-C006-0001", "type": "savings", "balance": 420_000_000, "currency": "IDR"}],
            "holdings": [
                {"asset_id": "DEP-12M", "name": "12M Time Deposit", "asset_class": "cash", "sector": "Cash", "market_value": 300_000_000, "cost_basis": 300_000_000, "currency": "IDR"},
                {"asset_id": "SBR012", "name": "SBR012 Savings Bond", "asset_class": "fixed_income", "sector": "Government", "market_value": 200_000_000, "cost_basis": 200_000_000, "currency": "IDR"},
            ],
            "transactions": [],
        },
    )


# ----------------------------------------------------------------------------- CRM mock
def crm() -> None:
    advisor = "A. B. Fadila"
    payload = {
        "_comment": "Mock CRM API. failure_injection simulates outages per client (timeout | error_500 | empty | flaky).",
        "failure_injection": {"C004": "timeout"},
        "clients": {
            "C001": {
                "status": "active",
                "risk_profile": {"category": "moderate", "max_equity_pct": 0.60, "score": 55, "last_assessed": "2026-01-15"},
                "goals": [
                    {"goal": "Retirement at 60", "target_amount": 8_000_000_000, "horizon_years": 15, "priority": "high"},
                    {"goal": "Children's university fund", "target_amount": 1_500_000_000, "horizon_years": 8, "priority": "medium"},
                ],
                "relationship": {"tier": "Gold", "last_contact_date": "2026-08-20", "advisor": advisor},
                "notes": [{"date": "2026-08-20", "author": advisor, "text": "Quarterly review; client comfortable with current allocation."}],
            },
            "C002": {
                "status": "active",
                "risk_profile": {"category": "moderate", "max_equity_pct": 0.60, "score": 50, "last_assessed": "2025-11-03"},
                "goals": [{"goal": "Wealth preservation", "horizon_years": 10, "priority": "high"}],
                "relationship": {"tier": "Platinum", "last_contact_date": "2026-06-15", "advisor": advisor},
                "notes": [{"date": "2026-06-15", "author": advisor, "text": "Client expressed interest in digital assets; advised caution."}],
            },
            "C003": {
                "status": "active",
                "goals": [{"goal": "Buy a house", "target_amount": 1_200_000_000, "horizon_years": 5, "priority": "high"}],
                "relationship": {"tier": "Silver", "last_contact_date": "2026-09-01", "advisor": advisor},
                "notes": [{"date": "2026-09-01", "author": advisor, "text": "Risk profiling questionnaire not yet completed."}],
            },
            "C004": {
                "status": "active",
                "risk_profile": {"category": "moderate", "max_equity_pct": 0.60, "score": 52, "last_assessed": "2026-03-10"},
                "goals": [{"goal": "Early retirement", "horizon_years": 20, "priority": "high"}],
                "relationship": {"tier": "Gold", "last_contact_date": "2026-09-12", "advisor": advisor},
                "notes": [],
            },
            "C006": {
                "status": "active",
                "risk_profile": {"category": "conservative", "max_equity_pct": 0.20, "score": 20, "last_assessed": "2025-06-01"},
                "goals": [{"goal": "Stable retirement income", "horizon_years": 10, "priority": "high"}],
                "relationship": {"tier": "Silver", "last_contact_date": "2025-12-01", "advisor": advisor},
                "notes": [{"date": "2025-12-01", "author": advisor, "text": "Prefers deposits and government bonds."}],
            },
        },
    }
    (ROOT / "data" / "crm_mock.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    CLIENTS.mkdir(parents=True, exist_ok=True)
    for fn in (c001, c002, c003, c004, c005, c006, crm):
        fn()
    print(f"Mock data written to {ROOT / 'data'}")
