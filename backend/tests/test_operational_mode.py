"""Unit tests for operational mode resolver and diagnostics."""

from datetime import UTC, datetime, timedelta

from app.domain.models import InstanceInfo, OperationalMode, SimulatorSnapshot
from app.ingest.store import StateStore
from app.resilience.breaker import CircuitBreaker, CircuitState
from app.resilience.mode import compute_operational_mode, diagnose_operational_mode


def _make_dummy_snapshot(is_stale: bool = False, age_seconds: float = 0.0) -> SimulatorSnapshot:
    fetched = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="bup-standard",
            scenario_version="1.0",
            seed=12345,
            sim_time="2026-11-22T10:00:00Z",
            tick=12,
            tick_minutes=15,
            status="RUNNING",
        ),
        depots=[],
        stations=[],
        routes=[],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=12,
        sim_time="2026-11-22T10:00:00Z",
        fetched_at=fetched,
        is_stale=is_stale,
    )


def test_mode_normal_when_healthy() -> None:
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=5.0)
    store = StateStore()
    snapshot = _make_dummy_snapshot(is_stale=False, age_seconds=1.0)
    store.update(snapshot)

    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=False,
        stale_threshold_seconds=10.0,
    )

    assert diagnosis.mode == OperationalMode.NORMAL
    assert not diagnosis.is_stale
    assert diagnosis.circuit_state == CircuitState.CLOSED.value
    assert "nominal" in diagnosis.reason.lower() or "healthy" in diagnosis.reason.lower()
    assert compute_operational_mode(breaker=breaker, store=store) == OperationalMode.NORMAL


def test_mode_degraded_cached_when_breaker_open() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    store = StateStore()
    store.update(_make_dummy_snapshot(is_stale=False))

    diagnosis = diagnose_operational_mode(breaker=breaker, store=store)
    assert diagnosis.mode == OperationalMode.DEGRADED_CACHED
    assert diagnosis.circuit_state == CircuitState.OPEN.value
    assert "circuit breaker is OPEN" in diagnosis.reason


def test_mode_degraded_cached_when_no_data() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    store = StateStore()

    diagnosis = diagnose_operational_mode(breaker=breaker, store=store)
    assert diagnosis.mode == OperationalMode.DEGRADED_CACHED
    assert diagnosis.is_stale
    assert "No simulator snapshot" in diagnosis.reason


def test_mode_degraded_cached_when_snapshot_flagged_stale() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    store = StateStore()
    store.update(_make_dummy_snapshot(is_stale=True, age_seconds=1.0))

    diagnosis = diagnose_operational_mode(breaker=breaker, store=store)
    assert diagnosis.mode == OperationalMode.DEGRADED_CACHED
    assert diagnosis.is_stale
    assert "X-Simulator-Stale" in diagnosis.reason


def test_mode_degraded_cached_when_snapshot_exceeds_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    store = StateStore()
    # 15 seconds old with threshold 10s
    store.update(_make_dummy_snapshot(is_stale=False, age_seconds=15.0))

    diagnosis = diagnose_operational_mode(
        breaker=breaker, store=store, stale_threshold_seconds=10.0
    )
    assert diagnosis.mode == OperationalMode.DEGRADED_CACHED
    assert diagnosis.is_stale
    assert "exceeds stale threshold" in diagnosis.reason


def test_mode_degraded_fallback_when_forecaster_fails() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    store = StateStore()
    store.update(_make_dummy_snapshot(is_stale=False))

    diagnosis = diagnose_operational_mode(breaker=breaker, store=store, forecaster_failed=True)
    assert diagnosis.mode == OperationalMode.DEGRADED_FALLBACK
    assert diagnosis.forecaster_failed
    assert "forecaster or twin failure" in diagnosis.reason
