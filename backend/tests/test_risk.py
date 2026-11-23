"""Unit tests for the Fuel Inventory Risk Engine and Forward-Projection Evaluator."""

from datetime import UTC, datetime

from app.domain.models import (
    Allocation,
    AllocationStatus,
    Depot,
    FuelMap,
    FuelType,
    InstanceInfo,
    RiskSeverity,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
    SupplyArrival,
)
from app.intelligence.forecast import (
    AnomalyReport,
    ForecastMetrics,
    ForecastPoint,
    StationFuelForecast,
)
from app.intelligence.risk import (
    RiskEngine,
    calculate_allocation_arrival_tick,
    calculate_urgency_score,
    compute_confidence_score,
)


def make_dummy_instance(tick: int = 10, tick_minutes: int = 15) -> InstanceInfo:
    """Helper to create dummy simulation instance metadata."""
    return InstanceInfo(
        id=1,
        scenario_id="baseline",
        scenario_version="1.0",
        seed=12345,
        sim_time="2026-01-01T02:30:00",
        tick=tick,
        tick_minutes=tick_minutes,
        status="RUNNING",
    )


def make_dummy_station(
    station_id: str = "station-mirpur",
    capacity_liters: float = 15000.0,
    inventory_liters: float = 9000.0,
    fuel: FuelType = FuelType.DIESEL,
    demand_multiplier: float = 1.0,
) -> Station:
    """Helper to create dummy Station."""
    cap_map = {f: 10000.0 for f in FuelType}
    cap_map[fuel] = capacity_liters
    inv_map = {f: 5000.0 for f in FuelType}
    inv_map[fuel] = inventory_liters

    return Station(
        id=station_id,
        name=f"Test {station_id}",
        region_id="region-dhaka",
        status=StationStatus.OPEN,
        demand_profile="urban_high",
        demand_multiplier=demand_multiplier,
        capacity=FuelMap(**{f.value: v for f, v in cap_map.items()}),
        inventory=FuelMap(**{f.value: v for f, v in inv_map.items()}),
    )


def make_dummy_depot(
    depot_id: str = "depot-gazipur",
    capacity_liters: float = 100000.0,
    inventory_liters: float = 60000.0,
    fuel: FuelType = FuelType.DIESEL,
) -> Depot:
    """Helper to create dummy Depot."""
    cap_map = {f: 100000.0 for f in FuelType}
    cap_map[fuel] = capacity_liters
    inv_map = {f: 60000.0 for f in FuelType}
    inv_map[fuel] = inventory_liters

    return Depot(
        id=depot_id,
        name=f"Test {depot_id}",
        region_id="region-dhaka",
        status="OPEN",
        dispatch_capacity_per_tick=10000.0,
        capacity=FuelMap(**{f.value: v for f, v in cap_map.items()}),
        inventory=FuelMap(**{f.value: v for f, v in inv_map.items()}),
    )


def make_dummy_route(
    route_id: str = "route-gazipur-mirpur",
    source: str = "depot-gazipur",
    dest: str = "station-mirpur",
    transit_ticks: int = 2,
    status: RouteStatus = RouteStatus.AVAILABLE,
) -> Route:
    """Helper to create dummy Route."""
    return Route(
        id=route_id,
        source_depot_id=source,
        destination_station_id=dest,
        transit_ticks=transit_ticks,
        max_shipment=7000.0,
        status=status,
    )


def make_dummy_snapshot(
    instance: InstanceInfo | None = None,
    stations: list[Station] | None = None,
    depots: list[Depot] | None = None,
    routes: list[Route] | None = None,
    allocations: list[Allocation] | None = None,
    supply_arrivals: list[SupplyArrival] | None = None,
    is_stale: bool = False,
) -> SimulatorSnapshot:
    """Helper to create a full SimulatorSnapshot."""
    inst = instance or make_dummy_instance()
    st_list = stations or [make_dummy_station()]
    dp_list = depots or [make_dummy_depot()]
    rt_list = routes or [make_dummy_route()]
    al_list = allocations or []
    sa_list = supply_arrivals or []

    return SimulatorSnapshot(
        instance=inst,
        depots=dp_list,
        stations=st_list,
        routes=rt_list,
        supply_arrivals=sa_list,
        events=[],
        allocations=al_list,
        tick=inst.tick,
        sim_time=str(inst.sim_time),
        fetched_at=datetime.now(UTC),
        is_stale=is_stale,
    )


