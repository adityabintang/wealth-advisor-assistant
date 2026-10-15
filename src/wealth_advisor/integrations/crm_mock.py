"""In-process mock CRM API with deterministic failure injection.

Failure modes (per client in ``crm_mock.json -> failure_injection`` or forced globally):
  - ``timeout``   : the call exceeds the client timeout
  - ``error_500`` : HTTP 500 server error
  - ``empty``     : 200 OK with an empty body
  - ``flaky``     : fails the first attempt with 503, then succeeds (demonstrates retry)
Unknown client ids return 404.
"""

from __future__ import annotations

import copy
import json
import random
import time
from pathlib import Path
from typing import Any, Literal

FailureMode = Literal["timeout", "error_500", "empty", "flaky"]
FAILURE_MODES: tuple[str, ...] = ("timeout", "error_500", "empty", "flaky")


class CRMError(Exception):
    status_code: int = 500


class CRMTimeoutError(CRMError):
    status_code = 504


class CRMServerError(CRMError):
    def __init__(self, status_code: int = 500, message: str = "Internal Server Error") -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class CRMNotFoundError(CRMError):
    status_code = 404


class MockCRMClient:
    """Pretends to be ``GET /crm/clients/{client_id}``."""

    def __init__(
        self,
        mock_path: Path,
        *,
        seed: int = 42,
        latency_ms: tuple[int, int] = (10, 40),
        forced_failure: FailureMode | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        raw = json.loads(Path(mock_path).read_text(encoding="utf-8")) if Path(mock_path).exists() else {}
        self._clients: dict[str, dict[str, Any]] = raw.get("clients", {})
        self._failures: dict[str, str] = raw.get("failure_injection", {})
        self._forced = forced_failure
        self._rng = random.Random(seed)
        self._latency = latency_ms
        self._sleep = sleep
        self._attempts: dict[str, int] = {}

    def failure_mode_for(self, client_id: str) -> str | None:
        return self._forced or self._failures.get(client_id)

    def get_client(self, client_id: str, *, timeout: float) -> dict[str, Any]:
        self._attempts[client_id] = self._attempts.get(client_id, 0) + 1
        mode = self.failure_mode_for(client_id)
        latency = self._rng.uniform(*self._latency) / 1000

        if mode == "timeout":
            self._sleep(timeout)
            raise CRMTimeoutError(f"CRM did not respond within {timeout:.2f}s")
        self._sleep(min(latency, timeout))
        if mode == "error_500":
            raise CRMServerError(500)
        if mode == "flaky" and self._attempts[client_id] == 1:
            raise CRMServerError(503, "Service Unavailable")
        if mode == "empty":
            return {}
        if client_id not in self._clients:
            raise CRMNotFoundError(f"client '{client_id}' not found in CRM")
        return copy.deepcopy(self._clients[client_id])
