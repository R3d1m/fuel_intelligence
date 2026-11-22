"""API route handlers for platform health and state endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import (
    get_app_settings,
    get_circuit_breaker,
    get_poller,
    get_state_store,
)
from app.api.schemas import (
    HealthResponse,
    PollerDependencyHealth,
    SimulatorDependencyHealth,
    StateResponse,
    StoreDependencyHealth,
    SyncResponse,
)
from app.config import Settings
from app.domain.models import OperationalMode
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.resilience.breaker import CircuitBreaker, CircuitState
from app.resilience.mode import diagnose_operational_mode

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Platform Liveness & Dependency Health",
    description="Exposes platform operational status, dependency health, and operational mode.",
)
async def get_health(
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    poller: SimulatorPoller = Depends(get_poller),
) -> HealthResponse:
    """Evaluate and report system liveness, dependency states, and operational mode."""
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=False,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    is_connected = breaker.state == CircuitState.CLOSED and store.get_latest() is not None
    overall_status = "healthy" if diagnosis.mode == OperationalMode.NORMAL else "degraded"

    return HealthResponse(
        status=overall_status,
        mode=diagnosis.mode,
        reason=diagnosis.reason,
        simulator=SimulatorDependencyHealth(
            connected=is_connected,
            circuit_state=breaker.state.value,
            consecutive_failures=breaker.consecutive_failures,
            cooldown_remaining=round(breaker.cooldown_remaining, 2),
        ),
        store=StoreDependencyHealth(
            has_snapshot=store.get_latest() is not None,
            is_stale=diagnosis.is_stale,
            data_age_seconds=(
                round(diagnosis.data_age_seconds, 2)
                if diagnosis.data_age_seconds is not None
                else None
            ),
            total_updates=store.total_updates,
            failed_polls=store.failed_polls,
        ),
        poller=PollerDependencyHealth(
            is_running=poller.is_running,
            poll_interval_seconds=poller.poll_interval_seconds,
        ),
        timestamp=diagnosis.evaluated_at,
    )


@router.get(
    "/state",
    response_model=StateResponse,
    summary="Authoritative Platform State",
    description="Returns current or last-known-good network snapshot and mode telemetry.",
)
async def get_state(
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
) -> StateResponse:
    """Retrieve the authoritative simulation state.

    In NORMAL mode: Serves the fresh latest snapshot.
    In DEGRADED_CACHED mode: Serves the cached last-known-good snapshot to avoid blind spots.
    """
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=False,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": (
                    "No simulator snapshot available yet. Initial synchronization in progress."
                ),
                "mode": diagnosis.mode,
                "reason": diagnosis.reason,
                "circuit_state": breaker.state.value,
            },
        )

    return StateResponse(
        mode=diagnosis.mode,
        reason=diagnosis.reason,
        data_age_seconds=(
            round(diagnosis.data_age_seconds, 2)
            if diagnosis.data_age_seconds is not None
            else None
        ),
        is_stale=diagnosis.is_stale,
        circuit_state=breaker.state.value,
        snapshot=snapshot,
        evaluated_at=diagnosis.evaluated_at,
    )


@router.post(
    "/sync",
    response_model=SyncResponse,
    summary="Trigger Manual Simulator Poll",
    description="Forces an immediate synchronization cycle with the simulator.",
)
async def trigger_sync(
    poller: SimulatorPoller = Depends(get_poller),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
) -> SyncResponse:
    """Manually invoke a polling cycle on demand."""
    if not breaker.can_execute():
        cooldown = breaker.cooldown_remaining
        return SyncResponse(
            success=False,
            message=(
                f"Sync rejected: Circuit breaker is {breaker.state.value} "
                f"(cooldown: {cooldown:.1f}s)"
            ),
        )

    snapshot = await poller.poll_once()
    if snapshot is None:
        return SyncResponse(
            success=False,
            message="Polling cycle failed. Check simulator connectivity or circuit status.",
        )

    return SyncResponse(
        success=True,
        tick=snapshot.tick,
        is_stale=snapshot.is_stale,
        message=f"Successfully synchronized state at simulation tick {snapshot.tick}.",
        details={
            "depots_count": len(snapshot.depots),
            "stations_count": len(snapshot.stations),
            "routes_count": len(snapshot.routes),
            "events_count": len(snapshot.events),
        },
    )