def make_dummy_forecast(
    station_id: str = "station-mirpur",
    fuel: FuelType = FuelType.DIESEL,
    forecast_per_tick: float = 100.0,
    horizon_ticks: int = 24,
    current_tick: int = 10,
    is_spike: bool = False,
    mape: float = 5.0,
) -> StationFuelForecast:
    """Helper to generate a mock forecast for testing."""
    trajectory = [
        ForecastPoint(
            tick=current_tick + step,
            forecast_liters_per_tick=forecast_per_tick,
            forecast_liters_per_hour=forecast_per_tick * 4.0,
            lower_bound_liters=max(0.0, forecast_per_tick - 10.0),
            upper_bound_liters=forecast_per_tick + 10.0,
        )
        for step in range(1, horizon_ticks + 1)
    ]
    return StationFuelForecast(
        station_id=station_id,
        fuel_type=fuel,
        forecast_per_tick=forecast_per_tick,
        forecast_per_hour=forecast_per_tick * 4.0,
        horizon_ticks=horizon_ticks,
        model_used="TEST_MOCK",
        trajectory=trajectory,
        metrics=ForecastMetrics(
            mae=3.0,
            mape=mape,
            rmse=4.0,
            residual_std=2.5,
            sample_count=20,
        ),
        anomaly=AnomalyReport(
            is_spike=is_spike,
            severity=RiskSeverity.CRITICAL if is_spike else RiskSeverity.OK,
            multiplier_spike=is_spike,
            statistical_spike=is_spike,
            demand_multiplier=2.0 if is_spike else 1.0,
            spike_ratio=2.0 if is_spike else 1.0,
            explanation="Test anomaly" if is_spike else "Nominal demand",
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_station_fuel_risk_nominal_ok():
    """Verify healthy station with ample inventory evaluates to RiskSeverity.OK."""
    station = make_dummy_station(
        capacity_liters=15000.0,
        inventory_liters=12000.0,  # 80% full
        fuel=FuelType.DIESEL,
    )
    snapshot = make_dummy_snapshot(stations=[station])
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.DIESEL,
        forecast_per_tick=100.0,  # 24 ticks * 100 = 2400 L consumption
        horizon_ticks=24,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        forecast=forecast,
    )

    assert risk.severity == RiskSeverity.OK
    assert risk.first_stockout_tick is None
    assert risk.hours_to_stockout is None
    assert risk.is_unavoidable_stockout is False
    assert risk.total_unmet_liters == 0.0
    assert risk.current_fill_ratio == 0.8
    assert risk.confidence_level == "HIGH"
    assert risk.needs_human_review is False
    assert len(risk.trajectory) == 24
    # End inventory should be 12000 - 2400 = 9600 L
    assert risk.trajectory[-1].projected_inventory == 9600.0


def test_station_fuel_risk_critical_low_inventory():
    """Verify tank inventory below 20% threshold triggers RiskSeverity.CRITICAL."""
    station = make_dummy_station(
        capacity_liters=15000.0,
        inventory_liters=2000.0,  # 13.3% (< 20%)
        fuel=FuelType.DIESEL,
    )
    snapshot = make_dummy_snapshot(stations=[station])
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.DIESEL,
        forecast_per_tick=50.0,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        forecast=forecast,
    )

    assert risk.severity == RiskSeverity.CRITICAL
    assert risk.drivers.is_low_inventory is True
    assert risk.needs_human_review is True
    assert "critically depleted" in risk.explanation.lower()


def test_station_fuel_risk_imminent_stockout_timing():
    """Verify exact stockout tick and hours calculation when demand depletes inventory."""
    station = make_dummy_station(
        capacity_liters=10000.0,
        inventory_liters=350.0,  # Depleted in 4 ticks at 100 L/tick
        fuel=FuelType.PETROL,
    )
    current_tick = 10
    instance = make_dummy_instance(tick=current_tick, tick_minutes=15)
    snapshot = make_dummy_snapshot(instance=instance, stations=[station])
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.PETROL,
        forecast_per_tick=100.0,
        current_tick=current_tick,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.PETROL,
        snapshot=snapshot,
        forecast=forecast,
    )

    # Tick 11: 350 - 100 = 250
    # Tick 12: 250 - 100 = 150
    # Tick 13: 150 - 100 = 50
    # Tick 14: 50 - 100 -> Stockout! 50 unmet demand.
    assert risk.first_stockout_tick == 14
    assert risk.ticks_to_stockout == 4
    # 4 ticks * 15 min / 60 min = 1.0 hour
    assert risk.hours_to_stockout == 1.0
    assert risk.severity == RiskSeverity.CRITICAL
    assert risk.total_unmet_liters > 0.0
    assert risk.trajectory[3].is_stockout is True  # step 4 (0-indexed 3)


