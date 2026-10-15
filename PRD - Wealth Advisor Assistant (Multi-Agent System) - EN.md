# PRD — Wealth Advisor Assistant (Multi-Agent System)

## 1. Summary

Wealth Advisor Assistant is a lightweight multi-agent system that helps a wealth advisor prepare advisory decisions for clients. The system takes a client's financial data (JSON), enriches it with context from a CRM (mock API), analyses the portfolio and transactions (including anomaly detection), and produces structured output (summary, risk signals, recommended actions) that the advisor can validate before it is finalised.


## 2. Problem and Opportunity

- Advisors have to read many sources (transactions, CRM notes, risk profiles) by hand before meeting a client.
- Risk signals such as unusual transactions, asset concentration or large outflows are easy to miss.
- Analysis results are usually unstructured, so they are hard to audit and to compare over time.

Opportunity: split responsibilities across specialist agents coordinated by an orchestrator, wrap all data access in a tool abstraction, and produce structured output with a complete audit trail.

## 3. Goals, Non-Goals and Metrics

**Goals**

- End-to-end pipeline: ingest client data → fetch CRM context → analysis + anomaly detection → structured advisory output.
- A clear multi-agent architecture: 1 orchestrator + at least 2 specialist agents.
- Tool abstraction: agents do not depend on raw implementations.
- The system stays stable under failure (API errors, missing data), with documented fallbacks.
- Structured logging for observability.
- Bonus: a memory layer (short-term and long-term) and a human-in-the-loop (HITL) checkpoint.

**Non-goals**

- Executing real trades or transactions.
- Authentication, KYC, or production CRM integration.
- A production web UI (a CLI is enough).
- Giving final investment advice: the final decision always rests with the advisor.

**Success metrics**

| Metric | Target |
| --- | --- |
| End-to-end run on all sample clients (including edge cases) | 100% complete without crashing |
| Recall of anomalies deliberately injected into the mock data | ≥ 90% |
| Execution steps recorded in the log with a run\_id | 100% |
| Behaviour when the CRM is down | Degraded mode + fallback, not a crash |
| Setup from a clean repo to a running system | ≤ 3 commands |
| Execution time per client (offline mode) | < 10 seconds |

## 4. Users and User Stories

**Primary persona:** a Wealth Advisor preparing a client review. **Secondary persona:** a technical reviewer assessing the design and code.

- As an advisor, I want a single summary of the client's financial situation so I don't have to open many sources.
- As an advisor, I want anomalies flagged with evidence and a severity level so I can follow up on what matters.
- As an advisor, I want to be able to approve, change or reject recommendations before they are final.
- As an advisor, I want the system to still give a result (with warnings) when the CRM is unavailable.
- As a reviewer, I want logs that explain who decided what and why.

## 5. Scope

**In scope (P0):** orchestrator, Data Fetcher Agent, Analyzer Agent, tool registry, mock CRM, anomaly detection, structured output, error handling, logging, README, sample input/output.

**In scope (bonus, P1):** short-term memory, long-term memory, HITL checkpoint, a separate Recommender Agent.

**Out of scope:** web UI, cloud deployment, real-time market data, multi-user, authentication.

## 6. Functional Requirements

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-1 | The orchestrator accepts a request (client\_id) and creates a run\_id and session context | P0 |
| FR-2 | The orchestrator runs agents in sequence, records the status of each step (pending, running, success, failed, skipped), and handles failures per step | P0 |
| FR-3 | The Data Fetcher loads and validates the client's financial data JSON (profile, accounts, portfolio, transactions) | P0 |
| FR-4 | The Data Fetcher calls the CRM API (mock) via a tool for client context: risk profile, interaction notes, goals, status | P0 |
| FR-5 | The Analyzer computes portfolio metrics: total assets, allocation by asset class, concentration, 30/90-day cash flow, change in value | P0 |
| FR-6 | The Analyzer runs anomaly detection (see section 10) and produces a list of findings with severity, evidence and explanation | P0 |
| FR-7 | The system produces a structured AdvisoryOutput (schema-validated JSON) containing a summary, findings, recommended actions, confidence and a degraded flag | P0 |
| FR-8 | All external access (files, CRM, memory, LLM) goes through the Tool interface | P0 |
| FR-9 | Error handling: retry, timeout, fallback and partial results (see section 11) | P0 |
| FR-10 | Structured logging of execution flow, decisions and errors, correlated by run\_id | P0 |
| FR-11 | Short-term memory: per-run session context shared between agents | P1 |
| FR-12 | Long-term memory: store analysis summaries and advisor decisions per client, used to compare trends and reduce repeated alerts | P1 |
| FR-13 | HITL checkpoint before finalisation under certain conditions (see section 14) | P1 |
| FR-14 | CLI: run for one client or for all sample clients; every run produces two outputs: validated JSON and a text report for the advisor | P0 |
| FR-15 | LLM-written summary narrative is optional and provider-agnostic (provider and model chosen via configuration), with a template fallback when no LLM is available | P2 |

