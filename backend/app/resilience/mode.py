"""Operational Mode Resolver and Resilience Diagnostics.

Computes the platform's explicit operational mode:
- NORMAL: All upstream dependencies healthy, state is fresh, and models active.
- DEGRADED_CACHED: Simulator API is down, circuit breaker is tripped, data is stale,
  or incoming snapshot was malformed. Serves last-known-good state.
- DEGRADED_FALLBACK: Forecaster or digital twin failed. Falls back to deterministic
  safety heuristics with mandatory human review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.domain.models import OperationalMode
from app.observability.logging import log_event
from app.observability.metrics import set_operational_mode
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
    twin_failed: bool = False
    malformed_rejected: bool = False
    active_alerts: list[str] = field(default_factory=list)


class ResilienceManager:
    """Resilience manager tracking model failures, corrupt snapshots, and circuit health."""

    def __init__(self) -> None:
        self._forecaster_failed: bool = False
        self._forecaster_error: str | None = None
        self._twin_failed: bool = False
        self._twin_error: str | None = None
        self._model_failure_time: datetime | None = None
        self._consecutive_model_failures: int = 0
        self._malformed_rejected: bool = False
        self._malformed_reason: str | None = None
        self._last_mode: OperationalMode | None = None

    @property
    def is_model_failed(self) -> bool:
        """Check whether any intelligence model (forecaster or twin) is degraded."""
        return self._forecaster_failed or self._twin_failed

    @property
    def forecaster_failed(self) -> bool:
        return self._forecaster_failed

    @property
    def twin_failed(self) -> bool:
        return self._twin_failed

    @property
    def malformed_rejected(self) -> bool:
        return self._malformed_rejected

    def record_forecaster_failure(self, error: str) -> None:
        """Record an unhandled exception or timeout from the forecaster."""
        self._forecaster_failed = True
        self._forecaster_error = error
        self._model_failure_time = datetime.now(UTC)
        self._consecutive_model_failures += 1
        logger.error(
            "Recorded forecaster failure (%d consecutive): %s",
            self._consecutive_model_failures,
            error,
        )

    def record_twin_failure(self, error: str) -> None:
        """Record an unhandled exception or timeout from the digital twin evaluator."""
        self._twin_failed = True
        self._twin_error = error
        self._model_failure_time = datetime.now(UTC)
        self._consecutive_model_failures += 1
        logger.error(
            "Recorded digital twin failure (%d consecutive): %s",
            self._consecutive_model_failures,
            error,
        )

    def record_model_success(self) -> None:
        """Clear model failures upon successful execution."""
        self._forecaster_failed = False
        self._forecaster_error = None
        self._twin_failed = False
        self._twin_error = None
        self._consecutive_model_failures = 0

    def record_malformed_snapshot(self, reason: str) -> None:
        """Record that an incoming simulator snapshot was rejected."""
        self._malformed_rejected = True
        self._malformed_reason = reason

    def clear_malformed_snapshot(self) -> None:
        """Clear malformed snapshot rejection state."""
        self._malformed_rejected = False
        self._malformed_reason = None

    def reset(self) -> None:
        """Reset all tracked resilience failure states."""
        self.record_model_success()
        self.clear_malformed_snapshot()
        self._last_mode = None

    def record_evaluated_mode(self, mode: OperationalMode, reason: str) -> None:
        """Record the latest evaluated operational mode and log transitions."""
        if self._last_mode is not None and mode != self._last_mode:
            log_event(
                logger,
                event="mode.changed",
                message=f"Operational mode changed: {self._last_mode.value} -> {mode.value}",
                level=logging.WARNING if mode != OperationalMode.NORMAL else logging.INFO,
                from_mode=self._last_mode.value,
                to_mode=mode.value,
                reason=reason,
            )
        self._last_mode = mode

    def get_mode(
        self,
        breaker: CircuitBreaker | None = None,
        store: StateStore | None = None,
        stale_threshold_seconds: float = 10.0,
    ) -> OperationalMode:
        """Compute the operational mode through this manager."""
        return compute_operational_mode(
            breaker=breaker,
            store=store,
            manager=self,
            stale_threshold_seconds=stale_threshold_seconds,
        )

    def diagnose(
        self,
        breaker: CircuitBreaker | None = None,
        store: StateStore | None = None,
        stale_threshold_seconds: float = 10.0,
    ) -> ModeDiagnosis:
        """Diagnose operational mode and justification through this manager."""
        return diagnose_operational_mode(
            breaker=breaker,
            store=store,
            manager=self,
            stale_threshold_seconds=stale_threshold_seconds,
        )


def compute_operational_mode(
    breaker: CircuitBreaker | None = None,
    store: StateStore | None = None,
    forecaster_failed: bool = False,
    twin_failed: bool = False,
    malformed_rejected: bool = False,
    manager: ResilienceManager | None = None,
    stale_threshold_seconds: float = 10.0,
) -> OperationalMode:
    """Compute the single authoritative operational mode for the platform.

    Evaluation precedence:
    1. If forecaster/twin model failed -> DEGRADED_FALLBACK
    2. If circuit breaker is not CLOSED (OPEN or HALF_OPEN) -> DEGRADED_CACHED
    3. If state store has no data, is stale, or malformed data rejected -> DEGRADED_CACHED
    4. Otherwise -> NORMAL
    """
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=forecaster_failed,
        twin_failed=twin_failed,
        malformed_rejected=malformed_rejected,
        manager=manager,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    return diagnosis.mode


def diagnose_operational_mode(
    breaker: CircuitBreaker | None = None,
    store: StateStore | None = None,
    forecaster_failed: bool = False,
    twin_failed: bool = False,
    malformed_rejected: bool = False,
    manager: ResilienceManager | None = None,
    stale_threshold_seconds: float = 10.0,
) -> ModeDiagnosis:
    """Perform a comprehensive diagnostic evaluation of the system's operational mode."""
    diagnosis = _evaluate_mode_diagnosis(
        breaker=breaker,
        store=store,
        forecaster_failed=forecaster_failed,
        twin_failed=twin_failed,
        malformed_rejected=malformed_rejected,
        manager=manager,
        stale_threshold_seconds=stale_threshold_seconds,
    )
    set_operational_mode(diagnosis.mode.value)
    if manager is not None:
        manager.record_evaluated_mode(diagnosis.mode, diagnosis.reason)
    return diagnosis


