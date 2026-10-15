"""Structured logging: JSON lines to file + compact console summary, correlated by run_id.

Context (run_id / agent / step) is carried in contextvars so every log line emitted
inside a run is automatically correlated. PII (names, account numbers) is masked.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOGGER_NAME = "wealth_advisor"

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent", default=None)
_step: contextvars.ContextVar[str | None] = contextvars.ContextVar("step", default=None)

# Keys whose values are PII and must be masked in logs.
_PII_NAME_KEYS = {"name", "client_name", "full_name", "reviewer_name"}
_PII_ACCOUNT_KEYS = {"account_id", "account_number", "iban", "card_number"}


def mask_name(value: str) -> str:
    """'Budi Santoso' -> 'B*** S***'."""
    return " ".join(f"{part[0]}***" if part else part for part in str(value).split(" "))


def mask_account(value: str) -> str:
    """'ACC-001-12345' -> '****2345'."""
    digits = re.sub(r"\W", "", str(value))
    return "****" + digits[-4:] if len(digits) > 4 else "****"


def mask_pii(obj: Any) -> Any:
    """Recursively mask PII-bearing keys in dicts/lists."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            lk = str(key).lower()
            if lk in _PII_NAME_KEYS and isinstance(value, str):
                out[key] = mask_name(value)
            elif lk in _PII_ACCOUNT_KEYS and isinstance(value, str):
                out[key] = mask_account(value)
            else:
                out[key] = mask_pii(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [mask_pii(v) for v in obj]
    return obj


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


@contextlib.contextmanager
def bind_context(*, run_id: str | None = None, agent: str | None = None, step: str | None = None) -> Iterator[None]:
    """Bind run_id/agent/step for all log lines emitted within the block."""
    tokens = []
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if agent is not None:
        tokens.append((_agent, _agent.set(agent)))
    if step is not None:
        tokens.append((_step, _step.set(step)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def log_event(
    event: str,
    message: str = "",
    *,
    level: int = logging.INFO,
    duration_ms: float | None = None,
    **detail: Any,
) -> None:
    """Emit one structured event (see PRD §12 for the event vocabulary)."""
    get_logger().log(
        level,
        message or event,
        extra={"event": event, "duration_ms": duration_ms, "detail": detail},
    )


class JsonFormatter(logging.Formatter):
    """One JSON object per line with the PRD §12 field set."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "run_id": _run_id.get(),
            "agent": _agent.get(),
            "step": _step.get(),
            "event": getattr(record, "event", "log"),
            "duration_ms": _round(getattr(record, "duration_ms", None)),
            "message": record.getMessage(),
            "detail": mask_pii(getattr(record, "detail", {}) or {}),
        }
        if record.exc_info:
            payload["detail"]["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Short human-readable line for the console."""

    def format(self, record: logging.LogRecord) -> str:
        run = (_run_id.get() or "-")[-6:]
        where = _agent.get() or _step.get() or "system"
        event = getattr(record, "event", "log")
        dur = getattr(record, "duration_ms", None)
        dur_s = f" ({dur:.0f}ms)" if isinstance(dur, (int, float)) else ""
        return f"{record.levelname:<7} [{run}] {where:<14} {event:<20} {record.getMessage()}{dur_s}"


def _round(value: Any) -> Any:
    return round(value, 2) if isinstance(value, float) else value


def configure_logging(log_file: Path | None, *, console_level: int = logging.WARNING, file_level: int = logging.DEBUG) -> None:
    """(Re)configure handlers. Idempotent, safe to call per CLI invocation / test."""
    logger = get_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(file_level)
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)
