"""Restart policy for the optional personal-WeChat bridge.

The bridge must never make the main application unavailable.  This module is
deliberately independent from FastAPI and subprocess so the retry/circuit
behaviour can be tested without starting Node.js.
"""

from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class RestartDecision:
    action: str
    failures: int
    delay_seconds: int = 0
    exit_code: int | None = None
    new_event: bool = False


@dataclass
class _BridgeState:
    pid: int = 0
    started_at: float = 0.0
    failures: int = 0
    exit_seen: bool = False
    next_retry_at: float = 0.0
    circuit_open: bool = False
    exit_code: int | None = None


class WechatRestartSupervisor:
    """Apply exponential backoff and open a circuit after repeated crashes."""

    def __init__(
        self,
        *,
        base_delay: int = 30,
        max_delay: int = 600,
        max_failures: int = 5,
        stable_seconds: int = 300,
    ):
        self.base_delay = max(1, int(base_delay))
        self.max_delay = max(self.base_delay, int(max_delay))
        self.max_failures = max(1, int(max_failures))
        self.stable_seconds = max(1, int(stable_seconds))
        self._states: dict[str, _BridgeState] = {}

    def record_started(self, key: str, pid: int, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else float(now)
        state = self._states.setdefault(key, _BridgeState())
        state.pid = int(pid)
        state.started_at = current
        state.exit_seen = False
        state.next_retry_at = 0.0
        state.exit_code = None

    def observe_exit(
        self,
        key: str,
        pid: int,
        exit_code: int | None,
        *,
        now: float | None = None,
    ) -> RestartDecision:
        current = time.monotonic() if now is None else float(now)
        state = self._states.setdefault(
            key, _BridgeState(pid=int(pid), started_at=current),
        )

        # A process that stayed healthy for a meaningful period starts a new
        # failure series instead of inheriting an old transient incident.
        if not state.exit_seen:
            if state.started_at and current - state.started_at >= self.stable_seconds:
                state.failures = 0
            state.failures += 1
            state.exit_seen = True
            state.exit_code = exit_code
            if state.failures >= self.max_failures:
                state.circuit_open = True
                return RestartDecision(
                    "disabled", state.failures, exit_code=exit_code, new_event=True,
                )
            delay = min(
                self.max_delay,
                self.base_delay * (2 ** max(0, state.failures - 1)),
            )
            state.next_retry_at = current + delay
            return RestartDecision(
                "wait", state.failures, delay_seconds=delay,
                exit_code=exit_code, new_event=True,
            )

        if state.circuit_open:
            return RestartDecision(
                "disabled", state.failures, exit_code=state.exit_code,
            )
        if current >= state.next_retry_at:
            return RestartDecision(
                "restart", state.failures, exit_code=state.exit_code,
            )
        return RestartDecision(
            "wait",
            state.failures,
            delay_seconds=max(1, int(state.next_retry_at - current)),
            exit_code=state.exit_code,
        )

    def record_launch_failure(
        self, key: str, *, now: float | None = None,
    ) -> RestartDecision:
        current = time.monotonic() if now is None else float(now)
        state = self._states.setdefault(key, _BridgeState())
        state.failures += 1
        if state.failures >= self.max_failures:
            state.circuit_open = True
            return RestartDecision(
                "disabled", state.failures, exit_code=state.exit_code, new_event=True,
            )
        delay = min(
            self.max_delay,
            self.base_delay * (2 ** max(0, state.failures - 1)),
        )
        state.next_retry_at = current + delay
        return RestartDecision(
            "wait", state.failures, delay_seconds=delay,
            exit_code=state.exit_code, new_event=True,
        )

    def snapshot(self, key: str) -> dict:
        state = self._states.get(key, _BridgeState())
        return {
            "pid": state.pid,
            "failures": state.failures,
            "circuit_open": state.circuit_open,
            "next_retry_at": state.next_retry_at,
            "exit_code": state.exit_code,
        }

