"""Minimal circuit breaker.

Kubernetes heals the *callee*; the breaker protects the *caller*. While a
backend is down (pods restarting, all endpoints unready) the gateway fails
fast with 503 instead of tying up connections on timeouts, then probes with
a single trial request once the cool-down elapses.
"""

from __future__ import annotations

import time
from enum import IntEnum

from autoheal_gateway.metrics import CIRCUIT_STATE


class State(IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class CircuitBreaker:
    def __init__(self, name: str, *, failure_threshold: int, reset_seconds: float, clock=time.monotonic) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        self._set(State.CLOSED)

    def _set(self, state: State) -> None:
        self._state = state
        CIRCUIT_STATE.labels(self.name).set(int(state))

    @property
    def state(self) -> State:
        if self._state is State.OPEN and self._clock() - self._opened_at >= self.reset_seconds:
            self._set(State.HALF_OPEN)
            self._trial_in_flight = False
        return self._state

    def allow(self) -> bool:
        state = self.state
        if state is State.CLOSED:
            return True
        if state is State.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def retry_after(self) -> float:
        return max(0.0, self.reset_seconds - (self._clock() - self._opened_at))

    def record_success(self) -> None:
        self._failures = 0
        self._trial_in_flight = False
        if self._state is not State.CLOSED:
            self._set(State.CLOSED)

    def record_failure(self) -> None:
        self._failures += 1
        self._trial_in_flight = False
        if self._state is State.HALF_OPEN or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()
            self._set(State.OPEN)
