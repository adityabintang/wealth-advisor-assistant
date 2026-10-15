"""Composition root: the only place that knows concrete implementations.

Swap a tool, agent, memory store or LLM here (or via ``RuntimeOptions``) without touching
the orchestrator or the agents.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agents import AnalyzerAgent, DataFetcherAgent, RecommenderAgent
from .config import Settings
from .integrations.crm_mock import MockCRMClient
from .llm import LLMProvider, create_llm_provider
from .memory import LongTermMemory
from .orchestrator import Orchestrator
from .tools import ToolRegistry
from .tools.anomaly import AnomalyDetectionTool
from .tools.client_data import ClientDataTool
from .tools.crm import CRMTool
from .tools.memory import MemoryTool
from .tools.metrics import PortfolioMetricsTool
from .tools.narrative import NarrativeTool
from .tools.resilience import CircuitBreaker


@dataclass
class RuntimeOptions:
    crm_failure: str | None = None  # force a CRM failure mode for every client (demo)
    use_llm: bool = True
    memory_path: str | Path | None = None  # ":memory:" = ephemeral
    llm_provider: LLMProvider | None = None  # inject a provider directly (tests)
    sleep: Callable[[float], None] = field(default=time.sleep)  # mock CRM latency (tests can no-op)


@dataclass
class Application:
    settings: Settings
    tools: ToolRegistry
    memory: LongTermMemory
    orchestrator: Orchestrator
    crm_breaker: CircuitBreaker
    extras: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        self.memory.close()


def build_application(settings: Settings, options: RuntimeOptions | None = None) -> Application:
    opts = options or RuntimeOptions()
    memory = LongTermMemory(opts.memory_path or settings.paths.memory_path)

    crm_client = MockCRMClient(
        settings.paths.crm_mock_path,
        seed=settings.crm.seed,
        latency_ms=settings.crm.latency_ms,
        forced_failure=opts.crm_failure,  # type: ignore[arg-type]
        sleep=opts.sleep,
    )
    breaker = CircuitBreaker("crm", settings.crm.circuit_breaker.failure_threshold, settings.crm.circuit_breaker.reset_seconds)

    llm = opts.llm_provider if opts.llm_provider is not None else (create_llm_provider(settings.llm) if opts.use_llm else None)

    tools = ToolRegistry(
        [
            ClientDataTool(settings),
            CRMTool(crm_client, settings.crm, breaker),
            PortfolioMetricsTool(settings),
            AnomalyDetectionTool(settings),
            MemoryTool(memory),
            NarrativeTool(llm),
        ]
    )
    orchestrator = Orchestrator(
        settings,
        tools,
        data_fetcher=DataFetcherAgent(tools, settings),
        analyzer=AnalyzerAgent(tools, settings),
        recommender=RecommenderAgent(tools, settings),
    )
    return Application(settings=settings, tools=tools, memory=memory, orchestrator=orchestrator, crm_breaker=breaker)
