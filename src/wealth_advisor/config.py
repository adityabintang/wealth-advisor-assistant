"""Configuration loading: YAML thresholds + environment variables (never secrets in code)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class RiskProfileLimits(BaseModel):
    max_equity_pct: float


class UnusualAmountConfig(BaseModel):
    robust_z_threshold: float = 3.5
    median_multiple: float = 5.0
    min_samples_per_category: int = 5
    high_severity_robust_z: float = 7.0
    high_severity_median_multiple: float = 10.0


class DuplicateConfig(BaseModel):
    window_hours: float = 24
    min_occurrences: int = 2


class FrequencySpikeConfig(BaseModel):
    sigma: float = 3.0
    min_days_observed: int = 14
    min_daily_count: int = 5


class LargeOutflowConfig(BaseModel):
    window_days: int = 30
    ratio_of_total_assets: float = 0.30


class NewCounterpartyConfig(BaseModel):
    min_amount: float = 100_000_000
    lookback_days: int = 30
    min_history_days: int = 30


class ConcentrationConfig(BaseModel):
    single_asset_pct: float = 0.40
    sector_pct: float = 0.40
    sector_exclusions: list[str] = Field(default_factory=list)
    single_asset_exclusion_sectors: list[str] = Field(default_factory=list)


class ValueDeclineConfig(BaseModel):
    window_days: int = 90
    threshold_pct: float = -0.10


class AnomalyConfig(BaseModel):
    min_transactions_for_statistics: int = 10
    unusual_amount: UnusualAmountConfig = UnusualAmountConfig()
    duplicate: DuplicateConfig = DuplicateConfig()
    frequency_spike: FrequencySpikeConfig = FrequencySpikeConfig()
    large_outflow: LargeOutflowConfig = LargeOutflowConfig()
    new_counterparty: NewCounterpartyConfig = NewCounterpartyConfig()
    concentration: ConcentrationConfig = ConcentrationConfig()
    value_decline: ValueDeclineConfig = ValueDeclineConfig()


class ConfidenceConfig(BaseModel):
    floor: float = 0.05
    penalties: dict[str, float] = Field(default_factory=dict)

    def penalty(self, key: str, default: float = 0.0) -> float:
        return float(self.penalties.get(key, default))


class HITLConfig(BaseModel):
    confidence_threshold: float = 0.70
    trigger_on_high_severity: bool = True
    trigger_on_degraded: bool = True


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 5
    reset_seconds: float = 30


class CRMConfig(BaseModel):
    timeout_seconds: float = 0.5
    max_retries: int = 2
    backoff_base_seconds: float = 0.2
    latency_ms: tuple[int, int] = (10, 40)
    seed: int = 42
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()


class OrchestratorConfig(BaseModel):
    step_max_attempts: int = 2


class RelationshipConfig(BaseModel):
    stale_contact_days: int = 180


class LLMSettings(BaseModel):
    """Provider-agnostic LLM settings. Empty provider = LLM disabled (template narrative)."""

    provider: str = ""
    model: str = ""
    temperature: float = 0.2
    timeout_seconds: float = 20

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.model)


class PathSettings(BaseModel):
    root: Path
    data_dir: Path
    clients_dir: Path
    crm_mock_path: Path
    log_dir: Path
    memory_path: Path
    output_dir: Path


class Settings(BaseModel):
    base_currency: str = "IDR"
    fx_rates: dict[str, dict[str, float]] = Field(default_factory=dict)
    risk_profiles: dict[str, RiskProfileLimits] = Field(default_factory=dict)
    default_risk_profile: str = "conservative"
    anomaly: AnomalyConfig = AnomalyConfig()
    confidence: ConfidenceConfig = ConfidenceConfig()
    hitl: HITLConfig = HITLConfig()
    crm: CRMConfig = CRMConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    relationship: RelationshipConfig = RelationshipConfig()
    llm: LLMSettings = LLMSettings()
    paths: PathSettings

    def fx_rate(self, from_ccy: str, to_ccy: str) -> float | None:
        """Static FX lookup (direct or inverse). Returns None when unknown."""
        f, t = from_ccy.upper(), to_ccy.upper()
        if f == t:
            return 1.0
        if f in self.fx_rates and t in self.fx_rates[f]:
            return self.fx_rates[f][t]
        if t in self.fx_rates and f in self.fx_rates[t] and self.fx_rates[t][f]:
            return 1.0 / self.fx_rates[t][f]
        return None

    def max_equity_for(self, category: str) -> float:
        limits = self.risk_profiles.get(category) or self.risk_profiles.get(self.default_risk_profile)
        return limits.max_equity_pct if limits else 0.30


def find_project_root() -> Path:
    """Resolve the project root: $WA_HOME, then cwd ancestors, then package location."""
    env = os.getenv("WA_HOME")
    if env:
        return Path(env).resolve()
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        if (candidate / "config" / "thresholds.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


def _resolve(root: Path, value: str | os.PathLike[str] | None, default: str) -> Path:
    path = Path(value) if value else Path(default)
    return path if path.is_absolute() else (root / path)


def load_settings(config_path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """Load settings from YAML + env. ``overrides`` are merged on top (used by tests/CLI)."""
    root = find_project_root()
    load_dotenv(root / ".env", override=False)

    cfg_path = _resolve(root, config_path or os.getenv("WA_CONFIG_PATH"), "config/thresholds.yaml")
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    data_dir = _resolve(root, os.getenv("WA_DATA_DIR"), "data")
    raw["paths"] = {
        "root": root,
        "data_dir": data_dir,
        "clients_dir": data_dir / "clients",
        "crm_mock_path": data_dir / "crm_mock.json",
        "log_dir": _resolve(root, os.getenv("WA_LOG_DIR"), "logs"),
        "memory_path": _resolve(root, os.getenv("WA_MEMORY_PATH"), ".state/memory.sqlite"),
        "output_dir": _resolve(root, os.getenv("WA_OUTPUT_DIR"), "output"),
    }
    raw["llm"] = {
        "provider": os.getenv("LLM_PROVIDER", "").strip(),
        "model": os.getenv("LLM_MODEL", "").strip(),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.2") or 0.2),
        "timeout_seconds": float(os.getenv("LLM_TIMEOUT_SECONDS", "20") or 20),
    }
    if overrides:
        raw = _deep_merge(raw, overrides)
    return Settings.model_validate(raw)


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
