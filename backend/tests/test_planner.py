"""Unit and integration tests for the Constrained Fuel Dispatch Planner."""

from datetime import UTC, datetime

import pytest
import respx
from app.api.main import create_app
from app.domain.models import (
    Allocation,
    AllocationStatus,
    Depot,
    DepotStatus,
    FuelMap,
    FuelType,
    InstanceInfo,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.intelligence.planner import (
    CandidateImpact,
    ConstrainedPlanner,
    ConstraintType,
    MitigationType,
    TwinEvaluatorProtocol,
)
from app.intelligence.risk import StationFuelRisk
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------


def make_test_instance(tick: int = 10, tick_minutes: int = 15) -> InstanceInfo:
    """Helper to generate simulation instance metadata."""
    return InstanceInfo(
        id=1,
        scenario_id="baseline",
        scenario_version="1.0",
        seed=42,
        sim_time="2026-01-01T02:30:00",
        tick=tick,
        tick_minutes=tick_minutes,
        status="RUNNING",
    )


def make_test_depot(
    depot_id: str = "depot-central",
    capacity_liters: float = 100000.0,
    inventory_liters: float = 50000.0,
    fuel: FuelType = FuelType.DIESEL,
    dispatch_capacity: float = 12000.0,
    status: DepotStatus | str = DepotStatus.OPEN,
) -> Depot:
    """Helper to generate test Depot."""
    cap_map = {f.value: 100000.0 for f in FuelType}
    cap_map[fuel.value] = capacity_liters
    inv_map = {f.value: 50000.0 for f in FuelType}
    inv_map[fuel.value] = inventory_liters

    return Depot(
        id=depot_id,
        name=f"Test {depot_id}",
        region_id="region-1",
        status=status,
        dispatch_capacity_per_tick=dispatch_capacity,
        capacity=FuelMap(**cap_map),
        inventory=FuelMap(**inv_map),
    )


def make_test_station(
    station_id: str = "station-north",
    capacity_liters: float = 20000.0,
    inventory_liters: float = 15000.0,
    fuel: FuelType = FuelType.DIESEL,
    status: StationStatus | str = StationStatus.OPEN,
) -> Station:
    """Helper to generate test Station."""
    cap_map = {f.value: 20000.0 for f in FuelType}
    cap_map[fuel.value] = capacity_liters
    inv_map = {f.value: 15000.0 for f in FuelType}
    inv_map[fuel.value] = inventory_liters

    return Station(
        id=station_id,
        name=f"Test {station_id}",
        region_id="region-1",
        status=status,
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity=FuelMap(**cap_map),
        inventory=FuelMap(**inv_map),
    )


def make_test_route(
    route_id: str = "route-c-n",
    source: str = "depot-central",
    dest: str = "station-north",
    transit_ticks: int = 2,
    max_shipment: float = 10000.0,
    status: RouteStatus | str = RouteStatus.AVAILABLE,
) -> Route:
    """Helper to generate test Route."""
    return Route(
        id=route_id,
        source_depot_id=source,
        destination_station_id=dest,
        transit_ticks=transit_ticks,
        max_shipment=max_shipment,
        status=status,
    )


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------


def test_planner_nominal_single_at_risk_station():
    """Verify single critical station produces a timely, fully constrained allocation."""
    instance = make_test_instance(tick=10, tick_minutes=15)
    # Station has 2,000L of 20,000L (10% fill ratio -> CRITICAL)
    station = make_test_station(
        station_id="station-north",
        capacity_liters=20000.0,
        inventory_liters=2000.0,
        fuel=FuelType.DIESEL,
    )
    # Depot has 40,000L, 10,000L dispatch capacity
    depot = make_test_depot(
        depot_id="depot-central",
        inventory_liters=40000.0,
        dispatch_capacity=10000.0,
    )
    # Route max shipment 8,000L, transit 2 ticks
    route = make_test_route(
        route_id="route-c-n",
        source=depot.id,
        dest=station.id,
        transit_ticks=2,
        max_shipment=8000.0,
    )

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    assert plan.total_liters_dispatched == 8000.0
    action = plan.actions[0]

    assert action.station_id == "station-north"
    assert action.fuel_type == FuelType.DIESEL
    assert action.route_id == "route-c-n"
    assert action.source_depot_id == "depot-central"
    assert action.quantity_liters == 8000.0
    # Route tanker max is 8,000L (smaller than depot stock 35k or station ullage 18k)
    assert action.constraints.bottleneck_constraint == ConstraintType.ROUTE_MAX_SHIPMENT
    assert action.constraints.route_max_shipment == 8000.0
    assert action.departure_tick == 11
    assert action.arrival_tick == 13
    assert action.is_timely is True
    assert action.mitigation_type in (
        MitigationType.FULL_PREVENTION,
        MitigationType.PREVENTATIVE_RESTOCK,
    )
    assert action.impact.score > 0.0


def test_planner_urgency_ordering():
    """Verify higher urgency station is prioritized and allocated first in plan."""
    instance = make_test_instance(tick=10)
    # Station A: very low inventory (1,000L -> CRITICAL, high urgency)
    st_a = make_test_station(
        station_id="station-urgent",
        capacity_liters=20000.0,
        inventory_liters=1000.0,
    )
    # Station B: moderate inventory (6,000L -> WATCH, lower urgency)
    st_b = make_test_station(
        station_id="station-watch",
        capacity_liters=20000.0,
        inventory_liters=6000.0,
    )

    depot = make_test_depot(
        depot_id="depot-central",
        inventory_liters=80000.0,
        dispatch_capacity=25000.0,
    )
    route_a = make_test_route("route-a", depot.id, st_a.id, transit_ticks=2, max_shipment=10000.0)
    route_b = make_test_route("route-b", depot.id, st_b.id, transit_ticks=2, max_shipment=10000.0)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[st_a, st_b],
        routes=[route_a, route_b],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 2
    # First action must be station-urgent
    assert plan.actions[0].station_id == "station-urgent"
    assert plan.actions[1].station_id == "station-watch"
    assert plan.actions[0].urgency_score >= plan.actions[1].urgency_score


def test_planner_shared_depot_dispatch_capacity_bookkeeping():
    """Verify shared depot dispatch capacity restricts downstream actions in the same plan."""
    instance = make_test_instance(tick=10)
    st_a = make_test_station("station-1", capacity_liters=20000.0, inventory_liters=1000.0)
    st_b = make_test_station("station-2", capacity_liters=20000.0, inventory_liters=2000.0)

    # Depot only has 10,000L dispatch capacity per tick!
    depot = make_test_depot(
        depot_id="depot-shared",
        inventory_liters=60000.0,
        dispatch_capacity=10000.0,
    )
    route_a = make_test_route("r-1", depot.id, st_a.id, transit_ticks=2, max_shipment=8000.0)
    route_b = make_test_route("r-2", depot.id, st_b.id, transit_ticks=2, max_shipment=8000.0)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[st_a, st_b],
        routes=[route_a, route_b],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 2
    # Action 1 takes 8,000L
    assert plan.actions[0].quantity_liters == 8000.0
    # Action 2 can only take the remaining 2,000L from depot dispatch capacity!
    assert plan.actions[1].quantity_liters == 2000.0
    assert (
        plan.actions[1].constraints.bottleneck_constraint == ConstraintType.DEPOT_DISPATCH_CAPACITY
    )
    assert plan.actions[1].constraints.depot_remaining_dispatch_capacity == 2000.0

    # Total allocated from depot dispatch cannot exceed 10,000L
    util = plan.shared_capacity_summary.depot_dispatch_utilization["depot-shared"]
    assert util["allocated_this_plan"] == 10000.0
    assert util["remaining_capacity"] == 0.0


def test_planner_shared_depot_inventory_and_reserve():
    """Verify depot operational reserve cannot be allocated."""
    instance = make_test_instance(tick=10)
    station = make_test_station("station-1", capacity_liters=30000.0, inventory_liters=1000.0)

    # Depot has 12,000L inventory. With 5,000L reserve, available stock is only 7,000L.
    depot = make_test_depot(
        depot_id="depot-tight",
        inventory_liters=12000.0,
        dispatch_capacity=20000.0,
    )
    route = make_test_route("r-1", depot.id, station.id, transit_ticks=2, max_shipment=15000.0)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot, depot_reserve_liters=5000.0)

    assert plan.total_actions == 1
    action = plan.actions[0]
    # Allocated quantity is strictly limited to 12,000 - 5,000 = 7,000L
    assert action.quantity_liters == 7000.0
    assert action.constraints.bottleneck_constraint == ConstraintType.DEPOT_INVENTORY
    assert action.constraints.depot_available_inventory == 7000.0


