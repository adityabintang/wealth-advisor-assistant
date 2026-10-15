"""Client financial data (input JSON, PRD §9). Row-level models are validated one by one
so a single bad row is quarantined instead of failing the whole client."""

from __future__ import annotations

from datetime import date, datetime
from datetime import date as _Date  # alias: a field named `date` shadows the type in class scope
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AssetClass = Literal["equity", "fixed_income", "cash", "commodity", "real_estate", "alternative", "other"]

_ASSET_CLASS_ALIASES = {
    "stock": "equity",
    "stocks": "equity",
    "equities": "equity",
    "bond": "fixed_income",
    "bonds": "fixed_income",
    "fixed income": "fixed_income",
    "money_market": "cash",
    "money market": "cash",
    "gold": "commodity",
    "property": "real_estate",
}


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ClientInfo(_Row):
    client_id: str = Field(min_length=1)
    name: str = "Unknown"
    date_of_birth: date | None = None
    age: int | None = Field(default=None, ge=0, le=130)
    base_currency: str = "IDR"
    segment: str | None = None

    def effective_age(self, as_of: date) -> int | None:
        if self.age is not None:
            return self.age
        if self.date_of_birth:
            dob = self.date_of_birth
            return as_of.year - dob.year - ((as_of.month, as_of.day) < (dob.month, dob.day))
        return None


class Account(_Row):
    account_id: str
    type: str = "savings"
    balance: float
    currency: str


class Holding(_Row):
    asset_id: str
    name: str | None = None
    asset_class: AssetClass = "other"
    sector: str = "Unknown"
    quantity: float = Field(default=0.0, ge=0)
    market_value: float = Field(ge=0)
    cost_basis: float | None = Field(default=None, ge=0)
    currency: str

    @field_validator("asset_class", mode="before")
    @classmethod
    def _normalise_class(cls, v: object) -> object:
        if isinstance(v, str):
            key = v.strip().lower()
            return _ASSET_CLASS_ALIASES.get(key, key)
        return v


class Transaction(_Row):
    txn_id: str
    date: datetime
    amount: float = Field(gt=0)
    direction: Literal["in", "out"]
    category: str = "uncategorized"
    counterparty: str = "unknown"
    account_id: str | None = None
    currency: str

    @field_validator("date", mode="before")
    @classmethod
    def _date_only(cls, v: object) -> object:
        # Accept plain dates ("2026-09-01") as midnight.
        if isinstance(v, str) and len(v) == 10:
            return f"{v}T00:00:00"
        return v

    @field_validator("date", mode="after")
    @classmethod
    def _naive(cls, v: datetime) -> datetime:
        # Single-timezone assumption: keep wall-clock time, drop tzinfo for consistent arithmetic.
        return v.replace(tzinfo=None)


class PortfolioSnapshot(_Row):
    date: _Date
    total_value: float = Field(ge=0)


class ClientFinancialData(BaseModel):
    """Normalised, validated client data (all amounts in the client's base currency)."""

    client: ClientInfo
    as_of: date
    accounts: list[Account] = Field(default_factory=list)
    holdings: list[Holding] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)
    history: list[PortfolioSnapshot] = Field(default_factory=list)

    @property
    def base_currency(self) -> str:
        return self.client.base_currency
