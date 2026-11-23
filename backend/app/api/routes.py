"""API route handlers for platform health and state endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.adapters.simulator_client import SimulatorClient
from app.api.deps import (
    get_app_settings,
    get_circuit_breaker,
    get_forecaster,
    get_poller,
    get_simulator_client,
    get_state_store,
)
from app.api.schemas import (
    ForecastResponse,
    HealthResponse,
    PollerDependencyHealth,
    SimulatorDependencyHealth,
    StateResponse,
    StationForecastResponse,
    StoreDependencyHealth,
    SyncResponse,
)
from app.config import Settings
from app.domain.models import DemandHistoryRecord, OperationalMode
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.intelligence.forecast import DemandForecaster
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
            round(diagnosis.data_age_seconds, 2) if diagnosis.data_age_seconds is not None else None
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


@router.get(
    "/forecasts",
    response_model=ForecastResponse,
    summary="Fleet Demand Forecasts & Anomaly Telemetry",
    description=(
        "Returns fleet-wide demand projections, out-of-sample "
        "prediction error metrics, and anomaly spikes."
    ),
)
async def get_fleet_forecasts(
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    client: SimulatorClient = Depends(get_simulator_client),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    forecaster: DemandForecaster = Depends(get_forecaster),
) -> ForecastResponse:
    """Compute and retrieve fleet-wide demand forecasts and prediction error metrics."""
    # Obtain snapshot (latest or cached fallback)
    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No simulation snapshot available yet to generate forecasts.",
        )

    # Attempt to fetch demand history if simulator is accessible
    history_records: list[DemandHistoryRecord] = []
    forecaster_failed = False

    if breaker.can_execute():
        try:
            resp = await client.get_demand_history(limit=settings.forecast_history_limit)
            history_records = resp.data
        except Exception as exc:
            logger.warning("Failed to fetch demand history from simulator: %s", exc)

    try:
        fleet_fc = forecaster.forecast_all(
            snapshot=snapshot,
            demand_history=history_records,
            horizon_ticks=settings.forecast_horizon_ticks,
        )
    except Exception as exc:
        logger.error("Forecaster encountered unexpected error: %s", exc)
        forecaster_failed = True
        # Create minimal fallback structure
        fleet_fc = forecaster.forecast_all(
            snapshot=snapshot,
            demand_history=[],
            horizon_ticks=settings.forecast_horizon_ticks,
        )

    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=forecaster_failed,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    # Serialize forecasts dict for response
    serialized_forecasts = {
        st_id: {fuel: fc.model_dump(mode="json") for fuel, fc in fuels.items()}
        for st_id, fuels in fleet_fc.forecasts.items()
    }

    serialized_anomalies = [a.model_dump(mode="json") for a in fleet_fc.active_anomalies]

    return ForecastResponse(
        mode=diagnosis.mode,
        reason=diagnosis.reason,
        total_fleet_demand_per_hour=fleet_fc.total_fleet_demand_per_hour,
        average_mae=fleet_fc.average_mae,
        average_mape=fleet_fc.average_mape,
        active_anomalies_count=len(serialized_anomalies),
        active_anomalies=serialized_anomalies,
        forecasts=serialized_forecasts,
        evaluated_at=diagnosis.evaluated_at,
    )


@router.get(
    "/forecasts/{station_id}",
    response_model=StationForecastResponse,
    summary="Station-Specific Demand Forecast",
    description="Returns demand forecast, metrics, and anomaly status for a specific station.",
)
async def get_station_forecast(
    station_id: str,
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    client: SimulatorClient = Depends(get_simulator_client),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    forecaster: DemandForecaster = Depends(get_forecaster),
) -> StationForecastResponse:
    """Retrieve demand forecast for a single station."""
    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No simulation snapshot available yet to generate forecasts.",
        )

    target_station = next((s for s in snapshot.stations if s.id == station_id), None)
    if target_station is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station '{station_id}' not found in current simulation topology.",
        )

    history_records: list[DemandHistoryRecord] = []
    if breaker.can_execute():
        try:
            resp = await client.get_demand_history(
                station_id=station_id,
                limit=settings.forecast_history_limit,
            )
            history_records = resp.data
        except Exception as exc:
            logger.warning("Failed to fetch demand history for %s: %s", station_id, exc)

    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=False,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    station_fcs = {}
    anomalies = []
    for fuel in target_station.capacity.as_dict().keys():
        fc = forecaster.forecast_station_fuel(
            station=target_station,
            fuel=fuel,
            history=history_records,
            tick_minutes=snapshot.instance.tick_minutes,
            horizon_ticks=settings.forecast_horizon_ticks,
            current_tick=snapshot.instance.tick,
        )
        station_fcs[fuel.value] = fc.model_dump(mode="json")
        if fc.anomaly.is_spike:
            anomalies.append(fc.anomaly.model_dump(mode="json"))

    return StationForecastResponse(
        mode=diagnosis.mode,
        station_id=station_id,
        forecasts=station_fcs,
        anomalies=anomalies,
        evaluated_at=diagnosis.evaluated_at,
    )