def test_station_fuel_risk_mitigated_by_in_transit_shipment():
    """Verify in-transit allocation arriving prior to stockout prevents depletion."""
    station = make_dummy_station(
        capacity_liters=10000.0,
        inventory_liters=250.0,  # Would stock out at tick 13 without arrival
        fuel=FuelType.DIESEL,
    )
    current_tick = 10
    instance = make_dummy_instance(tick=current_tick, tick_minutes=15)
    route = make_dummy_route(transit_ticks=2)

    # In-transit allocation expected to arrive at tick 12
    alloc = Allocation(
        id=101,
        idempotency_key="test-key-101",
        source_depot_id="depot-gazipur",
        destination_station_id=station.id,
        route_id=route.id,
        fuel_type=FuelType.DIESEL,
        quantity=3000.0,
        created_tick=9,
        departure_tick=10,
        expected_arrival_tick=12,
        status=AllocationStatus.IN_TRANSIT,
    )

    snapshot = make_dummy_snapshot(
        instance=instance,
        stations=[station],
        routes=[route],
        allocations=[alloc],
    )
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.DIESEL,
        forecast_per_tick=100.0,
        current_tick=current_tick,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        forecast=forecast,
    )

    # Step 1 (tick 11): 250 - 100 = 150
    # Step 2 (tick 12): 150 + 3000 (arrival) - 100 = 3050
    # Step 3 (tick 13): 3050 - 100 = 2950
    # Stockout averted!
    assert risk.first_stockout_tick is None
    assert risk.hours_to_stockout is None
    assert risk.drivers.in_transit_shipments_count == 1
    assert risk.drivers.in_transit_liters_total == 3000.0
    assert risk.trajectory[1].incoming_liters == 3000.0
    assert risk.trajectory[1].projected_inventory == 3050.0


def test_station_fuel_risk_unavoidable_shortage_detection():
    """Verify unavoidable stockout flag is raised when stockout precedes fastest delivery."""
    station = make_dummy_station(
        capacity_liters=10000.0,
        inventory_liters=80.0,  # Stocks out at tick 11
        fuel=FuelType.DIESEL,
    )
    current_tick = 10
    instance = make_dummy_instance(tick=current_tick, tick_minutes=15)
    # Available route has transit_ticks = 3
    # Earliest dispatch departure next tick (11) + 3 transit = arrival at tick 14
    route = make_dummy_route(transit_ticks=3, status=RouteStatus.AVAILABLE)

    snapshot = make_dummy_snapshot(
        instance=instance,
        stations=[station],
        routes=[route],
    )
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.DIESEL,
        forecast_per_tick=100.0,
        current_tick=current_tick,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        forecast=forecast,
    )

    assert risk.first_stockout_tick == 11
    assert risk.is_unavoidable_stockout is True
    # Stockout at tick 11, earliest possible delivery is tick 14 (10 + 1 + 3)
    # Unavoidable ticks: 14 - 11 = 3 ticks = 0.75 hours
    assert risk.unavoidable_shortage_hours == 0.75
    assert "Unavoidable stockout alert" in risk.explanation


def test_station_fuel_risk_isolated_station_all_routes_disrupted():
    """Verify when all routes are DISRUPTED, isolation is flagged and severity escalated."""
    station = make_dummy_station(
        capacity_liters=10000.0,
        inventory_liters=6000.0,  # 60% full (nominally OK)
        fuel=FuelType.DIESEL,
    )
    route1 = make_dummy_route(route_id="r1", transit_ticks=2, status=RouteStatus.DISRUPTED)
    route2 = make_dummy_route(route_id="r2", transit_ticks=4, status=RouteStatus.DISRUPTED)

    snapshot = make_dummy_snapshot(
        stations=[station],
        routes=[route1, route2],
    )
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.DIESEL,
        forecast_per_tick=50.0,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        forecast=forecast,
    )

    assert risk.drivers.all_supply_routes_disrupted is True
    assert risk.drivers.available_supply_routes_count == 0
    # Even though fill ratio is 60%, isolation escalates severity to WATCH
    assert risk.severity == RiskSeverity.WATCH
    assert "isolated" in risk.explanation.lower()