def _evaluate_mode_diagnosis(
    breaker: CircuitBreaker | None = None,
    store: StateStore | None = None,
    forecaster_failed: bool = False,
    twin_failed: bool = False,
    malformed_rejected: bool = False,
    manager: ResilienceManager | None = None,
    stale_threshold_seconds: float = 10.0,
) -> ModeDiagnosis:
    """Internal mode diagnosis evaluator."""
    now = datetime.now(UTC)
    circuit_state_str = breaker.state.value if breaker else CircuitState.CLOSED.value
    data_age = store.snapshot_age_seconds() if store else None
    is_stale = store.is_stale(stale_threshold_seconds=stale_threshold_seconds) if store else True

    # Check manager overrides if provided
    is_fc_failed = forecaster_failed or (manager is not None and manager.forecaster_failed)
    is_tw_failed = twin_failed or (manager is not None and manager.twin_failed)
    is_malformed = (
        malformed_rejected
        or (manager is not None and manager.malformed_rejected)
        or (
            store is not None
            and (
                getattr(store, "has_malformed_alert", False)
                or getattr(store, "latest_snapshot_rejected", False)
            )
        )
    )

    alerts: list[str] = []
    if store and getattr(store, "malformed_alert", None):
        alert_obj = store.malformed_alert
        if alert_obj:
            alerts.append(f"{alert_obj.headline}: {'; '.join(alert_obj.errors)}")

    # 1. Check model failure (highest priority for fallback heuristic trigger)
    if is_fc_failed or is_tw_failed:
        reasons: list[str] = []
        if is_fc_failed:
            reasons.append("Demand forecaster failure/timeout")
        if is_tw_failed:
            reasons.append("Digital twin simulation failure/timeout")

        reason_str = (
            f"Intelligence models degraded ({', '.join(reasons)}); "
            "safe rule-based heuristics active with human review mandated."
        )
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_FALLBACK,
            reason=reason_str,
            circuit_state=circuit_state_str,
            is_stale=is_stale,
            data_age_seconds=data_age,
            forecaster_failed=is_fc_failed,
            twin_failed=is_tw_failed,
            malformed_rejected=is_malformed,
            active_alerts=alerts,
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
            twin_failed=False,
            malformed_rejected=is_malformed,
            active_alerts=alerts,
            evaluated_at=now,
        )

    # 3. Check State Store availability & malformed rejections
    if store is None or store.get_latest() is None:
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_CACHED,
            reason="No simulator snapshot ingested yet; awaiting initial synchronization.",
            circuit_state=circuit_state_str,
            is_stale=True,
            data_age_seconds=None,
            forecaster_failed=False,
            twin_failed=False,
            malformed_rejected=is_malformed,
            active_alerts=alerts,
            evaluated_at=now,
        )

    if is_malformed:
        err_hint = (
            store.malformed_alert.errors[0]
            if store and store.malformed_alert and store.malformed_alert.errors
            else "domain validation invariants violated"
        )
        return ModeDiagnosis(
            mode=OperationalMode.DEGRADED_CACHED,
            reason=(
                f"Incoming simulator snapshot was rejected due to malformed data ({err_hint}); "
                "serving last-known-good cached state."
            ),
            circuit_state=circuit_state_str,
            is_stale=is_stale,
            data_age_seconds=data_age,
            forecaster_failed=False,
            twin_failed=False,
            malformed_rejected=True,
            active_alerts=alerts,
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
            twin_failed=False,
            malformed_rejected=False,
            active_alerts=alerts,
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
            twin_failed=False,
            malformed_rejected=False,
            active_alerts=alerts,
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
        twin_failed=False,
        malformed_rejected=False,
        active_alerts=alerts,
        evaluated_at=now,
    )