## 7. Architecture

**High-level flow**

```
CLI / Request
     │
     ▼
Orchestrator ──(session memory, logger, tool registry)
     │
     ├─1─► Data Fetcher Agent ──► ClientDataTool, CRMTool
     │         └─► ClientContext (+ data quality flags)
     │
     ├─2─► Analyzer Agent ──► PortfolioMetricsTool, AnomalyDetectionTool
     │         └─► AnalysisReport (metrics + anomalies)
     │
     ├─3─► Recommender Agent ──► (optional LLM) / template
     │         └─► AdvisoryDraft
     │
     ├─4─► HITL Checkpoint (approve / edit / reject)
     │
     └─5─► Finalize ──► AdvisoryOutput + save to long-term memory
```

**Components**

| Component | Responsibility | Input → Output |
| --- | --- | --- |
| Orchestrator | Central coordinator (LangGraph graph): routing, execution order, step-level retry, degraded-mode decisions, HITL | request → AdvisoryOutput |
| Data Fetcher Agent | Fetch, validate and normalise data from files and the CRM | client\_id → ClientContext |
| Analyzer Agent | Financial metrics and anomaly detection | ClientContext → AnalysisReport |
| Recommender Agent | Build recommendations and a summary from the findings | AnalysisReport → AdvisoryDraft |
| Tool Registry | List of tools with a uniform interface | name → Tool |
| Memory Store | Short-term (in-process) and long-term (SQLite/JSON) | key → value |

**Key design decisions**

- **The orchestrator is a LangGraph graph (StateGraph) with deterministic routing.** Each agent is a node, and transitions between nodes (including the degraded and HITL paths) are conditional edges defined in code, not by an LLM. Rationale: behaviour is predictable and easy to test and log, and state, checkpointer and interrupt for HITL come built in. Trade-off: less flexible than dynamic routing by an LLM.
- **LangGraph is only the orchestration layer.** Agents and tools remain plain Python classes that do not import LangGraph; the graph only connects them. Rationale: modularity is preserved and agents can be tested without the graph. Trade-off: an extra dependency and learning curve, and the LangGraph API changes quickly, so its version is pinned.
- **The LLM is provider-agnostic.** Agents only know the `LLMProvider` interface; its implementation wraps a LangChain chat model, so OpenAI, Anthropic, Gemini or a local model (Ollama) can be used just by changing configuration (`LLM_PROVIDER`, `LLM_MODEL`, API key via env). No default provider is hard-coded. Trade-off: provider-specific features are not used.
- **Anomaly detection is rule-based + statistical** (not an ML model). Rationale: the mock data is small, results are explainable, and no training is needed. Trade-off: less adaptive to new patterns.
- **The LLM is optional** and used only for the narrative. The core analysis stays deterministic so the system runs offline and results are reproducible; with no LLM configured, the narrative uses a template.

## 8. Tool Abstraction

Every tool implements the same interface:

- `name` and `description`
- `input_schema` (input validation)
- `execute(input) → ToolResult` with the fields `ok`, `data`, `error` and `meta` (duration, retry count)

Agents call tools through `ToolRegistry.get(name)` and never import raw implementations. Tools do not raise exceptions to agents: failures are returned as `ToolResult(ok=False, error=...)` so that error handling is consistent.

| Tool | Function |
| --- | --- |
| ClientDataTool | Loads and validates the client's financial data JSON |
| CRMTool | Calls the mock CRM API (supports failure injection: timeout, 500, empty data) |
| PortfolioMetricsTool | Computes allocation, concentration, cash flow and change in value |
| AnomalyDetectionTool | Runs all anomaly detectors |
| MemoryTool | Reads/writes short-term and long-term memory |
| NarrativeTool (optional) | Generates the narrative via LLMProvider (any configured provider), falls back to a template |

## 9. Data Model

**Client Financial Data (JSON)**

| Section | Main fields |
| --- | --- |
| client | client\_id, name, date of birth/age, base currency |
| accounts | account\_id, type, balance, currency |
| portfolio / holdings | asset\_id, asset class, sector, quantity, market value, cost basis |
| transactions | txn\_id, date, amount, direction (in/out), category, counterparty, account\_id |
| history (optional) | 90-day portfolio value snapshots |

**CRM API (mock)**

| Field | Description |
| --- | --- |
| risk\_profile | conservative / moderate / aggressive, plus allocation limits |
| goals | financial goals and time horizon |
| relationship | client tier, last contact date, advisor |
| notes | latest interaction notes |

**AdvisoryOutput (output schema)**

