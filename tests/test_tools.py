"""Tool interface, registry, data validation and CRM resilience."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import base_client, write_client
from pydantic import BaseModel

from wealth_advisor.config import Settings
from wealth_advisor.errors import ToolNotFoundError
from wealth_advisor.integrations.crm_mock import MockCRMClient
from wealth_advisor.tools import Tool, ToolRegistry
from wealth_advisor.tools.client_data import ClientDataTool
from wealth_advisor.tools.crm import CRMTool
from wealth_advisor.tools.resilience import CircuitBreaker


class _Boom(BaseModel):
    x: int


class BoomTool(Tool[_Boom]):
    name = "boom"
    description = "always raises"
    input_schema = _Boom

    def _run(self, params, meta):
        raise ZeroDivisionError("kaboom")


def test_tool_never_raises_and_validates_input() -> None:
    res = BoomTool().execute({"x": 1})
    assert not res.ok and res.error.code == "unexpected_error"
    bad = BoomTool().execute({"x": "not-int"})
    assert not bad.ok and bad.error.code == "invalid_input"
    assert res.meta.duration_ms >= 0


def test_registry_lookup_and_replace() -> None:
    reg = ToolRegistry([BoomTool()])
    assert reg.get("boom").name == "boom"
    with pytest.raises(ToolNotFoundError):
        reg.get("missing")
    with pytest.raises(ValueError):
        reg.register(BoomTool())
    reg.replace(BoomTool())
    assert reg.names() == ["boom"]


# ----------------------------------------------------------------------------- client data
def _load(settings: Settings, tmp_path: Path, payload) -> object:
    path = write_client(tmp_path / "clients", "X001_test", payload)
    return ClientDataTool(settings).execute({"client_id": "X001", "path": str(path)})


def test_invalid_json_is_controlled_failure(settings: Settings, tmp_path: Path) -> None:
    res = _load(settings, tmp_path, '{"client": {"client_id": "X001"},')
    assert not res.ok and res.error.code == "invalid_json"
    assert "line" in res.error.message


def test_missing_client_section_is_schema_error(settings: Settings, tmp_path: Path) -> None:
    res = _load(settings, tmp_path, {"accounts": []})
    assert not res.ok and res.error.code == "schema_error"


def test_row_level_quarantine_normalisation_and_defaults(settings: Settings, tmp_path: Path) -> None:
    payload = base_client(
        transactions=[
            {"txn_id": "OK", "date": "2026-10-01", "amount": 100_000, "direction": "out", "category": "x", "counterparty": "y", "currency": "IDR"},
            {"txn_id": "NEG", "date": "2026-10-02", "amount": -50_000, "category": "x", "counterparty": "y", "currency": "IDR"},
            {"txn_id": "BADDATE", "date": "2026-02-31", "amount": 1, "direction": "out", "currency": "IDR"},
            {"txn_id": "USD", "date": "2026-10-03", "amount": 10, "direction": "out", "category": "x", "counterparty": "y", "currency": "USD"},
            {"txn_id": "JPY", "date": "2026-10-03", "amount": 10, "direction": "out", "currency": "JPY"},
            {"txn_id": "FUTURE", "date": "2027-01-01", "amount": 10, "direction": "out", "currency": "IDR"},
            {"txn_id": "NODEF", "date": "2026-10-04", "amount": 10, "direction": "out"},
            {"txn_id": "OK", "date": "2026-10-05", "amount": 10, "direction": "out", "currency": "IDR"},
        ],
    )
    res = _load(settings, tmp_path, payload)
    assert res.ok
    txns = {t["txn_id"]: t for t in res.data["data"]["transactions"]}
    quarantined = {q["record_id"] for q in res.data["quarantined"]}
    assert txns["NEG"]["direction"] == "out" and txns["NEG"]["amount"] == 50_000
    assert txns["USD"]["amount"] == pytest.approx(158_000)
    assert txns["NODEF"]["category"] == "uncategorized" and txns["NODEF"]["currency"] == "IDR"
    assert {"BADDATE", "JPY", "FUTURE", "OK"} <= quarantined  # second "OK" is a duplicate id
    actions = {(i["record_id"], i["action"]) for i in res.data["data_quality"]}
    assert ("NEG", "normalized") in actions and ("USD", "converted") in actions and ("NODEF", "defaulted") in actions


def test_missing_sections_default_to_empty(settings: Settings, tmp_path: Path) -> None:
    res = _load(settings, tmp_path, {"client": {"client_id": "X001"}, "as_of": "2026-10-10"})
    assert res.ok
    assert res.data["data"]["transactions"] == [] and res.data["data"]["holdings"] == []
    assert any(i["issue"].startswith("section 'transactions' missing") for i in res.data["data_quality"])


# ----------------------------------------------------------------------------- CRM resilience
def _crm(settings: Settings, mode: str | None, breaker: CircuitBreaker | None = None) -> CRMTool:
    client = MockCRMClient(settings.paths.crm_mock_path, forced_failure=mode, sleep=lambda _s: None)  # type: ignore[arg-type]
    return CRMTool(client, settings.crm, breaker)


def test_crm_success(settings: Settings) -> None:
    res = _crm(settings, None).execute({"client_id": "C001"})
    assert res.ok and res.data["context"]["risk_profile"]["category"] == "moderate"
    assert res.meta.retries == 0


def test_crm_flaky_recovers_with_retry(settings: Settings) -> None:
    res = _crm(settings, "flaky").execute({"client_id": "C001"})
    assert res.ok and res.meta.retries == 1


@pytest.mark.parametrize("mode,code", [("timeout", "crm_timeout"), ("error_500", "crm_server_error")])
def test_crm_failures_after_retries(settings: Settings, mode: str, code: str) -> None:
    res = _crm(settings, mode).execute({"client_id": "C001"})
    assert not res.ok and res.error.code == code
    assert res.meta.retries == settings.crm.max_retries


def test_crm_empty_and_not_found(settings: Settings) -> None:
    empty = _crm(settings, "empty").execute({"client_id": "C001"})
    assert empty.ok and empty.data["empty"] is True
    missing = _crm(settings, None).execute({"client_id": "NOPE"})
    assert not missing.ok and missing.error.code == "crm_not_found"


def test_circuit_breaker_opens_and_half_opens(settings: Settings) -> None:
    now = [0.0]
    breaker = CircuitBreaker("crm", failure_threshold=3, reset_seconds=10, clock=lambda: now[0])
    tool = _crm(settings, "error_500", breaker)
    assert tool.execute({"client_id": "C001"}).error.code == "crm_server_error"  # 3 attempts -> opens
    assert breaker.state == "open"
    assert tool.execute({"client_id": "C001"}).error.code == "circuit_open"
    now[0] = 11
    assert breaker.state == "half_open"
    healthy = _crm(settings, None, breaker)
    assert healthy.execute({"client_id": "C001"}).ok
    assert breaker.state == "closed"
