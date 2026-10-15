"""Retry with exponential backoff + a minimal circuit breaker (PRD §11)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, TypeVar

from ..logging_setup import log_event

T = TypeVar("T")


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (cool-down) -> half_open -> closed/open."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_seconds: float = 30, clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_unlocked()

    def _state_unlocked(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.reset_seconds:
            return "half_open"
        return "open"

    def before_call(self) -> None:
        with self._lock:
            if self._state_unlocked() == "open":
                raise CircuitOpenError(f"circuit '{self.name}' is open")

    def record_success(self) -> None:
        with self._lock:
            if self._opened_at is not None:
                log_event("decision", f"circuit '{self.name}' closed after successful probe", circuit=self.name)
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            half_open = self._state_unlocked() == "half_open"
            if half_open or self._failures >= self.failure_threshold:
                self._opened_at = self._clock()
                log_event(
                    "decision",
                    f"circuit '{self.name}' opened after {self._failures} consecutive failures",
                    level=logging.WARNING,
                    circuit=self.name,
                    failures=self._failures,
                )


def call_with_retry(
    fn: Callable[[], T],
    *,
    retry_on: tuple[type[BaseException], ...],
    max_retries: int,
    backoff_base: float,
    breaker: CircuitBreaker | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[T, int]:
    """Run ``fn`` with up to ``max_retries`` retries. Returns ``(result, retries_used)``.

    Raises the last exception if all attempts fail, or ``CircuitOpenError`` if the breaker is open.
    """
    attempt = 0
    while True:
        if breaker:
            breaker.before_call()
        try:
            result = fn()
        except retry_on as exc:
            if breaker:
                breaker.record_failure()
            if attempt >= max_retries:
                raise
            delay = backoff_base * (2**attempt)
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            sleep(delay)
            attempt += 1
            continue
        if breaker:
            breaker.record_success()
        return result, attempt
