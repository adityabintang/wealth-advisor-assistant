"""Human-in-the-loop checkpoint (PRD §14).

The orchestrator pauses the LangGraph run with ``interrupt(packet)``; a ``Reviewer`` turns the
packet into a ``ReviewDecision`` and the run resumes with ``Command(resume=decision)``.
Non-interactive runs never auto-approve silently: they end as ``pending_review`` unless
``--auto-approve`` was given explicitly (which is recorded in the output and the log).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from .config import HITLConfig
from .models import AdvisoryDraft, Anomaly, ReviewDecision, ReviewInfo


def review_triggers(anomalies: list[Anomaly], confidence: float, degraded: bool, cfg: HITLConfig) -> list[str]:
    """Return the reasons a human review is required (empty list = not required)."""
    reasons: list[str] = []
    if cfg.trigger_on_high_severity:
        high = [a for a in anomalies if a.severity == "high" and not (a.recurring and a.previously_acknowledged)]
        if high:
            reasons.append(f"{len(high)} high-severity anomal{'y' if len(high) == 1 else 'ies'} ({', '.join(a.anomaly_id for a in high)})")
    if confidence < cfg.confidence_threshold:
        reasons.append(f"confidence {confidence:.2f} below threshold {cfg.confidence_threshold:.2f}")
    if cfg.trigger_on_degraded and degraded:
        reasons.append("output produced in degraded mode")
    return reasons


class Reviewer(Protocol):
    def review(self, packet: dict[str, Any]) -> ReviewDecision: ...


class AutoApproveReviewer:
    """Explicit opt-in for demos/CI (``--auto-approve``). Always labelled as such."""

    def review(self, packet: dict[str, Any]) -> ReviewDecision:
        return ReviewDecision(
            action="approve",
            reviewer="auto-approve",
            mode="auto_approve",
            note="Auto-approved via --auto-approve (non-interactive demo/CI). Not a human decision.",
        )


class PendingReviewer:
    """Non-interactive default: leave the output for manual review."""

    def review(self, packet: dict[str, Any]) -> ReviewDecision:
        return ReviewDecision(
            action="pending",
            reviewer="system",
            mode="non_interactive",
            note="No reviewer available (non-interactive run); awaiting manual review.",
        )


class ScriptedReviewer:
    """Returns a fixed decision (tests / scripted demos)."""

    def __init__(self, decision: ReviewDecision) -> None:
        self.decision = decision
        self.packets: list[dict[str, Any]] = []

    def review(self, packet: dict[str, Any]) -> ReviewDecision:
        self.packets.append(packet)
        return self.decision


def apply_decision(draft: AdvisoryDraft, decision: ReviewDecision, reasons: list[str]) -> tuple[AdvisoryDraft, ReviewInfo]:
    """Apply approve/edit/reject/pending to the draft and build an auditable ``ReviewInfo``."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes: list[str] = []
    recs = [r.model_copy() for r in draft.recommendations]

    if decision.action == "edit":
        existing = {r.rec_id for r in recs}
        for rid in decision.removed_rec_ids:
            if rid in existing:
                changes.append(f"removed {rid}")
        recs = [r for r in recs if r.rec_id not in set(decision.removed_rec_ids)]
        for r in recs:
            new_action = decision.modified_actions.get(r.rec_id)
            if new_action and new_action != r.action:
                changes.append(f"modified {r.rec_id}: '{r.action}' -> '{new_action}'")
                r.action = new_action
                r.edited_by_reviewer = True

    status = {
        "approve": "approved",
        "edit": "edited" if changes else "approved",
        "reject": "rejected",
        "pending": "pending_review",
    }[decision.action]
    info = ReviewInfo(
        status=status,  # type: ignore[arg-type]
        required=True,
        trigger_reasons=reasons,
        mode=decision.mode,
        reviewer=decision.reviewer,
        reviewer_note=decision.note,
        decided_at=now if decision.action != "pending" else None,
        changes=changes,
    )
    return draft.model_copy(update={"recommendations": recs}), info


def not_required_review() -> ReviewInfo:
    return ReviewInfo(
        status="not_required",
        required=False,
        mode="not_required",
        reviewer_note="HITL not triggered: no unacknowledged high-severity anomaly, confidence above threshold, not degraded.",
    )
