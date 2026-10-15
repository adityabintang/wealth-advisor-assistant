from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from wealth_advisor.app import Application, RuntimeOptions, build_application
from wealth_advisor.config import Settings, load_settings
from wealth_advisor.logging_setup import configure_logging

AS_OF = date(2026, 10, 10)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = load_settings()
    s.paths.memory_path = tmp_path / "memory.sqlite"
    s.paths.log_dir = tmp_path / "logs"
    s.paths.output_dir = tmp_path / "output"
    s.crm.backoff_base_seconds = 0.0  # keep tests fast
    s.crm.timeout_seconds = 0.01
    return s


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "logs" / "test.jsonl"
    configure_logging(path)
    return path


@pytest.fixture
def make_app(settings: Settings, log_file: Path) -> Iterator[Callable[..., Application]]:
    apps: list[Application] = []

    def factory(**kwargs: Any) -> Application:
        opts = RuntimeOptions(sleep=lambda _s: None, use_llm=False, **kwargs)
        app = build_application(settings, opts)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()


def txn(tid: str, day: date | datetime, amount: float, direction: str = "out", category: str = "groceries", cp: str = "Shop") -> dict[str, Any]:
    dt = day if isinstance(day, datetime) else datetime(day.year, day.month, day.day, 12, 0)
    return {"txn_id": tid, "date": dt.isoformat(), "amount": amount, "direction": direction, "category": category, "counterparty": cp, "currency": "IDR"}


def daily_txns(n_days: int = 60, per_day: int = 1, amount: float = 500_000, category: str = "groceries") -> list[dict[str, Any]]:
    rows = []
    for d in range(n_days):
        day = AS_OF - timedelta(days=n_days - d)
        for k in range(per_day):
            rows.append(txn(f"T{d:03d}{k}", datetime(day.year, day.month, day.day, 9 + k), amount + (d % 7) * 10_000 + k * 1000, category=category, cp=f"Shop{d % 5}"))
    return rows


def write_client(dir_: Path, name: str, payload: dict[str, Any] | str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def base_client(client_id: str = "X001", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "as_of": AS_OF.isoformat(),
        "client": {"client_id": client_id, "name": "Test Client", "age": 40, "base_currency": "IDR"},
        "accounts": [{"account_id": "ACC-1", "type": "savings", "balance": 500_000_000, "currency": "IDR"}],
        "holdings": [
            {"asset_id": "A", "asset_class": "equity", "sector": "Banking", "market_value": 100_000_000, "cost_basis": 90_000_000, "currency": "IDR"},
            {"asset_id": "B", "asset_class": "fixed_income", "sector": "Government", "market_value": 150_000_000, "cost_basis": 150_000_000, "currency": "IDR"},
            {"asset_id": "C", "asset_class": "equity", "sector": "Telecommunication", "market_value": 100_000_000, "cost_basis": 100_000_000, "currency": "IDR"},
        ],
        "transactions": daily_txns(),
        "history": [
            {"date": (AS_OF - timedelta(days=90)).isoformat(), "total_value": 340_000_000},
            {"date": AS_OF.isoformat(), "total_value": 350_000_000},
        ],
    }
    payload.update(overrides)
    return payload
