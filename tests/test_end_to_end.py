"""End-to-end integration tests over the LangGraph orchestrator and the sample scenarios."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import base_client, write_client

from wealth_advisor.config import Settings
from wealth_advisor.errors import LLMUnavailableError
from wealth_advisor.hitl import AutoApproveReviewer, PendingReviewer, ScriptedReviewer
from wealth_advisor.llm.base import LLMProvider
from wealth_advisor.models import AdvisoryOutput, ReviewDecision
from wealth_advisor.tools.client_data import client_id_from_path

AUTO = AutoApproveReviewer()


def _sample(settings: Settings, cid: str) -> str:
    return str(next(settings.paths.clients_dir.glob(f"{cid}_*.json")))


def test_all_sample_scenarios_complete_without_crash(settings: Settings, make_app) -> None:
    app = make_app()
    files = sorted(settings.paths.clients_dir.glob("*.json"))
    assert len(files) >= 5
    for path in files:
        t0 = time.perf_counter()
        result = app.orchestrator.run(client_id_from_path(path), AUTO, str(path))
        assert time.perf_counter() - t0 < 10
        assert result.status in ("completed", "failed")
        if result.status == "completed":
            # Output is valid against the published schema.
            AdvisoryOutput.model_validate_json(result.output.model_dump_json())


def test_normal_client_happy_path(settings: Settings, make_app) -> None:
    result = make_app().orchestrator.run("C001", PendingReviewer(), _sample(settings, "C001"))
    o = result.output
    assert result.status == "completed"
    assert o.anomalies == [] and o.confidence == 1.0 and not o.degraded
    assert o.review.status == "not_required"
    assert [s.status for s in o.steps] == ["success", "success", "success", "skipped", "success"]


def test_injected_anomalies_recall_and_hitl(settings: Settings, make_app) -> None:
    path = _sample(settings, "C002")
    injected = json.loads(Path(path).read_text(encoding="utf-8"))["_injected_anomalies"]
    reviewer = ScriptedReviewer(ReviewDecision(action="approve", reviewer="tester"))
    o = make_app().orchestrator.run("C002", reviewer, path).output
    detected = {a.type for a in o.anomalies}
    recall = sum(1 for i in injected if i["type"] in detected) / len(injected)
    assert recall >= 0.9
    assert reviewer.packets and "high-severity" in reviewer.packets[0]["reasons"][0]
    assert o.review.status == "approved" and o.review.reviewer == "tester"
    assert all(r.linked_anomaly_ids for r in o.recommendations)


def test_incomplete_data_partial_analysis(settings: Settings, make_app) -> None:
    o = make_app().orchestrator.run("C003", AUTO, _sample(settings, "C003")).output
    assert o.confidence < settings.hitl.confidence_threshold
    assert o.risk_profile["assumed"] is True
    skipped = {d.detector for d in o.detectors if d.status == "skipped"}
    assert {"unusual_amount", "frequency_spike", "value_decline"} <= skipped
    assert any(d.action == "quarantined" for d in o.data_quality)
    assert o.review.required and o.review.mode == "auto_approve"


def test_crm_down_degraded_then_memory_fallback(settings: Settings, make_app) -> None:
    app = make_app()
    # First run with healthy CRM stores the CRM context in long-term memory.
    ok = app.orchestrator.run("C001", AUTO, _sample(settings, "C001")).output
    assert ok.crm_source == "live"

    down = make_app(crm_failure="timeout")  # shares the same SQLite file
    o = down.orchestrator.run("C001", PendingReviewer(), _sample(settings, "C001")).output
    assert o.degraded and o.crm_source == "memory_cache"
    assert any("cached CRM context" in r for r in o.degraded_reasons)
    assert o.review.status == "pending_review"  # never auto-approved silently


def test_crm_down_without_cache_uses_conservative_default(settings: Settings, make_app) -> None:
    o = make_app().orchestrator.run("C004", AUTO, _sample(settings, "C004")).output
    assert o.degraded and o.crm_source == "none"
    assert o.risk_profile["category"] == "conservative" and o.risk_profile["assumed"]
    assert "crm_timeout" in o.degraded_reasons[0]


def test_broken_json_is_controlled_failure_and_batch_continues(settings: Settings, make_app) -> None:
    app = make_app()
    bad = app.orchestrator.run("C005", AUTO, _sample(settings, "C005"))
    assert bad.status == "failed" and bad.error.code == "invalid_json" and bad.error.step == "fetch_data"
    assert [s.status for s in bad.steps] == ["failed", "skipped", "skipped", "skipped", "skipped"]
    good = app.orchestrator.run("C001", AUTO, _sample(settings, "C001"))
    assert good.status == "completed"


def test_recurring_anomalies_use_long_term_memory(settings: Settings, make_app) -> None:
    app = make_app()
    path = _sample(settings, "C002")
    first = app.orchestrator.run("C002", AUTO, path).output
    assert first.trend is None and not any(a.recurring for a in first.anomalies)
    reviewer = ScriptedReviewer(ReviewDecision(action="approve"))
    second = app.orchestrator.run("C002", reviewer, path).output
    assert second.trend and second.trend.previous_run_id == first.run_id
    assert all(a.recurring and a.previously_acknowledged for a in second.anomalies)
    # Acknowledged recurring alerts are demoted and no longer force a HITL review.
    assert second.review.status == "not_required" and reviewer.packets == []
    assert all(r.priority != "high" for r in second.recommendations if r.linked_anomaly_ids)


def test_interactive_edit_is_applied_and_audited(settings: Settings, make_app) -> None:
    decision = ReviewDecision(action="edit", reviewer="advisor-1", removed_rec_ids=["R2"], modified_actions={"R1": "Call client today."}, note="ok")
    app = make_app()
    o = app.orchestrator.run("C002", ScriptedReviewer(decision), _sample(settings, "C002")).output
    assert o.review.status == "edited"
    assert "R2" not in {r.rec_id for r in o.recommendations}
    assert o.recommendations[0].action == "Call client today."
    decisions = app.memory.get_client_history("C002")["decisions"]
    assert decisions and decisions[0]["reviewer"] == "advisor-1" and decisions[0]["status"] == "edited"


def test_reject_keeps_output_but_marks_rejected(settings: Settings, make_app) -> None:
    o = make_app().orchestrator.run("C002", ScriptedReviewer(ReviewDecision(action="reject", note="wrong data")), _sample(settings, "C002")).output
    assert o.review.status == "rejected" and o.review.reviewer_note == "wrong data"


class FakeLLM(LLMProvider):
    name = "fake:model"

    def generate(self, system: str, prompt: str) -> str:
        return "LLM narrative."


class BrokenLLM(LLMProvider):
    name = "broken:model"

    def generate(self, system: str, prompt: str) -> str:
        raise LLMUnavailableError("provider down")


@pytest.mark.parametrize("llm,source", [(FakeLLM(), "llm"), (BrokenLLM(), "template")])
def test_llm_is_optional_and_falls_back(settings: Settings, make_app, llm, source) -> None:
    o = make_app(llm_provider=llm).orchestrator.run("C001", AUTO, _sample(settings, "C001")).output
    assert o.narrative_source == source
    assert o.anomalies == []  # findings never depend on the LLM


def test_agent_exception_produces_partial_result(settings: Settings, make_app) -> None:
    app = make_app()

    def explode(ctx):
        raise RuntimeError("analyzer bug")

    app.orchestrator.analyzer.run = explode  # type: ignore[method-assign]
    result = app.orchestrator.run("C001", AUTO, _sample(settings, "C001"))
    o = result.output
    assert result.status == "completed" and o.metrics is None
    assert o.degraded and o.steps[1].status == "failed"
    assert o.review.required and "analysis incomplete" in o.review.trigger_reasons
    assert o.recommendations[0].priority == "high"


def test_every_log_line_carries_run_id(settings: Settings, make_app, log_file: Path, tmp_path: Path) -> None:
    path = write_client(tmp_path / "c", "X001_ok", base_client())
    result = make_app().orchestrator.run("X001", AUTO, str(path))
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    run_lines = [ln for ln in lines if ln["run_id"] == result.run_id]
    events = {ln["event"] for ln in run_lines}
    assert {"run_started", "step_started", "step_finished", "tool_called", "decision", "run_finished"} <= events
    assert all(ln["run_id"] for ln in lines if ln["event"] in ("step_started", "tool_called", "run_finished"))
    assert "Test Client" not in log_file.read_text(encoding="utf-8")  # PII masked
