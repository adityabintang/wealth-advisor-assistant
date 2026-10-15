"""CRMTool: wraps the CRM API with timeout, retry + exponential backoff and a circuit breaker."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import CRMConfig
from ..errors import ToolFailure
from ..integrations.crm_mock import CRMError, CRMNotFoundError, CRMServerError, CRMTimeoutError, MockCRMClient
from ..logging_setup import log_event
from ..models import CRMContext
from .base import Tool
from .resilience import CircuitBreaker, CircuitOpenError, call_with_retry


class CRMInput(BaseModel):
    client_id: str


class CRMTool(Tool[CRMInput]):
    name = "crm_lookup"
    description = "Fetch CRM context (risk profile, goals, relationship, notes) for a client."
    input_schema = CRMInput

    def __init__(self, client: MockCRMClient, config: CRMConfig, breaker: CircuitBreaker | None = None) -> None:
        self._client = client
        self._cfg = config
        self._breaker = breaker or CircuitBreaker(
            "crm", config.circuit_breaker.failure_threshold, config.circuit_breaker.reset_seconds
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def _run(self, params: CRMInput, meta: dict[str, Any]) -> dict[str, Any]:
        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            meta["retries"] = attempt
            log_event(
                "retry",
                f"CRM attempt {attempt} failed ({type(exc).__name__}); retrying in {delay:.2f}s",
                level=logging.WARNING,
                tool=self.name,
                attempt=attempt,
                delay_s=delay,
            )

        try:
            raw, retries = call_with_retry(
                lambda: self._client.get_client(params.client_id, timeout=self._cfg.timeout_seconds),
                retry_on=(CRMTimeoutError, CRMServerError),
                max_retries=self._cfg.max_retries,
                backoff_base=self._cfg.backoff_base_seconds,
                breaker=self._breaker,
                on_retry=on_retry,
            )
            meta["retries"] = retries
        except CircuitOpenError as exc:
            raise ToolFailure(str(exc), code="circuit_open") from exc
        except CRMTimeoutError as exc:
            raise ToolFailure(f"CRM timeout after {meta['retries']} retries: {exc}", code="crm_timeout", retryable=True) from exc
        except CRMNotFoundError as exc:
            raise ToolFailure(str(exc), code="crm_not_found") from exc
        except CRMServerError as exc:
            raise ToolFailure(
                f"CRM server error after {meta['retries']} retries: {exc}", code="crm_server_error", retryable=True
            ) from exc
        except CRMError as exc:
            raise ToolFailure(str(exc), code="crm_error") from exc

        meta["breaker_state"] = self._breaker.state
        if not raw:
            return {"empty": True, "context": CRMContext(client_id=params.client_id).model_dump(mode="json"), "issues": []}

        issues: list[str] = []
        raw.setdefault("client_id", params.client_id)
        try:
            ctx = CRMContext.model_validate(raw)
        except ValidationError as exc:
            # Keep the valid parts; drop the invalid risk profile rather than failing the call.
            issues.append(f"invalid CRM fields dropped: {exc.error_count()} error(s)")
            raw.pop("risk_profile", None)
            ctx = CRMContext.model_validate(raw)
        return {"empty": ctx.is_empty, "context": ctx.model_dump(mode="json"), "issues": issues}
