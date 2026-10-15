"""Orchestrator: a LangGraph ``StateGraph`` with deterministic routing (PRD §7).

    START -> fetch_data --(fatal?)--> finalize_failed -> END
                 |
                 v
              analyze -> recommend -> hitl_gate --(review needed?)--> human_review -> finalize -> END
                                           \\________________________________________/

* Agents are injected; the graph only wires them together (agents never import LangGraph).
* Every node goes through ``_execute`` which records step status, attempts, duration and errors,
  retries transient failures, and turns exceptions into controlled failures / partial results.
* HITL uses LangGraph ``interrupt`` + ``Command(resume=...)`` with an in-memory checkpointer.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .agents import AnalyzerAgent, DataFetcherAgent, RecommenderAgent
from .config import Settings
from .confidence import compute_confidence
from .errors import FatalStepError, TransientStepError, WealthAdvisorError
from .hitl import Reviewer, apply_decision, not_required_review, review_triggers
from .logging_setup import bind_context, log_event
from .memory.short_term import WorkflowState
from .models import (
    AdvisoryDraft,
    AdvisoryOutput,
    AnalysisReport,
    ClientContext,
    ConfidencePenalty,
    ErrorInfo,
    ReviewDecision,
    ReviewInfo,
    RunResult,
    StepRecord,
)
from .tools import MEMORY, ToolRegistry

STEPS = ("fetch_data", "analyze", "recommend", "human_review", "finalize")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        tools: ToolRegistry,
        data_fetcher: DataFetcherAgent,
        analyzer: AnalyzerAgent,
        recommender: RecommenderAgent,
    ) -> None:
        self.settings = settings
        self.tools = tools
        self.data_fetcher = data_fetcher
        self.analyzer = analyzer
        self.recommender = recommender
        self.graph = self._build_graph()

    # ================================================================== graph
    def _build_graph(self) -> Any:
        g = StateGraph(WorkflowState)
        g.add_node("fetch_data", self._node_fetch)
        g.add_node("analyze", self._node_analyze)
        g.add_node("recommend", self._node_recommend)
        g.add_node("hitl_gate", self._node_gate)
        g.add_node("human_review", self._node_review)
        g.add_node("finalize", self._node_finalize)
        g.add_node("finalize_failed", self._node_failed)

        g.add_edge(START, "fetch_data")
        g.add_conditional_edges("fetch_data", self._route_after_fetch, {"analyze": "analyze", "failed": "finalize_failed"})
        g.add_edge("analyze", "recommend")
        g.add_edge("recommend", "hitl_gate")
        g.add_conditional_edges("hitl_gate", self._route_after_gate, {"review": "human_review", "finalize": "finalize"})
        g.add_edge("human_review", "finalize")
        g.add_edge("finalize", END)
        g.add_edge("finalize_failed", END)
        return g.compile(checkpointer=InMemorySaver())

    # ------------------------------------------------------------------ routing (pure, deterministic)
    @staticmethod
    def _route_after_fetch(state: WorkflowState) -> str:
        return "failed" if state.get("fatal_error") else "analyze"

    @staticmethod
    def _route_after_gate(state: WorkflowState) -> str:
        return "review" if state.get("review_reasons") else "finalize"

    # ------------------------------------------------------------------ step wrapper
    def _execute(
        self,
        state: WorkflowState,
        step: str,
        agent: str,
        fn: Callable[[WorkflowState], dict[str, Any]],
        on_failure: Callable[[Exception], dict[str, Any]],
    ) -> dict[str, Any]:
        with bind_context(run_id=state["run_id"], agent=agent, step=step):
            rec = StepRecord(name=step, status="running", started_at=_now())
            log_event("step_started", f"{step} started")
            t0 = time.perf_counter()
            update: dict[str, Any] | None = None
            error: Exception | None = None
            max_attempts = max(1, self.settings.orchestrator.step_max_attempts)
            for attempt in range(1, max_attempts + 1):
                rec.attempts = attempt
                try:
                    update = fn(state)
                    error = None
                    break
                except GraphBubbleUp:
                    raise  # HITL interrupt: let LangGraph pause the run
                except TransientStepError as exc:
                    error = exc
                    log_event("retry", f"{step} transient failure (attempt {attempt}/{max_attempts}): {exc}", level=logging.WARNING)
                except Exception as exc:  # noqa: BLE001 - orchestrator owns failure handling
                    error = exc
                    break
            rec.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            rec.finished_at = _now()
            if error is None and update is not None:
                rec.status = "success"
                log_event("step_finished", f"{step} succeeded", duration_ms=rec.duration_ms, status="success")
            else:
                assert error is not None
                code = getattr(error, "code", type(error).__name__)
                rec.status = "failed"
                rec.error = f"{code}: {error}"
                expected = isinstance(error, WealthAdvisorError)
                log_event(
                    "error",
                    f"{step} failed: {rec.error}",
                    level=logging.ERROR,
                    duration_ms=rec.duration_ms,
                    error_code=code,
                    error_type=type(error).__name__,
                    expected=expected,
                )
                if not expected:
                    logging.getLogger("wealth_advisor").debug("traceback", exc_info=error, extra={"event": "error_traceback", "detail": {}})
                update = on_failure(error)
                log_event("step_finished", f"{step} failed", level=logging.WARNING, duration_ms=rec.duration_ms, status="failed")
            update = dict(update)
            update["steps"] = {step: rec.model_dump()}
            return update

    # ------------------------------------------------------------------ nodes
    def _node_fetch(self, state: WorkflowState) -> dict[str, Any]:
        def run(s: WorkflowState) -> dict[str, Any]:
            ctx = self.data_fetcher.run(s["client_id"], s.get("source_path"))
            return {
                "context": ctx.model_dump(mode="json"),
                "penalties": [p.model_dump() for p in ctx.penalties],
                "degraded_reasons": list(ctx.degraded_reasons),
            }

        def failed(exc: Exception) -> dict[str, Any]:
            code = exc.code if isinstance(exc, FatalStepError) else "data_fetch_failed"
            return {"fatal_error": ErrorInfo(code=code, message=str(exc), step="fetch_data").model_dump()}

        return self._execute(state, "fetch_data", self.data_fetcher.name, run, failed)

    def _node_analyze(self, state: WorkflowState) -> dict[str, Any]:
        def run(s: WorkflowState) -> dict[str, Any]:
            report = self.analyzer.run(ClientContext.model_validate(s["context"]))
            return {"report": report.model_dump(mode="json"), "penalties": [p.model_dump() for p in report.penalties],
                    "warnings": report.warnings}

        def failed(exc: Exception) -> dict[str, Any]:
            # Non-critical: continue with a partial result.
            p = ConfidencePenalty(reason=f"analysis failed: {exc}", amount=self.settings.confidence.penalty("analysis_failed", 0.4), source="orchestrator")
            log_event("decision", "analysis failed; continuing with partial result", level=logging.WARNING)
            return {"report": None, "penalties": [p.model_dump()], "degraded_reasons": [f"analysis step failed ({exc})"]}

        return self._execute(state, "analyze", self.analyzer.name, run, failed)

    def _node_recommend(self, state: WorkflowState) -> dict[str, Any]:
        def run(s: WorkflowState) -> dict[str, Any]:
            report = AnalysisReport.model_validate(s["report"]) if s.get("report") else None
            draft = self.recommender.run(ClientContext.model_validate(s["context"]), report)
            return {"draft": draft.model_dump(mode="json")}

        def failed(exc: Exception) -> dict[str, Any]:
            p = ConfidencePenalty(reason=f"recommendation failed: {exc}", amount=self.settings.confidence.penalty("recommendation_failed", 0.2), source="orchestrator")
            return {"draft": AdvisoryDraft.minimal(str(exc)).model_dump(mode="json"), "penalties": [p.model_dump()],
                    "degraded_reasons": [f"recommendation step failed ({exc})"]}

        return self._execute(state, "recommend", self.recommender.name, run, failed)

    def _node_gate(self, state: WorkflowState) -> dict[str, Any]:
        with bind_context(run_id=state["run_id"], agent="orchestrator", step="hitl_gate"):
            report = AnalysisReport.model_validate(state["report"]) if state.get("report") else None
            confidence = self._confidence(state)
            reasons = review_triggers(report.anomalies if report else [], confidence, bool(state.get("degraded_reasons")), self.settings.hitl)
            if report is None:
                reasons.append("analysis incomplete")
            log_event(
                "decision",
                f"HITL {'required: ' + '; '.join(reasons) if reasons else 'not required'}",
                level=logging.WARNING if reasons else logging.INFO,
                hitl_required=bool(reasons),
                reasons=reasons,
                confidence=confidence,
            )
            update: dict[str, Any] = {"review_reasons": reasons}
            if not reasons:
                update["steps"] = {"human_review": StepRecord(name="human_review", status="skipped").model_dump()}
            return update

    def _node_review(self, state: WorkflowState) -> dict[str, Any]:
        def run(s: WorkflowState) -> dict[str, Any]:
            draft = AdvisoryDraft.model_validate(s["draft"])
            packet = self._review_packet(s, draft)
            log_event("hitl_requested", "awaiting reviewer decision", reasons=s.get("review_reasons", []))
            raw_decision = interrupt(packet)  # pauses here; resumes with the reviewer's decision
            decision = ReviewDecision.model_validate(raw_decision)
            new_draft, info = apply_decision(draft, decision, list(s.get("review_reasons", [])))
            log_event(
                "decision",
                f"review {info.status} by {info.reviewer} ({info.mode})",
                level=logging.WARNING if info.mode == "auto_approve" else logging.INFO,
                review_status=info.status,
                reviewer=info.reviewer,
                mode=info.mode,
                changes=info.changes,
                note=info.reviewer_note,
            )
            return {"draft": new_draft.model_dump(mode="json"), "review": info.model_dump(mode="json"),
                    "decisions": [{"type": "hitl", **info.model_dump(mode="json")}]}

        def failed(exc: Exception) -> dict[str, Any]:
            info = ReviewInfo(status="pending_review", required=True, trigger_reasons=list(state.get("review_reasons", [])),
                              mode="non_interactive", reviewer="system", reviewer_note=f"review failed: {exc}")
            return {"review": info.model_dump(mode="json")}

        return self._execute(state, "human_review", "hitl", run, failed)

    def _node_finalize(self, state: WorkflowState) -> dict[str, Any]:
        def run(s: WorkflowState) -> dict[str, Any]:
            ctx = ClientContext.model_validate(s["context"])
            report = AnalysisReport.model_validate(s["report"]) if s.get("report") else None
            draft = AdvisoryDraft.model_validate(s["draft"])
            review = ReviewInfo.model_validate(s["review"]) if s.get("review") else not_required_review()
            penalties = [ConfidencePenalty.model_validate(p) for p in s.get("penalties", [])]
            steps = self._step_list(s, finalize_status="success")
            degraded = list(dict.fromkeys(s.get("degraded_reasons", [])))
            output = AdvisoryOutput(
                run_id=s["run_id"],
                client_id=ctx.client_id,
                client_name=ctx.data.client.name,
                generated_at=_now(),
                as_of=ctx.data.as_of,
                summary=draft.summary,
                narrative_source=draft.narrative_source,
                metrics=report.metrics if report else None,
                anomalies=report.anomalies if report else [],
                recommendations=draft.recommendations,
                confidence=compute_confidence(penalties, self.settings.confidence.floor),
                confidence_breakdown=penalties,
                degraded=bool(degraded),
                degraded_reasons=degraded,
                crm_source=ctx.crm_source,
                risk_profile=ctx.risk_profile.model_dump(mode="json"),
                assumptions=ctx.assumptions,
                data_quality=ctx.data_quality,
                detectors=report.detectors if report else [],
                trend=report.trend if report else None,
                review=review,
                steps=steps,
            )
            # Round-trip through the schema: guarantees the emitted JSON is valid.
            output = AdvisoryOutput.model_validate_json(output.model_dump_json())
            self._persist(output)
            return {"output": output.model_dump(mode="json")}

        def failed(exc: Exception) -> dict[str, Any]:
            return {"fatal_error": ErrorInfo(code="finalize_failed", message=str(exc), step="finalize").model_dump()}

        return self._execute(state, "finalize", "orchestrator", run, failed)

    def _node_failed(self, state: WorkflowState) -> dict[str, Any]:
        steps = {name: StepRecord(name=name, status="skipped").model_dump() for name in STEPS if state["steps"][name]["status"] == "pending"}
        return {"steps": steps}

    # ------------------------------------------------------------------ helpers
    def _confidence(self, state: WorkflowState) -> float:
        penalties = [ConfidencePenalty.model_validate(p) for p in state.get("penalties", [])]
        return compute_confidence(penalties, self.settings.confidence.floor)

    def _review_packet(self, state: WorkflowState, draft: AdvisoryDraft) -> dict[str, Any]:
        report = state.get("report") or {}
        ctx = state.get("context") or {}
        return {
            "run_id": state["run_id"],
            "client_id": state["client_id"],
            "client_name": ((ctx.get("data") or {}).get("client") or {}).get("name"),
            "reasons": state.get("review_reasons", []),
            "confidence": self._confidence(state),
            "degraded_reasons": state.get("degraded_reasons", []),
            "summary": draft.summary,
            "anomalies": [
                {k: a.get(k) for k in ("anomaly_id", "severity", "title", "explanation", "recurring")}
                for a in report.get("anomalies", [])
            ],
            "recommendations": [r.model_dump() for r in draft.recommendations],
        }

    def _step_list(self, state: WorkflowState, finalize_status: str) -> list[StepRecord]:
        out = []
        for name in STEPS:
            rec = StepRecord.model_validate(state["steps"][name])
            if name == "finalize":
                rec = StepRecord(name="finalize", status=finalize_status, attempts=1, finished_at=_now())  # type: ignore[arg-type]
            elif rec.status == "pending":
                rec.status = "skipped"
            out.append(rec)
        return out

    def _persist(self, output: AdvisoryOutput) -> None:
        if not self.tools.has(MEMORY):
            return
        payload = output.model_dump(mode="json")
        res = self.tools.get(MEMORY).execute({"operation": "save_run", "client_id": output.client_id, "payload": payload})
        if output.review.required:
            self.tools.get(MEMORY).execute(
                {"operation": "record_decision", "client_id": output.client_id, "run_id": output.run_id, "payload": payload["review"]}
            )
        if not res.ok:
            log_event("fallback", "could not persist run to long-term memory", level=logging.WARNING, error=res.error.model_dump() if res.error else None)

    # ================================================================== public API
    def run(self, client_id: str, reviewer: Reviewer, source_path: str | None = None) -> RunResult:
        """Run the full pipeline for one client. Never raises: failures become ``RunResult(status='failed')``."""
        run_id = new_run_id()
        t0 = time.perf_counter()
        config = {"configurable": {"thread_id": run_id}}
        with bind_context(run_id=run_id, agent="orchestrator"):
            log_event("run_started", f"run started for {client_id}", client_id=client_id, source=source_path)
            initial: WorkflowState = {
                "run_id": run_id,
                "client_id": client_id,
                "source_path": source_path,
                "started_at": _now(),
                "steps": {name: StepRecord(name=name).model_dump() for name in STEPS},
                "penalties": [],
                "degraded_reasons": [],
                "warnings": [],
                "decisions": [],
                "fatal_error": None,
                "review_reasons": [],
            }
            try:
                self.graph.invoke(initial, config)
                snapshot = self.graph.get_state(config)
                while snapshot.interrupts:
                    packet = snapshot.interrupts[0].value
                    decision = reviewer.review(packet)
                    self.graph.invoke(Command(resume=decision.model_dump(mode="json")), config)
                    snapshot = self.graph.get_state(config)
                values = snapshot.values
            except Exception as exc:  # noqa: BLE001 - last line of defence: never crash a batch
                log_event("error", f"orchestrator error: {exc}", level=logging.ERROR, error_type=type(exc).__name__)
                values = {"fatal_error": ErrorInfo(code="orchestrator_error", message=str(exc)).model_dump(), "steps": initial["steps"]}

            duration = round((time.perf_counter() - t0) * 1000, 2)
            steps = [StepRecord.model_validate(values.get("steps", {}).get(n, {"name": n})) for n in STEPS]
            if values.get("output") and not values.get("fatal_error"):
                output = AdvisoryOutput.model_validate(values["output"])
                result = RunResult(run_id=run_id, client_id=client_id, status="completed", output=output, steps=output.steps, duration_ms=duration)
            else:
                err = ErrorInfo.model_validate(values.get("fatal_error") or {"code": "unknown", "message": "run produced no output"})
                result = RunResult(run_id=run_id, client_id=client_id, status="failed", error=err, steps=steps, duration_ms=duration)
            log_event(
                "run_finished",
                f"run {result.status} for {client_id}",
                level=logging.INFO if result.status == "completed" else logging.ERROR,
                duration_ms=duration,
                status=result.status,
                confidence=result.output.confidence if result.output else None,
                degraded=result.output.degraded if result.output else None,
                review_status=result.output.review.status if result.output else None,
                error=result.error.model_dump() if result.error else None,
            )
            return result
