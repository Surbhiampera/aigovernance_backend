"""In-process circuit breaker for the Azure OpenAI upstream.

Per-process, not distributed: each Uvicorn worker tracks its own state. That's
fine here — the goal is to stop a single worker from hammering an already
failing Azure deployment with new requests, not to coordinate cluster-wide.
"""
from __future__ import annotations

import asyncio
import time


class CircuitBreaker:
    """Three states: closed (normal), open (failing fast), half-open (probing).

    Opens after `failure_threshold` consecutive failures. Stays open for
    `reset_seconds`, then allows one probe request through (half-open); a
    successful probe closes the circuit, a failed probe re-opens it.
    """

    def __init__(self, failure_threshold: int, reset_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_probe_in_flight = False
        self._lock = asyncio.Lock()

    async def allow_request(self) -> bool:
        async with self._lock:
            if self._opened_at is None:
                return True
            if time.time() - self._opened_at < self._reset_seconds:
                return False
            if self._half_open_probe_in_flight:
                return False
            self._half_open_probe_in_flight = True
            return True

    async def record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            self._opened_at = None
            self._half_open_probe_in_flight = False

    async def record_failure(self) -> None:
        async with self._lock:
            self._half_open_probe_in_flight = False
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._opened_at = time.time()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None
