"""Circuit breaker for agent tool calls (ADR-015).

Protects downstream services (e.g. taxpayer DB, external APIs) from being
flooded by a looping or retrying agent.  Two mechanisms are provided:

1. PerRunCallCounter  — raises ToolCallLimitExceeded if a single tool is
   called more than max_calls times within one graph run (trace).

2. CircuitBreaker — process-level breaker that tracks consecutive failures per
   tool and transitions through Closed → Open → Half-Open states.

Both are integrated into ManifestExecutor._execute_tool_call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

logger = logging.getLogger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────


class ToolCallLimitExceeded(Exception):
    """Raised when a tool is called more than max_calls_per_run times in one run."""


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is Open and the call is blocked."""


# ── Per-run call counter ────────────────────────────────────────────


class PerRunCallCounter:
    """Counts tool invocations within a single graph run (trace_id scope).

    Instantiated once per trace and discarded after the run completes.
    Thread-safe via asyncio single-threaded assumptions; no lock required.
    """

    DEFAULT_MAX_CALLS: ClassVar[int] = 5

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        """limits maps tool_name → max_calls override (uses DEFAULT_MAX_CALLS otherwise)."""
        self._limits: dict[str, int] = limits or {}
        self._counts: dict[str, int] = {}

    def check_and_increment(self, tool_name: str) -> None:
        """Increment count for tool_name; raise ToolCallLimitExceeded if limit exceeded."""
        limit = self._limits.get(tool_name, self.DEFAULT_MAX_CALLS)
        current = self._counts.get(tool_name, 0)
        if current >= limit:
            raise ToolCallLimitExceeded(
                f"Tool '{tool_name}' called {current} times in this run "
                f"(limit: {limit}). Possible agent loop — call blocked."
            )
        self._counts[tool_name] = current + 1

    def reset(self) -> None:
        self._counts.clear()


# ── Circuit breaker ─────────────────────────────────────────────────


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Blocking calls; downstream is unhealthy
    HALF_OPEN = "half_open" # Probe: allow limited calls to test recovery


@dataclass
class CircuitBreaker:
    """Per-tool process-level circuit breaker.

    Parameters
    ----------
    failure_threshold : int
        Number of consecutive failures before opening the circuit (default 5).
    recovery_timeout : float
        Seconds the circuit stays Open before moving to Half-Open (default 30).
    half_open_max_calls : int
        Probe calls allowed in Half-Open before evaluating pass/fail (default 1).
    """

    tool_name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 1

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _opened_at: float = field(default=0.0, init=False, repr=False)
    _half_open_calls: int = field(default=0, init=False, repr=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                logger.info("CircuitBreaker[%s] → HALF_OPEN (recovery probe)", self.tool_name)
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
        return self._state

    def allow_call(self) -> bool:
        """Return True if the call is permitted; False if the circuit is Open."""
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.HALF_OPEN:
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False  # still probing
        return False  # OPEN

    def record_success(self) -> None:
        """Call after a successful tool invocation."""
        if self._state in (CircuitState.HALF_OPEN, CircuitState.CLOSED):
            if self._consecutive_failures > 0:
                logger.info(
                    "CircuitBreaker[%s] success — resetting failure count", self.tool_name
                )
            self._consecutive_failures = 0
            if self._state == CircuitState.HALF_OPEN:
                logger.info("CircuitBreaker[%s] → CLOSED (probe passed)", self.tool_name)
                self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Call after a failed tool invocation."""
        self._consecutive_failures += 1
        if self._state == CircuitState.HALF_OPEN:
            logger.warning(
                "CircuitBreaker[%s] probe FAILED → re-opening circuit", self.tool_name
            )
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
        elif self._consecutive_failures >= self.failure_threshold:
            logger.warning(
                "CircuitBreaker[%s] %d consecutive failures → OPEN",
                self.tool_name,
                self._consecutive_failures,
            )
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()


# ── Registry ────────────────────────────────────────────────────────


class CircuitBreakerRegistry:
    """Process-level store of CircuitBreaker instances, keyed by tool name.

    ManifestExecutor holds one registry per executor instance (not per run),
    so the breaker state persists across multiple graph runs (traces).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self._defaults = dict(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_max_calls=half_open_max_calls,
        )
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, tool_name: str) -> CircuitBreaker:
        if tool_name not in self._breakers:
            self._breakers[tool_name] = CircuitBreaker(tool_name=tool_name, **self._defaults)
        return self._breakers[tool_name]

    def reset(self, tool_name: str) -> None:
        """Reset a breaker to Closed (useful in tests or after maintenance)."""
        self._breakers.pop(tool_name, None)
