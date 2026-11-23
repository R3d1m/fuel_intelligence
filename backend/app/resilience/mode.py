"""Operational Mode Resolver and Resilience Diagnostics.

Computes the platform's explicit operational mode:
- NORMAL: All upstream dependencies healthy, state is fresh, and models active.
- DEGRADED_CACHED: Simulator API is down, circuit breaker is tripped, or data is stale.
  Serves last-known-good state and blocks automated dispatches.
- DEGRADED_FALLBACK: Forecaster or digital twin failed. Falls back to deterministic
  safety heuristics with mandatory human review.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.domain.models import OperationalMode
from app.resilience.breaker import CircuitBreaker, CircuitState

if TYPE_CHECKING:
    from app.ingest.store import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModeDiagnosis:
    """Detailed diagnosis explaining the rationale behind the current operational mode."""

    mode: OperationalMode
    reason: str
    circuit_state: str
    is_stale: bool
    data_age_seconds: float | None
    forecaster_failed: bool
    evaluated_at: datetime


def compute_operational_mode(
    breaker: CircuitBreaker | None = None,
    store: "StateStore | None" = None,
    forecaster_failed: bool = False,
    stale_threshold_seconds: float = 10.0,
) -> OperationalMode:
    """Compute the single authoritative operational mode for the platform.

    Evaluation precedence:
    1. If forecaster/model failed -> DEGRADED_FALLBACK
    2. If circuit breaker is not CLOSED (OPEN or HALF_OPEN) -> DEGRADED_CACHED
    3. If state store has no data or is stale -> DEGRADED_CACHED
    4. Otherwise -> NORMAL
    """
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=forecaster_failed,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    return diagnosis.mode


def diagnose_operational_mode(
    breaker: CircuitBreaker | None = None,
    store: "StateStore | None" = None,
    forecaster_failed: bool = False,
    stale_threshold_seconds: float = 10.0,
) -> ModeDiagnosis:
    """Perform a comprehensive diagnostic evaluation of the system's operational mode."""
    now = datetime.now(UTC)
    circuit_state_str = breaker.state.value if breaker else CircuitState.CLOSED.value
    data_age = store.snapshot_age_seconds() if store else None
    is_stale = store.is_stale(stale_threshold_seconds=stale_threshold_seconds) if store else True

    # 1. Check forecaster failure (highest priority for fallback heuristic trigger)
    if forecaster_failed:
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_FALLBACK,
            reason=(
                "Intelligence forecaster or twin failure detected; running rule-based heuristics."
            ),
            circuit_state=circuit_state_str,
            is_stale=is_stale,
            data_age_seconds=data_age,
            forecaster_failed=True,
            evaluated_at=now,
        )

    # 2. Check Circuit Breaker state
    if breaker and breaker.state != CircuitState.CLOSED:
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_CACHED,
            reason=(
                f"Simulator circuit breaker is {breaker.state.value} "
                f"({breaker.consecutive_failures} failures); serving cached state."
            ),
            circuit_state=circuit_state_str,
            is_stale=is_stale,
            data_age_seconds=data_age,
            forecaster_failed=False,
            evaluated_at=now,
        )

    # 3. Check State Store availability & staleness
    if store is None or store.get_latest() is None:
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_CACHED,
            reason="No simulator snapshot ingested yet; awaiting initial synchronization.",
            circuit_state=circuit_state_str,
            is_stale=True,
            data_age_seconds=None,
            forecaster_failed=False,
            evaluated_at=now,
        )

    latest_snapshot = store.get_latest()
    if latest_snapshot and latest_snapshot.is_stale:
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_CACHED,
            reason="Simulator upstream flagged payload as stale (X-Simulator-Stale: true).",
            circuit_state=circuit_state_str,
            is_stale=True,
            data_age_seconds=data_age,
            forecaster_failed=False,
            evaluated_at=now,
        )

    if is_stale:
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_CACHED,
            reason=(
                f"Snapshot age ({data_age:.1f}s) exceeds stale threshold "
                f"({stale_threshold_seconds:.1f}s)."
            ),
            circuit_state=circuit_state_str,
            is_stale=True,
            data_age_seconds=data_age,
            forecaster_failed=False,
            evaluated_at=now,
        )

    # 4. All systems nominal
    return ModeDiagnosis(
        mode=OperationalMode.NORMAL,
        reason="Simulator connection healthy, snapshot fresh, all models operational.",
        circuit_state=circuit_state_str,
        is_stale=False,
        data_age_seconds=data_age,
        forecaster_failed=False,
        evaluated_at=now,
    )