def test_planner_effective_station_ullage_constraint():
    """Verify station effective free capacity accounts for in-transit fuel and caps allocation."""
    instance = make_test_instance(tick=10)
    # Station tank capacity: 15,000L. Current inventory: 2,000L (13.3% fill -> at risk!).
    station = make_test_station("station-1", capacity_liters=15000.0, inventory_liters=2000.0)

    # But there is already an in-transit shipment of 10,000L!
    in_transit_alloc = Allocation(
        id=101,
        idempotency_key="existing-key-1",
        source_depot_id="depot-central",
        destination_station_id="station-1",
        route_id="r-1",
        fuel_type=FuelType.DIESEL,
        quantity=10000.0,
        created_tick=8,
        departure_tick=9,
        expected_arrival_tick=11,
        status=AllocationStatus.IN_TRANSIT,
    )

    depot = make_test_depot("depot-central", inventory_liters=50000.0, dispatch_capacity=20000.0)
    # Route max is 10,000L
    route = make_test_route("r-1", depot.id, station.id, transit_ticks=2, max_shipment=10000.0)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[in_transit_alloc],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    action = plan.actions[0]
    # Effective free ullage is 15,000 - (2,000 + 10,000) = 3,000L
    assert action.quantity_liters == 3000.0
    assert action.constraints.bottleneck_constraint == ConstraintType.STATION_ULLAGE
    assert action.constraints.station_effective_free_space == 3000.0


