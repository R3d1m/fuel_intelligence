"""Unit tests for the CircuitBreaker resilience pattern."""

import pytest
from app.resilience.breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)


class MockClock:
    """Deterministic simulated clock for testing time-dependent circuit transitions."""

    def __init__(self, start_time: float = 1000.0) -> None:
        self.current_time = start_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


def test_circuit_breaker_initialization():
    clock = MockClock()
    breaker = CircuitBreaker(
        failure_threshold=3,
        recovery_timeout=10.0,
        name="test-breaker",
        clock=clock,
    )

    assert breaker.name == "test-breaker"
    assert breaker.state == CircuitState.CLOSED
    assert breaker.is_closed is True
    assert breaker.is_open is False
    assert breaker.is_half_open is False
    assert breaker.failure_count == 0
    assert breaker.cooldown_remaining == 0.0
    assert breaker.can_execute() is True


def test_circuit_breaker_invalid_parameters():
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker(failure_threshold=0)

    with pytest.raises(ValueError, match="recovery_timeout"):
        CircuitBreaker(recovery_timeout=0.0)


def test_circuit_breaker_successes_keep_it_closed():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=3, clock=clock)

    breaker.record_success()
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_circuit_breaker_trips_to_open_after_threshold():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=15.0, clock=clock)

    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 1
    assert breaker.can_execute() is True

    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 2
    assert breaker.can_execute() is True

    # 3rd failure hits threshold -> trips OPEN
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.is_open is True
    assert breaker.can_execute() is False
    assert breaker.cooldown_remaining == 15.0


def test_circuit_breaker_open_blocks_execution():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is True

    clock.advance(5.0)
    assert breaker.is_open is True
    assert breaker.can_execute() is False
    assert breaker.cooldown_remaining == 5.0


def test_circuit_breaker_transitions_to_half_open_after_cooldown():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is True

    # Advance clock past cooldown
    clock.advance(10.0)
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.is_half_open is True
    assert breaker.can_execute() is True
    assert breaker.cooldown_remaining == 0.0


def test_half_open_successful_probe_resets_to_closed():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.state == CircuitState.HALF_OPEN

    # Probe call succeeds -> restores CLOSED state
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0
    assert breaker.is_closed is True


def test_half_open_failed_probe_re_trips_to_open():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.state == CircuitState.HALF_OPEN

    # Probe call fails -> immediately returns to OPEN
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.is_open is True
    assert breaker.can_execute() is False
    assert breaker.cooldown_remaining == 10.0


@pytest.mark.asyncio
async def test_circuit_breaker_call_wrapper():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0, clock=clock)

    async def successful_operation(val: int) -> int:
        return val * 2

    async def failing_operation():
        raise RuntimeError("Operation error")

    # Success call
    res = await breaker.call(successful_operation, 21)
    assert res == 42
    assert breaker.is_closed is True

    # First failure
    with pytest.raises(RuntimeError):
        await breaker.call(failing_operation)
    assert breaker.is_closed is True
    assert breaker.failure_count == 1

    # Second failure -> trips breaker
    with pytest.raises(RuntimeError):
        await breaker.call(failing_operation)
    assert breaker.is_open is True

    # Immediate call fast-fails with CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError, match="is OPEN"):
        await breaker.call(successful_operation, 5)


def test_circuit_breaker_manual_reset():
    clock = MockClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0, clock=clock)

    breaker.record_failure()
    assert breaker.is_open is True

    breaker.reset()
    assert breaker.is_closed is True
    assert breaker.failure_count == 0
    assert breaker.can_execute() is True
