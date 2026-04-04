"""Unit tests for ADR-015 circuit breaker and per-run call counter."""

import pytest

from ao.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpenError,
    CircuitState,
    PerRunCallCounter,
    ToolCallLimitExceeded,
)


class TestPerRunCallCounter:
    def test_allows_calls_within_limit(self):
        c = PerRunCallCounter()
        for _ in range(5):
            c.check_and_increment("my_tool")  # should not raise

    def test_blocks_on_limit_exceeded(self):
        c = PerRunCallCounter()
        for _ in range(5):
            c.check_and_increment("my_tool")
        with pytest.raises(ToolCallLimitExceeded):
            c.check_and_increment("my_tool")

    def test_custom_limit_per_tool(self):
        c = PerRunCallCounter(limits={"lookup_taxpayer": 2})
        c.check_and_increment("lookup_taxpayer")
        c.check_and_increment("lookup_taxpayer")
        with pytest.raises(ToolCallLimitExceeded):
            c.check_and_increment("lookup_taxpayer")

    def test_different_tools_have_independent_counts(self):
        c = PerRunCallCounter()
        for _ in range(5):
            c.check_and_increment("tool_a")
        # tool_b should still work
        c.check_and_increment("tool_b")

    def test_reset_clears_counts(self):
        c = PerRunCallCounter()
        for _ in range(5):
            c.check_and_increment("my_tool")
        c.reset()
        c.check_and_increment("my_tool")  # should not raise after reset


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(tool_name="test")
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_call() is True

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(tool_name="test", failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_call() is False

    def test_partial_failures_stay_closed(self):
        cb = CircuitBreaker(tool_name="test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(tool_name="test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        # Only 2 failures since last success — should still be closed
        assert cb.state == CircuitState.CLOSED

    def test_transitions_to_half_open_after_recovery_timeout(self, monkeypatch):
        import time
        cb = CircuitBreaker(tool_name="test", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb._state == CircuitState.OPEN

        # Simulate passage of time beyond recovery_timeout
        monkeypatch.setattr(time, "monotonic", lambda: cb._opened_at + 1.0)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_call() is True  # probe call allowed

    def test_half_open_success_closes_breaker(self, monkeypatch):
        import time
        cb = CircuitBreaker(tool_name="test", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        monkeypatch.setattr(time, "monotonic", lambda: cb._opened_at + 1.0)
        _ = cb.state  # trigger HALF_OPEN transition
        cb.record_success()
        assert cb._state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self, monkeypatch):
        import time
        cb = CircuitBreaker(tool_name="test", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        monkeypatch.setattr(time, "monotonic", lambda: cb._opened_at + 1.0)
        _ = cb.state  # trigger HALF_OPEN transition
        cb.record_failure()
        assert cb._state == CircuitState.OPEN


class TestCircuitBreakerRegistry:
    def test_creates_breaker_on_demand(self):
        reg = CircuitBreakerRegistry()
        b = reg.get("my_tool")
        assert b.tool_name == "my_tool"
        assert b.state == CircuitState.CLOSED

    def test_returns_same_instance(self):
        reg = CircuitBreakerRegistry()
        b1 = reg.get("my_tool")
        b2 = reg.get("my_tool")
        assert b1 is b2

    def test_reset_removes_breaker(self):
        reg = CircuitBreakerRegistry()
        b1 = reg.get("my_tool")
        b1.record_failure()
        b1.record_failure()
        b1.record_failure()
        b1.record_failure()
        b1.record_failure()
        assert reg.get("my_tool").state == CircuitState.OPEN
        reg.reset("my_tool")
        assert reg.get("my_tool").state == CircuitState.CLOSED