def test_planner_route_max_shipment_constraint():
    """Verify quantity cannot exceed physical route tanker limit."""
    instance = make_test_instance(tick=10)
    station = make_test_station("station-1", capacity_liters=30000.0, inventory_liters=1000.0)
    depot = make_test_depot("depot-central", inventory_liters=80000.0, dispatch_capacity=30000.0)
    # Route tanker limit is small: 4,000L
    route = make_test_route("r-small", depot.id, station.id, transit_ticks=2, max_shipment=4000.0)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    assert plan.actions[0].quantity_liters == 4000.0
    assert plan.actions[0].constraints.bottleneck_constraint == ConstraintType.ROUTE_MAX_SHIPMENT


def test_planner_ignores_disrupted_routes():
    """Verify planner avoids DISRUPTED routes to avoid lost fuel with no refund."""
    instance = make_test_instance(tick=10)
    station = make_test_station("station-1", capacity_liters=20000.0, inventory_liters=2000.0)
    depot = make_test_depot("depot-central", inventory_liters=50000.0, dispatch_capacity=20000.0)

    # Route 1: Faster (1 tick), but DISRUPTED!
    route_disrupted = make_test_route(
        "r-fast-disrupted", depot.id, station.id, transit_ticks=1, status=RouteStatus.DISRUPTED
    )
    # Route 2: Slower (3 ticks), but AVAILABLE!
    route_available = make_test_route(
        "r-slow-ok", depot.id, station.id, transit_ticks=3, status=RouteStatus.AVAILABLE
    )

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route_disrupted, route_available],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    # Must choose the available route
    assert plan.actions[0].route_id == "r-slow-ok"


def test_planner_ignores_closed_depots():
    """Verify planner excludes routes originating from CLOSED depots."""
    instance = make_test_instance(tick=10)
    station = make_test_station("station-1", capacity_liters=20000.0, inventory_liters=2000.0)

    # Depot 1 is CLOSED
    depot_closed = make_test_depot(
        "depot-closed", inventory_liters=60000.0, status=DepotStatus.CLOSED
    )
    # Depot 2 is OPEN
    depot_open = make_test_depot("depot-open", inventory_liters=60000.0, status=DepotStatus.OPEN)

    r_closed = make_test_route("r-closed", depot_closed.id, station.id, transit_ticks=1)
    r_open = make_test_route("r-open", depot_open.id, station.id, transit_ticks=2)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot_closed, depot_open],
        stations=[station],
        routes=[r_closed, r_open],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    assert plan.actions[0].source_depot_id == "depot-open"
    assert plan.actions[0].route_id == "r-open"


