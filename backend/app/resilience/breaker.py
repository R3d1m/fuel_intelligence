"""Circuit Breaker Pattern Implementation.

Protects upstream services and internal subsystems from cascading failures by
fast-failing requests when a failure threshold is exceeded and automatically
attempting recovery after a cooldown timeout.
"""

import logging
import time
from collections.abc import Callable, Coroutine
from enum import StrEnum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(StrEnum):
    """Operational states of a circuit breaker."""

    CLOSED = "CLOSED"  # Normal operations: calls pass through
    OPEN = "OPEN"  # Tripped: calls are fast-failed immediately
    HALF_OPEN = "HALF_OPEN"  # Testing recovery: trial probe calls permitted


class CircuitBreakerOpenError(Exception):
    """Raised when an operation is attempted while the circuit breaker is OPEN."""

    def __init__(self, message: str = "Circuit breaker is OPEN. Fast-failing request.") -> None:
        super().__init__(message)


class CircuitBreaker:
    """In-memory Circuit Breaker with configurable threshold and cooldown.

    State transitions:
    1. CLOSED -> OPEN: Triggered when consecutive failures reach `failure_threshold`.
    2. OPEN -> HALF_OPEN: Occurs after `recovery_timeout` seconds have elapsed.
    3. HALF_OPEN -> CLOSED: A successful call in HALF_OPEN resets the breaker.
    4. HALF_OPEN -> OPEN: Any failure in HALF_OPEN trips the breaker back to OPEN.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 10.0,
        name: str = "default",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_timeout <= 0.0:
            raise ValueError("recovery_timeout must be greater than 0")

        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float | None = None
        self._last_state_change: float = self._clock()

    @property
    def state(self) -> CircuitState:
        """Current operational state, taking elapsed cooldown time into account."""
        if self._state == CircuitState.OPEN:
            if self._last_failure_time is not None:
                elapsed = self._clock() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
        return self._state

    @property
    def is_closed(self) -> bool:
        """Check if circuit is in normal CLOSED state."""
        return self.state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is tripped in OPEN state."""
        return self.state == CircuitState.OPEN

    @property
    def is_half_open(self) -> bool:
        """Check if circuit is testing recovery in HALF_OPEN state."""
        return self.state == CircuitState.HALF_OPEN

    @property
    def failure_count(self) -> int:
        """Consecutive failure count towards the failure threshold."""
        return self._failure_count

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining before recovery probe is allowed. Returns 0.0 if not OPEN."""
        if self._state != CircuitState.OPEN or self._last_failure_time is None:
            return 0.0
        elapsed = self._clock() - self._last_failure_time
        return max(0.0, self.recovery_timeout - elapsed)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Internal state transition handler with audit logging."""
        if self._state != new_state:
            logger.warning(
                "Circuit breaker '%s' state transition: %s -> %s",
                self.name,
                self._state.value,
                new_state.value,
            )
            self._state = new_state
            self._last_state_change = self._clock()

    def can_execute(self) -> bool:
        """Check whether execution is currently permitted through the circuit breaker.

        Returns:
            True if state is CLOSED or HALF_OPEN (permitting a probe).
            False if state is OPEN and still cooling down.
        """
        current_state = self.state
        return current_state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        """Record a successful operation.

        Resets consecutive failure count and restores HALF_OPEN circuit to CLOSED.
        """
        current_state = self.state
        if current_state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker '%s' probe succeeded. Resetting to CLOSED.", self.name)
            self._failure_count = 0
            self._last_failure_time = None
            self._transition_to(CircuitState.CLOSED)
        elif current_state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed operation.

        Increments failure counter. Trips circuit to OPEN if failure_threshold is reached,
        or immediately re-trips if currently HALF_OPEN.
        """
        self._failure_count += 1
        self._last_failure_time = self._clock()

        current_state = self.state
        if current_state == CircuitState.HALF_OPEN:
            logger.warning(
                "Circuit breaker '%s' probe failed. Re-opening circuit for %.1fs.",
                self.name,
                self.recovery_timeout,
            )
            self._transition_to(CircuitState.OPEN)
        elif self._failure_count >= self.failure_threshold:
            logger.error(
                "Circuit breaker '%s' tripped OPEN (%d consecutive failures >= threshold %d). "
                "Cooling down for %.1fs.",
                self.name,
                self._failure_count,
                self.failure_threshold,
                self.recovery_timeout,
            )
            self._transition_to(CircuitState.OPEN)

    def reset(self) -> None:
        """Manually reset the breaker back to clean CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
        self._last_state_change = self._clock()
        logger.info("Circuit breaker '%s' manually reset to CLOSED.", self.name)

    async def call(
        self,
        func: Callable[..., Coroutine[Any, Any, T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute an asynchronous coroutine guarded by the circuit breaker.

        Raises:
            CircuitBreakerOpenError: If breaker is OPEN and cooldown has not elapsed.
            Exception: Any exception raised by `func`, after updating breaker state.
        """
        if not self.can_execute():
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' is OPEN. "
                f"Cooldown remaining: {self.cooldown_remaining:.2f}s."
            )

        try:
            result = await func(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise
