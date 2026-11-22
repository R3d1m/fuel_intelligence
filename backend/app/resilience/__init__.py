"""Resilience patterns: circuit breaker, degraded modes, fallbacks."""

from app.resilience.breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)
from app.resilience.mode import (
    ModeDiagnosis,
    compute_operational_mode,
    diagnose_operational_mode,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "CircuitState",
    "ModeDiagnosis",
    "compute_operational_mode",
    "diagnose_operational_mode",
]