def test_planner_feasibility_partial_mitigation():
    """Verify arrival past stockout tick is flagged as PARTIAL_MITIGATION with shortage hours."""
    instance = make_test_instance(tick=10, tick_minutes=15)
    # Station inventory 500L, burns ~85L/tick -> stocks out at tick 16
    station = make_test_station("station-1", capacity_liters=20000.0, inventory_liters=500.0)
    depot = make_test_depot("depot-central", inventory_liters=50000.0)

    # Route takes 8 transit ticks: departs tick 11 -> arrives tick 19!
    # Stockout occurs at tick 16. Shortage window = 19 - 16 = 3 ticks = 45 mins = 0.75h
    route = make_test_route("r-long", depot.id, station.id, transit_ticks=8)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    action = plan.actions[0]
    assert action.arrival_tick == 19
    # Since arrival 19 > stockout tick 16
    assert action.is_timely is False
    assert action.mitigation_type == MitigationType.PARTIAL_MITIGATION
    assert action.unavoidable_shortage_hours is not None
    assert action.unavoidable_shortage_hours > 0.0


def test_planner_top_choice_plus_alternatives():
    """Verify primary recommendation retains up to 2 feasible alternatives."""
    instance = make_test_instance(tick=10)
    station = make_test_station("station-1", capacity_liters=20000.0, inventory_liters=1000.0)

    depot_1 = make_test_depot("depot-1", inventory_liters=60000.0)
    depot_2 = make_test_depot("depot-2", inventory_liters=60000.0)
    depot_3 = make_test_depot("depot-3", inventory_liters=60000.0)

    # 3 routes with different transit times
    r1 = make_test_route("r-1", depot_1.id, station.id, transit_ticks=1, max_shipment=10000.0)
    r2 = make_test_route("r-2", depot_2.id, station.id, transit_ticks=2, max_shipment=9000.0)
    r3 = make_test_route("r-3", depot_3.id, station.id, transit_ticks=3, max_shipment=8000.0)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot_1, depot_2, depot_3],
        stations=[station],
        routes=[r1, r2, r3],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    action = plan.actions[0]
    # Primary is fastest route (r-1)
    assert action.route_id == "r-1"
    # Alternatives list contains the next 2 options
    assert len(action.alternatives) == 2
    assert action.alternatives[0].route_id == "r-2"
    assert action.alternatives[1].route_id == "r-3"
    assert action.alternatives[0].score <= action.impact.score


def test_planner_unmet_targets_when_all_routes_disrupted():
    """Verify station with all disrupted routes is cleanly recorded as an unmet target."""
    instance = make_test_instance(tick=10)
    station = make_test_station("station-isolated", capacity_liters=20000.0, inventory_liters=500.0)
    depot = make_test_depot("depot-1", inventory_liters=60000.0)
    route = make_test_route(
        "r-disrupted", depot.id, station.id, transit_ticks=2, status=RouteStatus.DISRUPTED
    )

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 0
    assert len(plan.unmet_targets) >= 1
    diesel_unmet = next((u for u in plan.unmet_targets if u.fuel_type == FuelType.DIESEL), None)
    assert diesel_unmet is not None
    assert diesel_unmet.station_id == "station-isolated"
    assert diesel_unmet.reason_code == "ALL_ROUTES_DISRUPTED"
    assert "DISRUPTED" in diesel_unmet.explanation


def test_planner_unmet_targets_when_depots_exhausted():
    """Verify target is unmet when source depots have zero available fuel above reserve."""
    instance = make_test_instance(tick=10)
    station = make_test_station("station-1", capacity_liters=20000.0, inventory_liters=500.0)
    # Depot has only 4,000L. With 5,000L reserve, available stock is 0L.
    depot = make_test_depot("depot-empty", inventory_liters=4000.0)
    route = make_test_route("r-1", depot.id, station.id, transit_ticks=2)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot, depot_reserve_liters=5000.0)

    assert plan.total_actions == 0
    assert len(plan.unmet_targets) == 1
    unmet = plan.unmet_targets[0]
    assert unmet.reason_code == "DEPOT_OR_ROUTE_CONSTRAINED"


