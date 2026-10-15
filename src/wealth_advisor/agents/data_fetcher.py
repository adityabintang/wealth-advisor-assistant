"""Data Fetcher Agent: load + validate client data, enrich with CRM context (FR-3, FR-4, §11)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..errors import FatalStepError
from ..logging_setup import log_event
from ..models import ClientContext, ClientFinancialData, CRMContext, DataQualityIssue, QuarantinedRecord, RiskProfile
from ..tools import CLIENT_DATA, CRM, MEMORY
from .base import Agent


class DataFetcherAgent(Agent):
    name = "data_fetcher"

    def run(self, client_id: str, source_path: str | None = None) -> ClientContext:
        # 1) Client financial data — fatal for this client if it cannot be read.
        res = self.use(CLIENT_DATA, client_id=client_id, path=source_path)
        if not res.ok:
            assert res.error is not None
            raise FatalStepError(res.error.message, code=res.error.code)
        data = ClientFinancialData.model_validate(res.data["data"])
        dq = [DataQualityIssue.model_validate(i) for i in res.data["data_quality"]]
        quarantined = [QuarantinedRecord.model_validate(q) for q in res.data["quarantined"]]

        penalties = []
        n_def = sum(1 for i in dq if i.action == "defaulted")
        if quarantined:
            penalties.append(
                self.penalty("quarantined_record", f"{len(quarantined)} record(s) quarantined", multiplier=len(quarantined), cap_key="quarantined_record_cap")
            )
            log_event("data_quality", f"{len(quarantined)} record(s) quarantined", level=logging.WARNING,
                      records=[{"section": q.section, "id": q.record_id, "reason": q.reason} for q in quarantined])
        if n_def:
            penalties.append(self.penalty("defaulted_field", f"{n_def} missing field(s) defaulted", multiplier=n_def, cap_key="defaulted_field_cap"))

        # 2) CRM context — degraded mode on failure, with long-term memory fallback.
        crm, source, fetched_at, degraded, assumptions = self._crm(data.client.client_id, dq, penalties)

        # 3) Effective risk profile (CRM, or conservative default flagged as an assumption).
        risk = self._risk_profile(crm, assumptions, penalties)

        return ClientContext(
            client_id=data.client.client_id,
            data=data,
            crm=crm,
            crm_source=source,  # type: ignore[arg-type]
            crm_fetched_at=fetched_at,
            risk_profile=risk,
            data_quality=dq,
            quarantined=quarantined,
            degraded_reasons=degraded,
            assumptions=assumptions,
            penalties=[p for p in penalties if p is not None],
        )

    # ------------------------------------------------------------------ CRM
    def _crm(self, client_id: str, dq: list[DataQualityIssue], penalties: list) -> tuple:
        degraded: list[str] = []
        assumptions: list[str] = []
        res = self.use(CRM, client_id=client_id)
        if res.ok:
            ctx = CRMContext.model_validate(res.data["context"])
            for issue in res.data.get("issues", []):
                dq.append(DataQualityIssue(section="crm", issue=issue, action="noted"))
            if res.data.get("empty"):
                assumptions.append("CRM returned an empty record for this client.")
                log_event("decision", "CRM returned empty data; continuing with defaults", level=logging.WARNING)
            else:
                self.use(MEMORY, operation="save_crm", client_id=client_id, payload=ctx.model_dump(mode="json"))
            return ctx, "live", datetime.now(timezone.utc).isoformat(timespec="seconds"), degraded, assumptions

        assert res.error is not None
        if res.error.code == "crm_not_found":
            reason = "CRM has no record for this client"
        else:
            reason = f"CRM unavailable ({res.error.code}, {res.meta.retries} retries)"
        degraded.append(reason)
        log_event("decision", f"entering degraded mode: {reason}", level=logging.WARNING, error=res.error.model_dump())

        cached = self.use(MEMORY, operation="get_crm", client_id=client_id)
        if cached.ok and cached.data:
            ctx = CRMContext.model_validate(cached.data["context"])
            fetched_at = cached.data["fetched_at"]
            degraded.append(f"using cached CRM context from long-term memory (fetched {fetched_at})")
            penalties.append(self.penalty("crm_from_cache", "CRM context served from cache"))
            log_event("fallback", "CRM fallback: cached context from long-term memory", level=logging.WARNING, fetched_at=fetched_at)
            return ctx, "memory_cache", fetched_at, degraded, assumptions

        penalties.append(self.penalty("crm_unavailable", "no CRM context (live or cached)"))
        log_event("fallback", "CRM fallback: no cached context; continuing without CRM", level=logging.WARNING)
        return None, "none", None, degraded, assumptions

    def _risk_profile(self, crm: CRMContext | None, assumptions: list[str], penalties: list) -> RiskProfile:
        if crm and crm.risk_profile:
            rp = crm.risk_profile
            if rp.max_equity_pct is None:
                rp = rp.model_copy(update={"max_equity_pct": self.settings.max_equity_for(rp.category)})
            return rp
        default = self.settings.default_risk_profile
        assumptions.append(
            f"Risk profile not available from CRM; assumed '{default}' (default) until confirmed with the client."
        )
        penalties.append(self.penalty("risk_profile_assumed", f"risk profile assumed '{default}'"))
        log_event("decision", f"risk profile missing; defaulting to '{default}'", level=logging.WARNING)
        return RiskProfile(category=default, max_equity_pct=self.settings.max_equity_for(default), assumed=True)  # type: ignore[arg-type]
