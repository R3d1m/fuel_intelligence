"""API route handlers for platform health and state endpoints."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.adapters.simulator_client import SimulatorClient
from app.api.deps import (
    get_app_settings,
    get_circuit_breaker,
    get_decision_service,
    get_digital_twin,
    get_forecaster,
    get_planner,
    get_poller,
    get_resilience_manager,
    get_risk_engine,
    get_simulator_client,
    get_state_store,
)
from app.api.schemas import (
    AuditHistoryResponse,
    AuditStatsResponse,
    ForecastResponse,
    HealthResponse,
    PlanResponse,
    PollerDependencyHealth,
    RecommendationApproveRequest,
    RecommendationListResponse,
    RecommendationRejectRequest,
    RecommendationResponse,
    RiskResponse,
    SimulatorDependencyHealth,
    StateResponse,
    StationForecastResponse,
    StationRiskResponse,
    StoreDependencyHealth,
    SyncResponse,
    TwinSimulateRequest,
    TwinSimulationResponse,
)
from app.config import Settings
from app.decisions.service import DecisionService
from app.domain.models import DemandHistoryRecord, FuelType, OperationalMode, RiskSeverity
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.intelligence.forecast import DemandForecaster
from app.intelligence.planner import ConstrainedPlanner
from app.intelligence.risk import RiskAlert, RiskEngine, calculate_urgency_score
from app.intelligence.twin import DigitalTwin
from app.observability.metrics import (
    record_alert,
    render_prometheus_metrics,
    set_forecast_accuracy,
)
from app.resilience.breaker import CircuitBreaker, CircuitState
from app.resilience.mode import ResilienceManager, diagnose_operational_mode

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
    manager: ResilienceManager = Depends(get_resilience_manager),
) -> HealthResponse:
    """Evaluate and report system liveness, dependency states, and operational mode."""
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        manager=manager,
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
        alerts=diagnosis.active_alerts,
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
    manager: ResilienceManager = Depends(get_resilience_manager),
) -> StateResponse:
    """Retrieve the authoritative simulation state.

    In NORMAL mode: Serves the fresh latest snapshot.
    In DEGRADED_CACHED mode: Serves the cached last-known-good snapshot to avoid blind spots.
    """
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        manager=manager,
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
        alerts=diagnosis.active_alerts,
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
    manager: ResilienceManager = Depends(get_resilience_manager),
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
        manager.record_forecaster_failure(str(exc))
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
        manager=manager,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    # Serialize forecasts dict for response
    serialized_forecasts = {
        st_id: {fuel: fc.model_dump(mode="json") for fuel, fc in fuels.items()}
        for st_id, fuels in fleet_fc.forecasts.items()
    }

    serialized_anomalies = [a.model_dump(mode="json") for a in fleet_fc.active_anomalies]
    set_forecast_accuracy(fleet_fc.average_mae, fleet_fc.average_mape)

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


@router.get(
    "/risks",
    response_model=RiskResponse,
    summary="Fleet-Wide Fuel Risk Assessment",
    description=(
        "Returns inventory projections, stockout times, urgency alerts, "
        "and risk drivers across all stations."
    ),
)
async def get_risks(
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    client: SimulatorClient = Depends(get_simulator_client),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    forecaster: DemandForecaster = Depends(get_forecaster),
    risk_engine: RiskEngine = Depends(get_risk_engine),
    manager: ResilienceManager = Depends(get_resilience_manager),
) -> RiskResponse:
    """Evaluate and return fleet-wide fuel risk projections and operational alerts."""
    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No simulation snapshot available yet to evaluate risk projections.",
        )

    history_records: list[DemandHistoryRecord] = []
    if breaker.can_execute():
        try:
            resp = await client.get_demand_history(limit=settings.forecast_history_limit)
            history_records = resp.data
        except Exception as exc:
            logger.warning("Failed to fetch demand history from simulator: %s", exc)

    data_age = store.snapshot_age_seconds() or 0.0
    is_stale = store.is_stale(stale_threshold_seconds=settings.stale_threshold_seconds)
    engine_failed = False

    try:
        fleet_fc = forecaster.forecast_all(
            snapshot=snapshot,
            demand_history=history_records,
            horizon_ticks=settings.risk_horizon_ticks,
        )
        fleet_risk = risk_engine.assess_fleet_risk(
            snapshot=snapshot,
            fleet_forecast=fleet_fc,
            demand_history=history_records,
            data_age_seconds=data_age,
            is_stale=is_stale,
            horizon_ticks=settings.risk_horizon_ticks,
        )
    except Exception as exc:
        logger.error("Risk engine encountered unexpected error: %s", exc)
        engine_failed = True
        manager.record_forecaster_failure(str(exc))
        # Minimal fallback
        fleet_risk = risk_engine.assess_fleet_risk(
            snapshot=snapshot,
            fleet_forecast=None,
            demand_history=[],
            data_age_seconds=data_age,
            is_stale=is_stale,
            horizon_ticks=settings.risk_horizon_ticks,
        )

    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=engine_failed,
        manager=manager,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    serialized_station_risks = {
        st_id: {fuel: r.model_dump(mode="json") for fuel, r in fuels.items()}
        for st_id, fuels in fleet_risk.station_risks.items()
    }
    serialized_depot_risks = {
        dp_id: {fuel: r.model_dump(mode="json") for fuel, r in fuels.items()}
        for dp_id, fuels in fleet_risk.depot_risks.items()
    }
    serialized_alerts = [a.model_dump(mode="json") for a in fleet_risk.active_alerts]
    for alert in fleet_risk.active_alerts:
        record_alert(alert.severity.value)

    return RiskResponse(
        mode=diagnosis.mode,
        reason=diagnosis.reason,
        overall_severity=fleet_risk.overall_severity.value,
        critical_count=fleet_risk.critical_count,
        watch_count=fleet_risk.watch_count,
        ok_count=fleet_risk.ok_count,
        total_projected_unmet_liters=fleet_risk.total_projected_unmet_liters,
        earliest_stockout_station_id=fleet_risk.earliest_stockout_station_id,
        earliest_stockout_fuel=(
            fleet_risk.earliest_stockout_fuel.value if fleet_risk.earliest_stockout_fuel else None
        ),
        earliest_stockout_hours=fleet_risk.earliest_stockout_hours,
        fleet_average_confidence=fleet_risk.fleet_average_confidence,
        active_alerts_count=len(serialized_alerts),
        active_alerts=serialized_alerts,
        station_risks=serialized_station_risks,
        depot_risks=serialized_depot_risks,
        evaluated_at=diagnosis.evaluated_at,
    )


@router.get(
    "/risks/{station_id}",
    response_model=StationRiskResponse,
    summary="Station-Specific Risk Assessment",
    description=(
        "Returns forward-projected inventory trajectories and "
        "stockout risks for a specific station."
    ),
)
async def get_station_risk(
    station_id: str,
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    client: SimulatorClient = Depends(get_simulator_client),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    forecaster: DemandForecaster = Depends(get_forecaster),
    risk_engine: RiskEngine = Depends(get_risk_engine),
) -> StationRiskResponse:
    """Retrieve detailed risk projections and trajectory points for a single station."""
    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No simulation snapshot available yet to evaluate risk projections.",
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

    data_age = store.snapshot_age_seconds() or 0.0
    is_stale = store.is_stale(stale_threshold_seconds=settings.stale_threshold_seconds)

    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=False,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    station_risks_dict = {}
    station_alerts = []
    highest_sev = RiskSeverity.OK

    for fuel in target_station.capacity.as_dict().keys():
        fc = forecaster.forecast_station_fuel(
            station=target_station,
            fuel=fuel,
            history=history_records,
            tick_minutes=snapshot.instance.tick_minutes,
            horizon_ticks=settings.risk_horizon_ticks,
            current_tick=snapshot.instance.tick,
        )
        r = risk_engine.assess_station_fuel_risk(
            station=target_station,
            fuel=fuel,
            snapshot=snapshot,
            forecast=fc,
            demand_history=history_records,
            data_age_seconds=data_age,
            is_stale=is_stale,
            horizon_ticks=settings.risk_horizon_ticks,
        )
        station_risks_dict[fuel.value] = r.model_dump(mode="json")

        if r.severity == RiskSeverity.CRITICAL:
            highest_sev = RiskSeverity.CRITICAL
        elif r.severity == RiskSeverity.WATCH and highest_sev != RiskSeverity.CRITICAL:
            highest_sev = RiskSeverity.WATCH

        if r.severity in (RiskSeverity.CRITICAL, RiskSeverity.WATCH):
            urgency = calculate_urgency_score(
                severity=r.severity,
                hours_to_stockout=r.hours_to_stockout,
                current_fill_ratio=r.current_fill_ratio,
                is_demand_spike=r.drivers.is_demand_spike,
            )
            if r.severity == RiskSeverity.CRITICAL and r.hours_to_stockout is not None:
                headline = (
                    f"CRITICAL: {target_station.name} {fuel.value} stockout "
                    f"in {r.hours_to_stockout:.1f}h"
                )
            else:
                headline = (
                    f"{r.severity.value}: {target_station.name} {fuel.value} "
                    f"at {r.current_fill_ratio:.1%} capacity"
                )
            station_alerts.append(
                RiskAlert(
                    station_id=station_id,
                    station_name=target_station.name,
                    fuel_type=fuel,
                    severity=r.severity,
                    hours_to_stockout=r.hours_to_stockout,
                    first_stockout_tick=r.first_stockout_tick,
                    current_fill_ratio=r.current_fill_ratio,
                    urgency_score=urgency,
                    headline=headline,
                    explanation=r.explanation,
                    created_tick=snapshot.instance.tick,
                ).model_dump(mode="json")
            )

    station_alerts.sort(key=lambda a: a["urgency_score"], reverse=True)

    return StationRiskResponse(
        mode=diagnosis.mode,
        reason=diagnosis.reason,
        station_id=station_id,
        station_name=target_station.name,
        highest_severity=highest_sev.value,
        risks=station_risks_dict,
        alerts=station_alerts,
        evaluated_at=diagnosis.evaluated_at,
    )


@router.get(
    "/plan",
    response_model=PlanResponse,
    summary="Constrained Dispatch Optimization Plan",
    description=(
        "Generates a multi-constraint, urgency-ordered fuel dispatch plan "
        "enforcing route tanker caps, depot reserves, shared dispatch quotas, "
        "and station ullage with risk reduction alternatives."
    ),
)
async def get_dispatch_plan(
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    client: SimulatorClient = Depends(get_simulator_client),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    forecaster: DemandForecaster = Depends(get_forecaster),
    risk_engine: RiskEngine = Depends(get_risk_engine),
    planner: ConstrainedPlanner = Depends(get_planner),
    manager: ResilienceManager = Depends(get_resilience_manager),
) -> PlanResponse:
    """Evaluate network risk and generate an optimized constrained dispatch plan."""
    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No simulation snapshot available yet to formulate dispatch plan.",
        )

    history_records: list[DemandHistoryRecord] = []
    if breaker.can_execute():
        try:
            resp = await client.get_demand_history(limit=settings.forecast_history_limit)
            history_records = resp.data
        except Exception as exc:
            logger.warning("Failed to fetch demand history from simulator: %s", exc)

    data_age = store.snapshot_age_seconds() or 0.0
    is_stale = store.is_stale(stale_threshold_seconds=settings.stale_threshold_seconds)
    planner_failed = False

    try:
        fleet_fc = forecaster.forecast_all(
            snapshot=snapshot,
            demand_history=history_records,
            horizon_ticks=settings.risk_horizon_ticks,
        )
        fleet_risk = risk_engine.assess_fleet_risk(
            snapshot=snapshot,
            fleet_forecast=fleet_fc,
            demand_history=history_records,
            data_age_seconds=data_age,
            is_stale=is_stale,
            horizon_ticks=settings.risk_horizon_ticks,
        )
        plan = planner.generate_plan(
            snapshot=snapshot,
            fleet_risk=fleet_risk,
        )
    except Exception as exc:
        logger.error("Planning pipeline encountered unexpected error: %s", exc)
        planner_failed = True
        manager.record_forecaster_failure(str(exc))
        from app.intelligence.fallback import FallbackPlanner

        fallback_p = getattr(planner, "fallback_planner", None) or FallbackPlanner(
            settings=settings
        )
        plan = fallback_p.generate_plan(
            snapshot=snapshot, fallback_reason=f"Pipeline exception: {exc}"
        )

    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=planner_failed,
        manager=manager,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    serialized_actions = [a.model_dump(mode="json") for a in plan.actions]
    serialized_unmet = [u.model_dump(mode="json") for u in plan.unmet_targets]
    serialized_summary = plan.shared_capacity_summary.model_dump(mode="json")

    return PlanResponse(
        mode=diagnosis.mode,
        reason=diagnosis.reason,
        plan_id=plan.plan_id,
        created_tick=plan.created_tick,
        total_actions=plan.total_actions,
        total_liters_dispatched=plan.total_liters_dispatched,
        actions=serialized_actions,
        unmet_targets=serialized_unmet,
        shared_capacity_summary=serialized_summary,
        summary=plan.summary,
        evaluated_at=diagnosis.evaluated_at,
    )


@router.post(
    "/twin/simulate",
    response_model=TwinSimulationResponse,
    summary="Monte Carlo Digital Twin Simulation",
    description=(
        "Executes paired Monte Carlo forward inventory simulations (baseline vs treatment) "
        "calibrated with empirical forecast residuals and random seeds."
    ),
)
async def simulate_digital_twin(
    request: TwinSimulateRequest,
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    twin: DigitalTwin = Depends(get_digital_twin),
) -> TwinSimulationResponse:
    """Run comparative digital twin simulation for a station and fuel."""
    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No simulation snapshot available yet to execute digital twin.",
        )

    target_station = next((s for s in snapshot.stations if s.id == request.station_id), None)
    if target_station is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station '{request.station_id}' not found in current simulation topology.",
        )

    try:
        fuel = FuelType(request.fuel_type.upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid fuel type '{request.fuel_type}'. Must be DIESEL, PETROL, or OCTANE.",
        ) from exc

    cand_shipment = None
    if request.candidate_shipment_quantity > 0.0:
        arr_tick = (
            request.candidate_arrival_tick
            if request.candidate_arrival_tick is not None
            else snapshot.instance.tick + 2
        )
        cand_shipment = (arr_tick, request.candidate_shipment_quantity)

    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        forecaster_failed=False,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )

    result = twin.compare_scenarios(
        station=target_station,
        fuel=fuel,
        snapshot=snapshot,
        candidate_shipment=cand_shipment,
        horizon_ticks=settings.risk_horizon_ticks,
        trials=request.trials or settings.twin_trials,
        seed=request.seed if request.seed is not None else settings.twin_random_seed,
    )

    return TwinSimulationResponse(
        mode=diagnosis.mode,
        reason=diagnosis.reason,
        simulation=result.model_dump(mode="json"),
        evaluated_at=diagnosis.evaluated_at,
    )


@router.get(
    "/twin/{station_id}/{fuel_type}",
    response_model=TwinSimulationResponse,
    summary="Station Fuel Digital Twin Simulation",
    description="Runs forward Monte Carlo simulation for a specific station and fuel.",
)
async def get_station_fuel_twin(
    station_id: str,
    fuel_type: str,
    trials: int | None = None,
    seed: int | None = None,
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    twin: DigitalTwin = Depends(get_digital_twin),
) -> TwinSimulationResponse:
    """Execute digital twin simulation for a station and fuel without candidate shipment."""
    req = TwinSimulateRequest(
        station_id=station_id,
        fuel_type=fuel_type,
        candidate_shipment_quantity=0.0,
        trials=trials,
        seed=seed,
    )
    return await simulate_digital_twin(
        request=req,
        settings=settings,
        store=store,
        breaker=breaker,
        twin=twin,
    )


# ---------------------------------------------------------------------------
# Recommendations, Approvals & SQLite Audit Ledger Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/recommendations",
    response_model=RecommendationListResponse,
    summary="List Recommendations",
    description="Queries actionable recommendations filtered by status or destination station.",
)
async def list_recommendations(
    status: str | None = None,
    station_id: str | None = None,
    limit: int = 100,
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    decision_service: DecisionService = Depends(get_decision_service),
    manager: ResilienceManager = Depends(get_resilience_manager),
) -> RecommendationListResponse:
    """Retrieve recommendations sorted by urgency score."""
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        manager=manager,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )
    mode = diagnosis.mode
    reason = diagnosis.reason

    recs = decision_service.list_recommendations(status=status, station_id=station_id, limit=limit)
    return RecommendationListResponse(
        mode=mode,
        reason=reason,
        total_count=len(recs),
        recommendations=[r.model_dump(mode="json") for r in recs],
        evaluated_at=datetime.now(UTC),
    )


@router.post(
    "/recommendations/generate",
    response_model=RecommendationListResponse,
    summary="Generate Recommendations from Current State",
    description="Runs constrained planner on latest snapshot and records new proposals.",
)
async def generate_recommendations(
    settings: Settings = Depends(get_app_settings),
    store: StateStore = Depends(get_state_store),
    breaker: CircuitBreaker = Depends(get_circuit_breaker),
    decision_service: DecisionService = Depends(get_decision_service),
    manager: ResilienceManager = Depends(get_resilience_manager),
) -> RecommendationListResponse:
    """Evaluate current network risks and formulate actionable recommendations."""
    diagnosis = diagnose_operational_mode(
        breaker=breaker,
        store=store,
        manager=manager,
        stale_threshold_seconds=settings.stale_threshold_seconds,
    )
    mode = diagnosis.mode
    reason = diagnosis.reason

    snapshot = store.get_effective_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "State repository has no snapshot available. Awaiting first poll cycle.",
                "mode": mode.value,
                "reason": reason,
            },
        )

    force_fallback = mode == OperationalMode.DEGRADED_FALLBACK
    recs = decision_service.generate_recommendations_from_snapshot(
        snapshot, force_fallback=force_fallback
    )
    return RecommendationListResponse(
        mode=mode,
        reason=reason,
        total_count=len(recs),
        recommendations=[r.model_dump(mode="json") for r in recs],
        evaluated_at=datetime.now(UTC),
    )


@router.get(
    "/recommendations/{recommendation_id}",
    response_model=RecommendationResponse,
    summary="Get Recommendation Details",
    description="Fetch single recommendation with full constraints, signals, and alternatives.",
)
async def get_recommendation_by_id(
    recommendation_id: str,
    decision_service: DecisionService = Depends(get_decision_service),
) -> RecommendationResponse:
    """Fetch recommendation by ID."""
    rec = decision_service.get_recommendation(recommendation_id)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail=f"Recommendation '{recommendation_id}' not found.",
        )

    return RecommendationResponse(
        recommendation=rec.model_dump(mode="json"),
        evaluated_at=datetime.now(UTC),
    )


@router.post(
    "/recommendations/{recommendation_id}/approve",
    response_model=RecommendationResponse,
    summary="Approve and Dispatch Recommendation",
    description=(
        "Approves recommendation, generates deterministic idempotency key, "
        "and dispatches allocation to simulator. Records outcome in SQLite audit ledger."
    ),
)
async def approve_recommendation(
    recommendation_id: str,
    body: RecommendationApproveRequest | None = None,
    decision_service: DecisionService = Depends(get_decision_service),
) -> RecommendationResponse:
    """Approve a recommendation and execute allocation dispatch."""
    notes = body.operator_notes if body else None
    try:
        rec = await decision_service.approve_recommendation(
            recommendation_id=recommendation_id,
            operator_notes=notes,
        )
    except ValueError as err:
        err_msg = str(err)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=404, detail=err_msg) from err
        raise HTTPException(status_code=400, detail=err_msg) from err

    return RecommendationResponse(
        recommendation=rec.model_dump(mode="json"),
        evaluated_at=datetime.now(UTC),
    )


@router.post(
    "/recommendations/{recommendation_id}/reject",
    response_model=RecommendationResponse,
    summary="Reject Recommendation",
    description="Rejects a proposed recommendation and logs operator rationale in audit ledger.",
)
async def reject_recommendation(
    recommendation_id: str,
    body: RecommendationRejectRequest | None = None,
    decision_service: DecisionService = Depends(get_decision_service),
) -> RecommendationResponse:
    """Reject a proposed recommendation."""
    notes = body.operator_notes if body else None
    try:
        rec = decision_service.reject_recommendation(
            recommendation_id=recommendation_id,
            operator_notes=notes,
        )
    except ValueError as err:
        err_msg = str(err)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=404, detail=err_msg) from err
        raise HTTPException(status_code=400, detail=err_msg) from err

    return RecommendationResponse(
        recommendation=rec.model_dump(mode="json"),
        evaluated_at=datetime.now(UTC),
    )


@router.get(
    "/decisions/audit",
    response_model=AuditHistoryResponse,
    summary="Get Audit Log History",
    description="Queries historical decision events and state transitions from SQLite ledger.",
)
async def get_audit_history(
    recommendation_id: str | None = None,
    limit: int = 100,
    decision_service: DecisionService = Depends(get_decision_service),
) -> AuditHistoryResponse:
    """Retrieve decision event audit trail."""
    events = decision_service.get_audit_history(recommendation_id=recommendation_id, limit=limit)
    return AuditHistoryResponse(
        total_events=len(events),
        events=events,
        evaluated_at=datetime.now(UTC),
    )


@router.get(
    "/decisions/stats",
    response_model=AuditStatsResponse,
    summary="Get Audit Ledger Statistics",
    description="Returns aggregate compliance statistics, status breakdown, and rejection rates.",
)
async def get_audit_stats(
    decision_service: DecisionService = Depends(get_decision_service),
) -> AuditStatsResponse:
    """Retrieve consolidated audit statistics."""
    stats = decision_service.get_audit_statistics()
    return AuditStatsResponse(
        stats=stats,
        evaluated_at=datetime.now(UTC),
    )


@router.get(
    "/metrics",
    summary="Prometheus Metrics Endpoint",
    description="Exposes platform observability metrics in Prometheus text exposition format.",
    include_in_schema=True,
)
async def get_metrics() -> Response:
    """Serve Prometheus metrics in standard text exposition format."""
    payload, content_type = render_prometheus_metrics()
    return Response(content=payload, media_type=content_type)