def test_planner_empty_at_risk_returns_empty_plan():
    """Verify fully healthy stations (all OK) produce 0 actions and 0 unmet targets."""
    instance = make_test_instance(tick=10)
    # Station is 95% full (19,000 of 20,000L) -> OK severity
    station = make_test_station("station-full", capacity_liters=20000.0, inventory_liters=19000.0)
    depot = make_test_depot("depot-1", inventory_liters=60000.0)
    route = make_test_route("r-1", depot.id, station.id, transit_ticks=2)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 0
    assert plan.total_liters_dispatched == 0.0
    assert len(plan.actions) == 0
    assert len(plan.unmet_targets) == 0


def test_planner_minimum_shipment_threshold():
    """Verify allocations below minimum threshold are not dispatched as uneconomical tankers."""
    instance = make_test_instance(tick=10)
    # Station is at 16,800L of 20,000L. Target 85% is 17,000L -> deficit is only 200L!
    station = make_test_station(
        "station-near-target", capacity_liters=20000.0, inventory_liters=16800.0
    )
    depot = make_test_depot("depot-1", inventory_liters=60000.0)
    route = make_test_route("r-1", depot.id, station.id, transit_ticks=2)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    planner = ConstrainedPlanner()
    # Min shipment threshold is 500L; deficit 200L is too small
    plan = planner.generate_plan(snapshot=snapshot, min_shipment_liters=500.0)

    assert plan.total_actions == 0


def test_planner_twin_evaluator_custom_override():
    """Verify custom TwinEvaluatorProtocol implementation plugs into planner scoring."""

    class CustomMockTwin(TwinEvaluatorProtocol):
        def evaluate(
            self,
            candidate_quantity: float,
            route: Route,
            station: Station,
            fuel: FuelType,
            snapshot: SimulatorSnapshot,
            risk: StationFuelRisk,
            departure_tick: int,
            arrival_tick: int,
            is_timely: bool,
        ) -> CandidateImpact:
            return CandidateImpact(
                expected_unmet_reduction_liters=candidate_quantity,
                hours_of_cover_added=12.5,
                stockout_probability_before=0.88,
                stockout_probability_after=0.03,
                score=9999.0,
                scoring_details={"custom_twin": 9999.0},
            )

    instance = make_test_instance(tick=10)
    station = make_test_station("station-1", capacity_liters=20000.0, inventory_liters=2000.0)
    depot = make_test_depot("depot-1", inventory_liters=60000.0)
    route = make_test_route("r-1", depot.id, station.id, transit_ticks=2)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    custom_twin = CustomMockTwin()
    planner = ConstrainedPlanner(twin_evaluator=custom_twin)
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    assert plan.actions[0].impact.score == 9999.0
    assert plan.actions[0].impact.stockout_probability_after == 0.03
    assert plan.actions[0].impact.hours_of_cover_added == 12.5


# ---------------------------------------------------------------------------
# API Integration Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_dispatch_plan_api_endpoint():
    """Test GET /plan API endpoint returns complete plan and shared capacity audit."""
    respx.get("http://localhost:8000/v1/demand-history").respond(json=[])
    app = create_app(start_poller=False)

    instance = make_test_instance(tick=10)
    station = make_test_station("station-north", capacity_liters=20000.0, inventory_liters=2000.0)
    depot = make_test_depot("depot-central", inventory_liters=60000.0, dispatch_capacity=10000.0)
    route = make_test_route("r-1", depot.id, station.id, transit_ticks=2, max_shipment=8000.0)

    snapshot = SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )

    # Ingest snapshot into store
    app.state.store.update(snapshot)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/plan")
        assert resp.status_code == 200
        data = resp.json()

        assert data["mode"] == "NORMAL"
        assert data["created_tick"] == 10
        assert data["total_actions"] == 1
        assert data["total_liters_dispatched"] == 8000.0
        assert len(data["actions"]) == 1
        action = data["actions"][0]
        assert action["station_id"] == "station-north"
        assert action["quantity_liters"] == 8000.0
        assert action["constraints"]["bottleneck_constraint"] == "ROUTE_MAX_SHIPMENT"
        assert "shared_capacity_summary" in data
        assert "depot_dispatch_utilization" in data["shared_capacity_summary"]