def test_station_fuel_risk_pending_on_disrupted_route_ignored():
    """Verify pending allocation on a DISRUPTED route is NOT counted as valid incoming fuel."""
    station = make_dummy_station(
        capacity_liters=10000.0,
        inventory_liters=150.0,  # Depleted quickly
        fuel=FuelType.DIESEL,
    )
    current_tick = 10
    instance = make_dummy_instance(tick=current_tick)
    route = make_dummy_route(transit_ticks=2, status=RouteStatus.DISRUPTED)

    # PENDING allocation on a disrupted route
    alloc = Allocation(
        id=202,
        idempotency_key="disrupted-alloc-202",
        source_depot_id="depot-gazipur",
        destination_station_id=station.id,
        route_id=route.id,
        fuel_type=FuelType.DIESEL,
        quantity=4000.0,
        created_tick=current_tick,
        status=AllocationStatus.PENDING,
    )

    snapshot = make_dummy_snapshot(
        instance=instance,
        stations=[station],
        routes=[route],
        allocations=[alloc],
    )
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.DIESEL,
        forecast_per_tick=100.0,
        current_tick=current_tick,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        forecast=forecast,
    )

    # Allocation should NOT be counted in pending_liters because route is disrupted
    assert risk.drivers.pending_liters_total == 0.0
    assert "will fail at departure" in risk.explanation
    # Station still stocks out because pending fuel is rejected
    assert risk.first_stockout_tick is not None


def test_station_fuel_risk_tank_overflow_capped():
    """Verify incoming shipments exceeding tank capacity are capped and overflow is recorded."""
    station = make_dummy_station(
        capacity_liters=10000.0,
        inventory_liters=8000.0,
        fuel=FuelType.DIESEL,
    )
    current_tick = 10
    instance = make_dummy_instance(tick=current_tick)
    route = make_dummy_route(transit_ticks=1)

    # In-transit delivery of 5000 L arriving at tick 11
    # 8000 + 5000 = 13000 > 10000 (overflow 3000 L)
    alloc = Allocation(
        id=303,
        idempotency_key="overflow-alloc-303",
        source_depot_id="depot-gazipur",
        destination_station_id=station.id,
        route_id=route.id,
        fuel_type=FuelType.DIESEL,
        quantity=5000.0,
        created_tick=9,
        expected_arrival_tick=11,
        status=AllocationStatus.IN_TRANSIT,
    )

    snapshot = make_dummy_snapshot(
        instance=instance,
        stations=[station],
        routes=[route],
        allocations=[alloc],
    )
    forecast = make_dummy_forecast(
        station_id=station.id,
        fuel=FuelType.DIESEL,
        forecast_per_tick=100.0,
        current_tick=current_tick,
    )

    engine = RiskEngine()
    risk = engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        forecast=forecast,
    )

    step1 = risk.trajectory[0]
    assert step1.projected_overflow_liters == 3000.0
    # Capped inventory minus demand: 10000 - 100 = 9900
    assert step1.projected_inventory == 9900.0


def test_confidence_calculation_penalized_by_staleness_and_mape():
    """Verify confidence score degradation under stale data and high forecast MAPE."""
    # 1. Nominal fresh + accurate
    c_fresh, lvl_fresh, review_fresh = compute_confidence_score(
        data_age_seconds=2.0,
        is_stale=False,
        stale_threshold_seconds=10.0,
        forecast_mape=5.0,
        sample_count=20,
    )
    assert c_fresh >= 0.85
    assert lvl_fresh == "HIGH"
    assert review_fresh is False

    # 2. Flagged stale
    c_stale, lvl_stale, review_stale = compute_confidence_score(
        data_age_seconds=15.0,
        is_stale=True,
        stale_threshold_seconds=10.0,
        forecast_mape=5.0,
        sample_count=20,
    )
    assert c_stale <= 0.40
    assert lvl_stale == "LOW"
    assert review_stale is True

    # 3. High MAPE (50%)
    c_high_err, _, _ = compute_confidence_score(
        data_age_seconds=2.0,
        is_stale=False,
        stale_threshold_seconds=10.0,
        forecast_mape=50.0,
        sample_count=20,
    )
    assert c_high_err < c_fresh


