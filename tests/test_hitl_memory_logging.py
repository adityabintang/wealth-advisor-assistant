"""HITL decision logic, long-term memory, confidence and PII masking."""

from __future__ import annotations

from wealth_advisor.confidence import compute_confidence
from wealth_advisor.config import Settings
from wealth_advisor.hitl import apply_decision, review_triggers
from wealth_advisor.logging_setup import mask_pii
from wealth_advisor.memory import LongTermMemory
from wealth_advisor.models import AdvisoryDraft, Anomaly, ConfidencePenalty, Recommendation, ReviewDecision


def _draft() -> AdvisoryDraft:
    return AdvisoryDraft(
        summary="s",
        recommendations=[
            Recommendation(rec_id="R1", action="a1", rationale="r", priority="high"),
            Recommendation(rec_id="R2", action="a2", rationale="r", priority="low"),
        ],
    )


def _anomaly(severity: str = "high", **kw) -> Anomaly:
    return Anomaly(anomaly_id="A1", type="large_outflow", severity=severity, title="t", evidence={"x": 1}, explanation="e", fingerprint="fp", **kw)


def test_review_triggers(settings: Settings) -> None:
    cfg = settings.hitl
    assert review_triggers([], 0.95, False, cfg) == []
    assert "high-severity" in review_triggers([_anomaly()], 0.95, False, cfg)[0]
    assert "confidence" in review_triggers([], 0.5, False, cfg)[0]
    assert "degraded" in review_triggers([], 0.95, True, cfg)[0]
    # A recurring high anomaly already acknowledged by the advisor does not re-trigger.
    assert review_triggers([_anomaly(recurring=True, previously_acknowledged=True)], 0.95, False, cfg) == []


def test_apply_edit_records_changes() -> None:
    decision = ReviewDecision(action="edit", reviewer="alice", removed_rec_ids=["R2"], modified_actions={"R1": "new action"})
    draft, info = apply_decision(_draft(), decision, ["reason"])
    assert info.status == "edited" and info.reviewer == "alice"
    assert [r.rec_id for r in draft.recommendations] == ["R1"]
    assert draft.recommendations[0].action == "new action" and draft.recommendations[0].edited_by_reviewer
    assert len(info.changes) == 2


def test_apply_reject_and_pending() -> None:
    _, rej = apply_decision(_draft(), ReviewDecision(action="reject", note="no"), [])
    assert rej.status == "rejected" and rej.decided_at
    _, pend = apply_decision(_draft(), ReviewDecision(action="pending", mode="non_interactive"), [])
    assert pend.status == "pending_review" and pend.decided_at is None


def test_confidence_floor_and_cap() -> None:
    assert compute_confidence([]) == 1.0
    assert compute_confidence([ConfidencePenalty(reason="x", amount=0.3, source="t")]) == 0.7
    assert compute_confidence([ConfidencePenalty(reason="x", amount=5, source="t")], floor=0.05) == 0.05


def test_long_term_memory_roundtrip(tmp_path) -> None:
    mem = LongTermMemory(tmp_path / "m.sqlite")
    mem.save_crm("C1", {"client_id": "C1"})
    assert mem.get_crm("C1")["context"] == {"client_id": "C1"}
    out = {
        "run_id": "r1", "client_id": "C1", "generated_at": "2026-10-10T00:00:00", "metrics": {"total_assets": 10.0},
        "confidence": 0.9, "degraded": False, "summary": "s", "review": {"status": "approved"},
        "anomalies": [{"fingerprint": "fp1", "type": "large_outflow"}],
    }
    mem.save_run(out)
    mem.save_run(out | {"run_id": "r2", "generated_at": "2026-10-11T00:00:00"})
    hist = mem.get_client_history("C1")
    assert [r["run_id"] for r in hist["runs"]] == ["r2", "r1"]
    assert hist["anomalies"]["fp1"]["occurrences"] == 2 and hist["anomalies"]["fp1"]["acknowledged"] == 1
    mem.reset()
    assert mem.get_client_history("C1")["runs"] == []
    mem.close()


def test_pii_masking() -> None:
    masked = mask_pii({"name": "Budi Santoso", "nested": [{"account_id": "ACC-C001-0001"}], "amount": 5})
    assert masked["name"] == "B*** S***"
    assert masked["nested"][0]["account_id"] == "****0001"
    assert masked["amount"] == 5
