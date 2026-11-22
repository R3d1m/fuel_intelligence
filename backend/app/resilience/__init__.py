"""Resilience patterns: circuit breaker, degraded modes, fallbacks."""

from app.resilience.breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "CircuitState",
]