| Field | Description |
| --- | --- |
| run\_id, client\_id, generated\_at | metadata |
| summary | summary of the client's situation |
| metrics | portfolio metrics |
| anomalies\[\] | type, severity (low/medium/high), evidence, explanation |
| recommendations\[\] | action, rationale, priority, linked\_anomaly\_ids |
| confidence | 0–1, decreases when data is incomplete |
| degraded | true when any data source failed, plus a list of reasons |
| review | status (approved / edited / rejected / pending\_review), reviewer note |

## 10. Anomaly Detection Specification

| Type | Method | Initial threshold | Severity |
| --- | --- | --- | --- |
| Unusually large transaction | Robust z-score (median/MAD) per category | > 3.5 or > 5x median | Medium–High |
| Duplicate transaction | Same amount + counterparty within 24 hours | ≥ 2 occurrences | Medium |
| Frequency spike | Daily transaction count vs mean + 3σ | > mean + 3σ | Medium |
| Large outflow | 30-day net outflow / total assets | > 30% | High |
| High-value new counterparty | First-time counterparty with value above a threshold | Configurable | Medium |
| Asset concentration | Weight of a single asset or sector | > 40% | Medium |
| Deviation from risk profile | Equity allocation vs the CRM profile limit | Exceeds the limit | High |
| Declining value trend | 90-day change in portfolio value | < -10% | Medium |

All thresholds are stored in a configuration file. Every finding must include evidence (a txn\_id or computed figures) and a short explanation. When there is not enough data for the statistical methods (for example, < 10 transactions), the statistical detectors are skipped, the simple rules still run, and confidence is lowered.

## 11. Error Handling and Fallback

| Failure | Handling | Impact on the result |
| --- | --- | --- |
| CRM timeout / 5xx | Retry 2x with exponential backoff; then a circuit breaker; fall back to the last CRM context in long-term memory, or continue without the CRM | degraded = true, confidence lowered |
| Empty CRM data / no risk profile | Use a conservative default profile and flag it as an assumption | Warning in the output |
| Invalid JSON | Fatal for that client; clear error; other runs continue | Controlled run failure |
| Missing optional field | Schema validation, field filled with a default, data quality flag | Partial analysis |
| Odd values (negative, invalid date, mixed currencies) | Normalise when safe; otherwise quarantine the row and record it | Row skipped, recorded in the log |
| Exception inside an agent | The orchestrator catches it, marks the step failed, and continues if the step is non-critical | Partial result |
| LLM unavailable | Template-based narrative fallback | No change to the findings |
| HITL with no response (non-interactive) | Status pending\_review; never silently auto-approved | Requires manual review |

Principle: fail in a controlled way, always produce either a result or a clear error, and make every failure visible in the log and in the output (degraded flag).

## 12. Logging and Observability

- Structured logs (JSON lines) to a file and a summary to the console, with the fields: timestamp, level, run\_id, agent, step, event, duration\_ms, detail.
- Required events: run\_started, step\_started/finished, tool\_called (brief input, result, duration, retries), decision (for example entering degraded mode or triggering HITL), anomaly\_detected, error, run\_finished.
- Levels: DEBUG (tool details), INFO (flow), WARNING (fallback), ERROR (failure).
- Sensitive data (full names, account numbers) is masked in the logs.

## 13. Memory Layer (Bonus)

**Short-term:** per-run session context (intermediate results, data quality flags, interim decisions). It lives for one run, is stored as LangGraph graph state, and is shared between agents via the Orchestrator.

**Long-term:** persistent storage (SQLite or a JSON file) holding analysis summaries per client, previous anomalies and advisor decisions. It is used to (a) compare trends across runs, (b) flag recurring anomalies so the same alert is not repeated, and (c) provide the last CRM context as a fallback.

## 14. Human-in-the-Loop Checkpoint (Bonus)

The checkpoint runs before finalisation if any of these holds: there is a High-severity anomaly, confidence is below the threshold, or the output is in degraded mode.

- The advisor sees a summary of the findings and recommendations in the CLI, then chooses: **approve**, **edit** (change or remove recommendations), or **reject**.
- Non-interactive mode (`--auto-approve` for demo/CI) is recorded explicitly in the log and in the review field.
- Every override is recorded (who, when, what changed) in the log and in long-term memory; the checkpoint uses LangGraph's interrupt and resume mechanism.

## 15. Non-Functional Requirements

- Modular: agents, tools and memory can be replaced without changing the Orchestrator.
- Clean code: type hints, validated data models (Pydantic), short docstrings, configuration separate from code.
- Tested: unit tests for the anomaly detectors, tools and error paths; one end-to-end integration test.
- Reproducible: mock data and failure injection with a fixed seed.
- Secure: no secrets in code; a `.env.example` is provided; PII is masked in logs.
- Easy to run: one setup command and one run command.

## 16. Recommended Tech Stack

