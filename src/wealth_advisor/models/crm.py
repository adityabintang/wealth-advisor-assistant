"""CRM context (mock API response, PRD §9)."""

from __future__ import annotations

from datetime import date
from datetime import date as _Date  # alias: a field named `date` shadows the type in class scope
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RiskCategory = Literal["conservative", "moderate", "aggressive"]


class RiskProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: RiskCategory
    max_equity_pct: float | None = Field(default=None, ge=0, le=1)
    score: int | None = None
    last_assessed: date | None = None
    assumed: bool = False  # True when not confirmed by CRM (default profile)


class Goal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal: str
    target_amount: float | None = None
    horizon_years: float | None = None
    priority: str | None = None


class Relationship(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tier: str | None = None
    last_contact_date: date | None = None
    advisor: str | None = None


class Note(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: _Date | None = None
    author: str | None = None
    text: str


class CRMContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_id: str
    status: str | None = None
    risk_profile: RiskProfile | None = None
    goals: list[Goal] = Field(default_factory=list)
    relationship: Relationship | None = None
    notes: list[Note] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.risk_profile is None and not self.goals and self.relationship is None and not self.notes