def test_fleet_risk_assessment_aggregation_and_alert_ranking():
    """Verify fleet risk assessment correctly tallies severities and ranks alerts by urgency."""
    # Station 1: CRITICAL (stocks out in 1 hour)
    st1 = make_dummy_station(
        station_id="st-1",
        capacity_liters=10000.0,
        inventory_liters=200.0,
        fuel=FuelType.DIESEL,
    )
    # Station 2: WATCH (stocks out in 6 hours)
    st2 = make_dummy_station(
        station_id="st-2",
        capacity_liters=10000.0,
        inventory_liters=2400.0,
        fuel=FuelType.DIESEL,
    )
    # Station 3: OK (ample inventory)
    st3 = make_dummy_station(
        station_id="st-3",
        capacity_liters=10000.0,
        inventory_liters=8000.0,
        fuel=FuelType.DIESEL,
    )

    current_tick = 10
    instance = make_dummy_instance(tick=current_tick)
    snapshot = make_dummy_snapshot(
        instance=instance,
        stations=[st1, st2, st3],
    )

    engine = RiskEngine()
    fleet_risk = engine.assess_fleet_risk(snapshot=snapshot)

    assert fleet_risk.overall_severity == RiskSeverity.CRITICAL
    assert fleet_risk.critical_count >= 1
    assert fleet_risk.earliest_stockout_station_id == "st-1"
    assert len(fleet_risk.active_alerts) >= 2

    # Verify alerts are sorted by urgency score descending
    scores = [a.urgency_score for a in fleet_risk.active_alerts]
    assert scores == sorted(scores, reverse=True)
    assert fleet_risk.active_alerts[0].station_id == "st-1"


def test_depot_fuel_risk_evaluation():
    """Verify depot inventory risk considers pending allocations and scheduled replenishment."""
    depot = make_dummy_depot(
        depot_id="depot-gazipur",
        capacity_liters=100000.0,
        inventory_liters=10000.0,  # 10% inventory
        fuel=FuelType.DIESEL,
    )
    current_tick = 10
    instance = make_dummy_instance(tick=current_tick)

    # Outbound pending allocation of 5000 L
    alloc = Allocation(
        id=404,
        idempotency_key="depot-alloc-404",
        source_depot_id=depot.id,
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type=FuelType.DIESEL,
        quantity=5000.0,
        created_tick=current_tick,
        status=AllocationStatus.PENDING,
    )

    snapshot = make_dummy_snapshot(
        instance=instance,
        depots=[depot],
        allocations=[alloc],
    )

    engine = RiskEngine()
    depot_risk = engine.assess_depot_fuel_risk(
        depot=depot,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
    )

    # Available: 10000 - 5000 = 5000 L / 100000 = 5% (< 15% critical)
    assert depot_risk.severity == RiskSeverity.CRITICAL
    assert depot_risk.effective_available_liters == 5000.0
    assert "critically constrained" in depot_risk.explanation.lower()


def test_allocation_arrival_tick_helper():
    """Verify calculate_allocation_arrival_tick handles expected, departed, and pending states."""
    route = make_dummy_route(route_id="r1", transit_ticks=3)

    # 1. Explicit expected arrival tick
    a1 = Allocation(
        id=1,
        idempotency_key="k1",
        source_depot_id="d1",
        destination_station_id="s1",
        route_id="r1",
        fuel_type=FuelType.DIESEL,
        quantity=1000.0,
        created_tick=5,
        expected_arrival_tick=12,
        status=AllocationStatus.IN_TRANSIT,
    )
    assert calculate_allocation_arrival_tick(a1, [route], current_tick=10) == 12

    # 2. Departed tick + transit
    a2 = Allocation(
        id=2,
        idempotency_key="k2",
        source_depot_id="d1",
        destination_station_id="s1",
        route_id="r1",
        fuel_type=FuelType.DIESEL,
        quantity=1000.0,
        created_tick=5,
        departure_tick=8,
        status=AllocationStatus.IN_TRANSIT,
    )
    assert calculate_allocation_arrival_tick(a2, [route], current_tick=10) == 11  # 8 + 3

    # 3. Pending (departs on next tick: current_tick + 1 + transit)
    a3 = Allocation(
        id=3,
        idempotency_key="k3",
        source_depot_id="d1",
        destination_station_id="s1",
        route_id="r1",
        fuel_type=FuelType.DIESEL,
        quantity=1000.0,
        created_tick=10,
        status=AllocationStatus.PENDING,
    )
    assert calculate_allocation_arrival_tick(a3, [route], current_tick=10) == 14  # 10 + 1 + 3


def test_urgency_score_order():
    """Verify urgency score properly ranks severity and proximity."""
    critical_soon = calculate_urgency_score(RiskSeverity.CRITICAL, 1.0, 0.1, True)
    critical_later = calculate_urgency_score(RiskSeverity.CRITICAL, 5.0, 0.1, False)
    watch_score = calculate_urgency_score(RiskSeverity.WATCH, 4.0, 0.3, False)
    ok_score = calculate_urgency_score(RiskSeverity.OK, None, 0.8, False)

    assert critical_soon > critical_later
    assert critical_later > watch_score
    assert watch_score > ok_score