| Need | Choice |
| --- | --- |
| Language | Python 3.11+ |
| Agent orchestration | LangGraph (StateGraph: one node per agent, conditional edges, checkpointer, interrupt for HITL) |
| LLM abstraction | A thin `LLMProvider` interface over a LangChain chat model (`init_chat_model`); provider and model chosen via configuration, e.g. OpenAI, Anthropic, Gemini or Ollama |
| Data validation | Pydantic v2 |
| CLI | Typer or argparse, with terminal output using `rich` (coloured anomaly tables, compact logs) |
| HTTP / mock CRM | In-process mock class, optionally FastAPI + httpx |
| Short-term memory | LangGraph graph state + in-memory checkpointer |
| Long-term memory | SQLite |
| Logging | Standard `logging` with a JSON formatter |
| Testing | pytest |

## 17. Repository Structure

```
wealth-advisor-assistant/
├── README.md
├── pyproject.toml
├── .env.example            # LLM_PROVIDER, LLM_MODEL, API key (optional)
├── config/thresholds.yaml
├── data/
│   ├── clients/            # sample JSON (normal, anomalies, incomplete data, CRM down)
│   └── crm_mock.json
├── src/wealth_advisor/
│   ├── orchestrator.py     # LangGraph graph: state, nodes, edges
│   ├── agents/             # base.py, data_fetcher.py, analyzer.py, recommender.py
│   ├── tools/              # base.py, registry.py, client_data.py, crm.py, anomaly.py, metrics.py, memory.py
│   ├── llm/                # base.py (LLMProvider), factory.py
│   ├── memory/             # short_term.py, long_term.py
│   ├── models/             # Pydantic schemas
│   ├── hitl.py
│   ├── logging_setup.py
│   └── cli.py
├── tests/
└── samples/output/         # sample output for each scenario
```

## 18. Deliverables

1. A GitHub repository containing working code.
2. A README (English recommended, matching the language of the assignment) covering: architecture, agent interaction flow, decisions and trade-offs, assumptions, and instructions for running.
3. Sample input/output for the following scenarios:

| Scenario | Purpose |
| --- | --- |
| Normal client | Success path with no anomalies |
| Client with injected anomalies | Validate detection and HITL |
| Incomplete data / no transactions | Partial analysis and lower confidence |
| CRM down | Retry, fallback, degraded mode |
| Broken JSON | Controlled error without stopping other runs |

## 19. 24-Hour Work Plan

| Hours | Work |
| --- | --- |
| 0–2 | Finalise design, data schema, repo structure |
| 2–5 | Mock data, Pydantic models, tool interface and registry |
| 5–9 | Data Fetcher, mock CRM + failure injection, error handling |
| 9–14 | Analyzer, portfolio metrics, anomaly detectors + tests |
| 14–17 | Orchestrator, Recommender, logging |
| 17–20 | Memory layer and HITL |
| 20–23 | Integration test, sample output, README |
| 23–24 | Final review, run from a clean repo, submit |

If time runs short, cut from the bottom: first the Recommender and NarrativeTool, then long-term memory. Do not cut error handling, logging or the README.

## 20. Assumptions and Risks

**Assumptions**

- Data and CRM may be mocked; there is no access to production systems.
- One client is processed per run; batch mode is just a simple loop.
- One base currency per client; currency conversion is out of scope (mixed-currency rows are quarantined or converted using static rates from the configuration).
- Anomaly thresholds are examples and configurable, not regulatory standards.
- The output is a decision-support tool, not investment advice.

**Risks**

| Risk | Mitigation |
| --- | --- |
| Scope creep from bonus features | Do P0 first; bonus features only once the pipeline is stable |
| Over-engineered abstractions | Minimal interfaces: one base class each for Tool and Agent |
| Detectors produce false positives | Configurable thresholds, tiered severity, evidence included |
| LLM dependency harms reproducibility | Core analysis is deterministic; LLM used only for the optional narrative |

## 21. Acceptance Criteria and Evaluation Mapping

| Evaluation criterion | Evidence in the project |
| --- | --- |
| System design and architectural clarity | Section 7 + diagram and flow explanation in the README |
| Agent abstraction and modularity | Agent/Tool base classes, registry, agents do not import tool implementations |
| Code quality and maintainability | Type hints, Pydantic, separate configuration, tests |
| Edge-case handling | 5 sample scenarios (section 18) + error matrix (section 11) |
| Rigour of trade-offs | Decisions and trade-offs section in the README |

Definition of done: all P0 requirements are met, tests pass, the five scenarios produce output or a controlled error, and the project runs from a clean repo by following the README.

## 22. Open Questions

- Is use of an LLM (for example for the narrative) expected, or is a rule-based system enough? This PRD's default: optional and provider-agnostic, with an offline mode.
- Is there a required JSON schema for client data? If so, section 9 needs to be adjusted.
