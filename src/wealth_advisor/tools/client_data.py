"""ClientDataTool: load + validate + normalise a client's financial JSON (PRD FR-3, §11).

Policy:
  * Unparseable JSON / missing ``client`` section  -> ToolResult(ok=False) (fatal for this client)
  * Missing optional field                          -> default value + data-quality flag
  * Odd values (negative, bad date, mixed currency) -> normalise when safe, else quarantine the row
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..errors import ToolFailure
from ..models import (
    Account,
    ClientFinancialData,
    ClientInfo,
    DataQualityIssue,
    Holding,
    PortfolioSnapshot,
    QuarantinedRecord,
    Transaction,
)
from .base import Tool

_DIRECTION_ALIASES = {"in": "in", "credit": "in", "inflow": "in", "out": "out", "debit": "out", "outflow": "out"}


class ClientDataInput(BaseModel):
    client_id: str | None = None
    path: str | None = None


def find_client_file(clients_dir: Path, client_id: str) -> Path | None:
    """Files are named ``<client_id>.json`` or ``<client_id>_<scenario>.json``."""
    exact = clients_dir / f"{client_id}.json"
    if exact.exists():
        return exact
    matches = sorted(clients_dir.glob(f"{client_id}_*.json"))
    return matches[0] if matches else None


def client_id_from_path(path: Path) -> str:
    return path.stem.split("_", 1)[0]


class ClientDataTool(Tool[ClientDataInput]):
    name = "load_client_data"
    description = "Load and validate a client's financial data JSON (profile, accounts, holdings, transactions, history)."
    input_schema = ClientDataInput

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _run(self, params: ClientDataInput, meta: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(params)
        meta["source"] = path.name
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolFailure(
                f"Invalid JSON in '{path.name}': {exc.msg} (line {exc.lineno}, column {exc.colno})",
                code="invalid_json",
            ) from exc
        except UnicodeDecodeError as exc:
            raise ToolFailure(f"'{path.name}' is not UTF-8 text", code="invalid_json") from exc
        if not isinstance(raw, dict):
            raise ToolFailure(f"'{path.name}': top-level JSON must be an object", code="schema_error")

        normaliser = _Normaliser(self._settings, expected_client_id=params.client_id)
        data = normaliser.run(raw)
        return {
            "data": data.model_dump(mode="json"),
            "data_quality": [i.model_dump() for i in normaliser.issues],
            "quarantined": [q.model_dump(mode="json") for q in normaliser.quarantined],
            "source": path.name,
        }

    def _resolve_path(self, params: ClientDataInput) -> Path:
        if params.path:
            path = Path(params.path)
            if not path.exists():
                raise ToolFailure(f"Client data file '{path}' not found", code="not_found")
            return path
        if not params.client_id:
            raise ToolFailure("Either client_id or path is required", code="invalid_input")
        path = find_client_file(self._settings.paths.clients_dir, params.client_id)
        if path is None:
            raise ToolFailure(f"No data file for client '{params.client_id}'", code="not_found")
        return path


class _Normaliser:
    def __init__(self, settings: Settings, expected_client_id: str | None) -> None:
        self.s = settings
        self.expected_client_id = expected_client_id
        self.issues: list[DataQualityIssue] = []
        self.quarantined: list[QuarantinedRecord] = []

    # ----------------------------------------------------------------- entry
    def run(self, raw: dict[str, Any]) -> ClientFinancialData:
        client = self._client(raw.get("client"))
        base = client.base_currency
        accounts = self._rows("accounts", raw, Account, "account_id", lambda r: self._account(r, base))
        holdings = self._rows("holdings", raw, Holding, "asset_id", lambda r: self._holding(r, base))
        txns = self._rows("transactions", raw, Transaction, "txn_id", lambda r: self._transaction(r, base))
        history = self._rows("history", raw, PortfolioSnapshot, "date", lambda r: r, optional=True)

        as_of = self._as_of(raw.get("as_of"), txns)
        txns = self._drop_future_and_duplicates(txns, as_of)
        txns.sort(key=lambda t: t.date)
        history.sort(key=lambda h: h.date)
        return ClientFinancialData(
            client=client, as_of=as_of, accounts=accounts, holdings=holdings, transactions=txns, history=history
        )

    # ----------------------------------------------------------------- client
    def _client(self, raw: Any) -> ClientInfo:
        if not isinstance(raw, dict):
            raise ToolFailure("Missing required 'client' section", code="schema_error")
        if not raw.get("client_id"):
            raise ToolFailure("Missing required field 'client.client_id'", code="schema_error")
        raw = dict(raw)
        if self.expected_client_id and raw["client_id"] != self.expected_client_id:
            self._issue("client", raw["client_id"], "client_id", f"file client_id differs from requested '{self.expected_client_id}'", "noted")
        if not raw.get("name"):
            self._issue("client", raw["client_id"], "name", "name missing", "defaulted")
        if not raw.get("base_currency"):
            raw["base_currency"] = self.s.base_currency
            self._issue("client", raw["client_id"], "base_currency", f"base currency missing; assumed {self.s.base_currency}", "defaulted")
        if raw.get("date_of_birth") is None and raw.get("age") is None:
            self._issue("client", raw["client_id"], "date_of_birth", "age / date of birth unknown", "noted")
        try:
            return ClientInfo.model_validate(raw)
        except ValidationError as exc:
            # Drop invalid optional fields and retry; client_id itself is already present.
            bad = {str(e["loc"][0]) for e in exc.errors()}
            for field in bad - {"client_id"}:
                raw.pop(field, None)
                self._issue("client", raw["client_id"], field, "invalid value dropped", "defaulted")
            try:
                return ClientInfo.model_validate(raw)
            except ValidationError as exc2:
                raise ToolFailure(f"Invalid client section: {exc2.error_count()} error(s)", code="schema_error") from exc2

    # ----------------------------------------------------------------- generic rows
    def _rows(self, section: str, raw: dict[str, Any], model: type[BaseModel], id_field: str, prepare: Any, optional: bool = False) -> list[Any]:
        rows = raw.get(section)
        if rows is None:
            if not optional:
                self._issue(section, None, None, f"section '{section}' missing; treated as empty", "defaulted")
            else:
                self._issue(section, None, None, f"optional section '{section}' not provided", "noted")
            return []
        if not isinstance(rows, list):
            self._quarantine(section, None, f"section '{section}' is not a list", {"value": str(rows)[:200]})
            return []
        out: list[Any] = []
        for idx, row in enumerate(rows):
            rid = str(row.get(id_field)) if isinstance(row, dict) and row.get(id_field) is not None else f"{section}[{idx}]"
            if not isinstance(row, dict):
                self._quarantine(section, rid, "row is not an object", {"value": str(row)[:200]})
                continue
            try:
                prepared = prepare(dict(row))
                if prepared is None:
                    continue  # already quarantined by the preparer
                out.append(model.model_validate(prepared))
            except ValidationError as exc:
                reason = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
                self._quarantine(section, rid, f"validation failed ({reason})", row)
        return out

    # ----------------------------------------------------------------- per-section preparers
    def _account(self, row: dict[str, Any], base: str) -> dict[str, Any] | None:
        rid = str(row.get("account_id", "?"))
        if not row.get("type"):
            row["type"] = "savings"
            self._issue("accounts", rid, "type", "account type missing; assumed savings", "defaulted")
        row = self._currency(row, "accounts", rid, base, amount_fields=("balance",))
        if row is not None and isinstance(row.get("balance"), (int, float)) and row["balance"] < 0 and row["type"] not in ("credit", "loan"):
            self._issue("accounts", rid, "balance", "negative balance on a non-credit account (overdraft?)", "noted")
        return row

    def _holding(self, row: dict[str, Any], base: str) -> dict[str, Any] | None:
        rid = str(row.get("asset_id", "?"))
        if not row.get("sector"):
            row["sector"] = "Unknown"
            self._issue("holdings", rid, "sector", "sector missing", "defaulted")
        if not row.get("asset_class"):
            row["asset_class"] = "other"
            self._issue("holdings", rid, "asset_class", "asset class missing; treated as 'other'", "defaulted")
        if row.get("cost_basis") is None:
            self._issue("holdings", rid, "cost_basis", "cost basis missing; P&L excludes this holding", "noted")
        mv = row.get("market_value")
        if isinstance(mv, (int, float)) and mv < 0:
            self._quarantine("holdings", rid, "negative market value", row)
            return None
        return self._currency(row, "holdings", rid, base, amount_fields=("market_value", "cost_basis"))

    def _transaction(self, row: dict[str, Any], base: str) -> dict[str, Any] | None:
        rid = str(row.get("txn_id", "?"))
        original = dict(row)
        for field, default in (("category", "uncategorized"), ("counterparty", "unknown")):
            if not row.get(field):
                row[field] = default
                self._issue("transactions", rid, field, f"{field} missing", "defaulted")

        direction = _DIRECTION_ALIASES.get(str(row.get("direction", "")).strip().lower())
        amount = row.get("amount")
        if not isinstance(amount, (int, float)):
            self._quarantine("transactions", rid, "amount missing or not numeric", original)
            return None
        if amount == 0:
            self._quarantine("transactions", rid, "zero amount", original)
            return None
        if amount < 0:
            if direction in (None, "out"):
                row["amount"], direction = abs(amount), "out"
                self._issue("transactions", rid, "amount", "negative amount normalised to outflow", "normalized")
            else:
                self._quarantine("transactions", rid, "negative amount with direction 'in' is ambiguous", original)
                return None
        if direction is None:
            self._quarantine("transactions", rid, "direction missing or invalid", original)
            return None
        row["direction"] = direction
        return self._currency(row, "transactions", rid, base, amount_fields=("amount",))

    def _currency(self, row: dict[str, Any], section: str, rid: str, base: str, amount_fields: tuple[str, ...]) -> dict[str, Any] | None:
        ccy = row.get("currency")
        if not ccy:
            row["currency"] = base
            self._issue(section, rid, "currency", f"currency missing; assumed {base}", "defaulted")
            return row
        ccy = str(ccy).upper()
        if ccy == base:
            row["currency"] = base
            return row
        rate = self.s.fx_rate(ccy, base)
        if rate is None:
            self._quarantine(section, rid, f"currency {ccy} cannot be converted to {base} (no static FX rate)", row)
            return None
        for field in amount_fields:
            if isinstance(row.get(field), (int, float)):
                row[field] = row[field] * rate
        row["currency"] = base
        self._issue(section, rid, "currency", f"converted {ccy}->{base} at static rate {rate:g}", "converted")
        return row

    # ----------------------------------------------------------------- post-processing
    def _as_of(self, raw_as_of: Any, txns: list[Transaction]) -> date:
        if raw_as_of:
            try:
                return date.fromisoformat(str(raw_as_of)[:10])
            except ValueError:
                self._issue("client", None, "as_of", f"invalid as_of '{raw_as_of}'", "defaulted")
        if txns:
            as_of = max(t.date for t in txns).date()
            self._issue("client", None, "as_of", f"as_of missing; using latest transaction date {as_of}", "defaulted")
            return as_of
        today = date.today()
        self._issue("client", None, "as_of", f"as_of missing; using today {today}", "defaulted")
        return today

    def _drop_future_and_duplicates(self, txns: list[Transaction], as_of: date) -> list[Transaction]:
        limit = datetime.combine(as_of + timedelta(days=1), datetime.min.time())
        seen: set[str] = set()
        out: list[Transaction] = []
        for t in txns:
            if t.date.replace(tzinfo=None) >= limit:
                self._quarantine("transactions", t.txn_id, f"date {t.date.date()} is after as_of {as_of}", t.model_dump(mode="json"))
                continue
            if t.txn_id in seen:
                self._quarantine("transactions", t.txn_id, "duplicate txn_id", t.model_dump(mode="json"))
                continue
            seen.add(t.txn_id)
            out.append(t)
        return out

    # ----------------------------------------------------------------- recording
    def _issue(self, section: str, rid: str | None, field: str | None, issue: str, action: str) -> None:
        self.issues.append(DataQualityIssue(section=section, record_id=rid, field=field, issue=issue, action=action))  # type: ignore[arg-type]

    def _quarantine(self, section: str, rid: str | None, reason: str, raw: dict[str, Any]) -> None:
        self.quarantined.append(QuarantinedRecord(section=section, record_id=rid, reason=reason, raw=_jsonable(raw)))
        self._issue(section, rid, None, reason, "quarantined")


def _jsonable(raw: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(raw, default=str))
