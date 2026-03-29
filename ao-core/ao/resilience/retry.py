"""Retry policies — exponential backoff, jitter, circuit breaker.

Provides decorators and utilities for resilient execution of steps and tool calls.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    """Configuration for retry behavior."""

    max_retries: int = 3
    base_delay: float = 1.0  # seconds
    max_delay: float = 60.0  # seconds
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,)

    def get_delay(self, attempt: int) -> float:
        delay = min(self.base_delay * (self.exponential_base ** attempt), self.max_delay)
        if self.jitter:
            delay = delay * (0.5 + random.random() * 0.5)  # noqa: S311
        return delay


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """Circuit breaker to prevent cascading failures.

    - CLOSED: normal operation, count failures
    - OPEN: all calls rejected immediately for `recovery_timeout` seconds
    - HALF_OPEN: allow one test call, if it succeeds → CLOSED, else → OPEN
    """

    failure_threshold: int = 5
    recovery_timeout: float = 30.0  # seconds before trying again
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning("Circuit breaker OPEN after %d failures", self._failure_count)

    def allow_request(self) -> bool:
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN:
            return True  # Allow one test request
        return False


def with_retry(policy: RetryPolicy | None = None) -> Callable:
    """Decorator that retries an async function according to the given policy.

    Usage:
        @with_retry(RetryPolicy(max_retries=3))
        async def call_external_api():
            ...
    """
    if policy is None:
        policy = RetryPolicy()

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            for attempt in range(policy.max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except policy.retryable_exceptions as e:
                    last_exception = e
                    if attempt < policy.max_retries:
                        delay = policy.get_delay(attempt)
                        logger.warning(
                            "Retry %d/%d for %s after %.1fs: %s",
                            attempt + 1,
                            policy.max_retries,
                            fn.__qualname__,
                            delay,
                            e,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            "All %d retries exhausted for %s: %s",
                            policy.max_retries,
                            fn.__qualname__,
                            e,
                        )
            raise last_exception  # type: ignore[misc]

        return wrapper

    return decorator
