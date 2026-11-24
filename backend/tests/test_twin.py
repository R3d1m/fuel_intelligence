"""Unit and integration tests for the Monte Carlo Digital Twin Simulation Engine."""

from datetime import UTC, datetime

import pytest
from app.api.main import create_app
from app.domain.models import (
    Depot,
    FuelMap,
    FuelType,
    InstanceInfo,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.intelligence.planner import ConstrainedPlanner
from app.intelligence.risk import RiskEngine
from app.intelligence.twin import DigitalTwin
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


def make_test_station(
    station_id: str = "station-1",
    capacity_liters: float = 20000.0,
    inventory_liters: float = 2000.0,
    fuel: FuelType = FuelType.DIESEL,
) -> Station:
    """Helper to generate test Station."""
    cap_map = {f.value: 20000.0 for f in FuelType}
    cap_map[fuel.value] = capacity_liters
    inv_map = {f.value: 10000.0 for f in FuelType}
    inv_map[fuel.value] = inventory_liters

    return Station(
        id=station_id,
        name=f"Test {station_id}",
        region_id="region-1",
        status=StationStatus.OPEN,
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity=FuelMap(**cap_map),
        inventory=FuelMap(**inv_map),
    )


def make_test_snapshot(station: Station, tick: int = 10) -> SimulatorSnapshot:
    """Helper to wrap station in a minimal SimulatorSnapshot."""
    instance = make_test_instance(tick=tick)
    depot = Depot(
        id="depot-1",
        name="Test Depot",
        region_id="region-1",
        status="OPEN",
        dispatch_capacity_per_tick=15000.0,
        capacity=FuelMap(DIESEL=100000, PETROL=100000, OCTANE=100000),
        inventory=FuelMap(DIESEL=60000, PETROL=60000, OCTANE=60000),
    )
    route = Route(
        id="r-1",
        source_depot_id="depot-1",
        destination_station_id=station.id,
        transit_ticks=2,
        max_shipment=10000.0,
        status=RouteStatus.AVAILABLE,
    )
    return SimulatorSnapshot(
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=tick,
        sim_time="2026-01-01T02:30:00",
        fetched_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------


def test_twin_reproducibility_with_fixed_seed():
    """Verify that identical random seeds yield byte-identical Monte Carlo simulation results."""
    station = make_test_station("station-seed", capacity_liters=20000.0, inventory_liters=2500.0)
    snapshot = make_test_snapshot(station, tick=10)

    twin = DigitalTwin()

    res_1 = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(13, 8000.0),
        horizon_ticks=24,
        trials=200,
        seed=12345,
    )

    res_2 = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(13, 8000.0),
        horizon_ticks=24,
        trials=200,
        seed=12345,
    )

    assert res_1.baseline.stockout_probability == res_2.baseline.stockout_probability
    assert res_1.with_shipment.stockout_probability == res_2.with_shipment.stockout_probability
    assert res_1.baseline.expected_unmet_liters == res_2.baseline.expected_unmet_liters
    assert res_1.with_shipment.expected_unmet_liters == res_2.with_shipment.expected_unmet_liters
    assert res_1.baseline.median_trajectory == res_2.baseline.median_trajectory
    assert res_1.with_shipment.median_trajectory == res_2.with_shipment.median_trajectory


def test_twin_different_seeds_produce_stochastic_variation():
    """Verify different seeds produce slightly varied stochastic sample realisations."""
    station = make_test_station("station-var", capacity_liters=20000.0, inventory_liters=2500.0)
    snapshot = make_test_snapshot(station, tick=10)

    twin = DigitalTwin()

    res_a = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(13, 8000.0),
        horizon_ticks=24,
        trials=200,
        seed=42,
    )

    res_b = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(13, 8000.0),
        horizon_ticks=24,
        trials=200,
        seed=999,
    )

    # Median trajectory or unmet liters should differ slightly due to different random noise
    assert (
        res_a.baseline.median_trajectory != res_b.baseline.median_trajectory
        or res_a.baseline.expected_unmet_liters != res_b.baseline.expected_unmet_liters
    )


def test_twin_shipment_significantly_reduces_stockout_probability():
    """Verify candidate allocation substantially lowers stockout risk and unmet demand."""
    # Station with low inventory (1,200L) that will deplete within ~10 ticks
    station = make_test_station("station-at-risk", capacity_liters=20000.0, inventory_liters=1200.0)
    snapshot = make_test_snapshot(station, tick=10)

    twin = DigitalTwin()
    # Candidate shipment of 8,000L arrives at tick 12
    result = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(12, 8000.0),
        horizon_ticks=24,
        trials=300,
        seed=42,
    )

    # Baseline counterfactual without shipment should have high stockout probability
    assert result.baseline.stockout_probability > 0.50
    # With treatment shipment arriving at tick 12, stockout probability must drop significantly
    assert result.with_shipment.stockout_probability < result.baseline.stockout_probability
    assert result.stockout_probability_reduction > 0.30
    assert result.relative_risk_reduction_percent > 40.0

    # Expected unmet customer demand drops
    assert result.expected_unmet_reduction_liters > 0.0
    assert result.with_shipment.expected_unmet_liters < result.baseline.expected_unmet_liters
    assert result.hours_of_cover_added > 0.0
    assert result.candidate_score > 0.0


def test_twin_confidence_interval_bounds():
    """Verify confidence interval bounds satisfy lower <= median <= upper."""
    station = make_test_station("station-ci", capacity_liters=20000.0, inventory_liters=2000.0)
    snapshot = make_test_snapshot(station, tick=10)

    twin = DigitalTwin()
    res = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(13, 6000.0),
        horizon_ticks=24,
        trials=300,
        seed=42,
    )

    for scenario in (res.baseline, res.with_shipment):
        ci = scenario.unmet_ci
        assert ci.lower <= ci.median <= ci.upper
        assert ci.ci_percent == 95.0

        min_ci = scenario.min_inventory_ci
        assert min_ci.lower <= min_ci.median <= min_ci.upper


def test_twin_percentile_trajectories_order():
    """Verify that P10 <= P50 <= P90 holds across all forward simulation steps."""
    station = make_test_station("station-pct", capacity_liters=20000.0, inventory_liters=3000.0)
    snapshot = make_test_snapshot(station, tick=10)

    twin = DigitalTwin()
    res = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(12, 5000.0),
        horizon_ticks=20,
        trials=250,
        seed=42,
    )

    for scenario in (res.baseline, res.with_shipment):
        assert len(scenario.p10_trajectory) == 20
        assert len(scenario.median_trajectory) == 20
        assert len(scenario.p90_trajectory) == 20

        for t in range(20):
            p10 = scenario.p10_trajectory[t]
            p50 = scenario.median_trajectory[t]
            p90 = scenario.p90_trajectory[t]
            assert p10 <= p50 <= p90


def test_twin_tank_capacity_capping_no_overflow():
    """Verify forward inventory trajectory never exceeds physical station capacity."""
    # Station tank is already nearly full (18,000 of 20,000L)
    station = make_test_station("station-full", capacity_liters=20000.0, inventory_liters=18000.0)
    snapshot = make_test_snapshot(station, tick=10)

    twin = DigitalTwin()
    # A large shipment of 10,000L arrives
    res = twin.compare_scenarios(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        candidate_shipment=(12, 10000.0),
        horizon_ticks=24,
        trials=200,
        seed=42,
    )

    # At no point should any percentile trajectory exceed physical tank capacity (20,000L)
    for scenario in (res.baseline, res.with_shipment):
        for val in scenario.p90_trajectory:
            assert val <= 20000.0
        for val in scenario.median_trajectory:
            assert val <= 20000.0


def test_twin_higher_residual_std_widens_confidence_interval():
    """Verify larger forecast error variance (residual sigma) widens confidence intervals."""
    twin = DigitalTwin()
    base_demand = [120.0] * 24
    arrivals = {}

    # 1. Low residual noise (sigma = 3.0 L)
    sim_narrow = twin.simulate_single_scenario(
        initial_inventory=2500.0,
        capacity=20000.0,
        base_demand=base_demand,
        scheduled_arrivals=arrivals,
        horizon_ticks=24,
        trials=400,
        residual_std=3.0,
        seed=42,
        label="narrow",
    )

    # 2. High residual noise (sigma = 30.0 L)
    sim_wide = twin.simulate_single_scenario(
        initial_inventory=2500.0,
        capacity=20000.0,
        base_demand=base_demand,
        scheduled_arrivals=arrivals,
        horizon_ticks=24,
        trials=400,
        residual_std=30.0,
        seed=42,
        label="wide",
    )

    spread_narrow = sim_narrow.unmet_ci.upper - sim_narrow.unmet_ci.lower
    spread_wide = sim_wide.unmet_ci.upper - sim_wide.unmet_ci.lower

    # Wider residual variance must produce wider confidence interval
    assert spread_wide > spread_narrow


def test_twin_evaluator_protocol_conformance():
    """Verify DigitalTwin fulfills TwinEvaluatorProtocol interface for planner integration."""
    station = make_test_station("station-proto", capacity_liters=20000.0, inventory_liters=1500.0)
    snapshot = make_test_snapshot(station, tick=10)
    route = snapshot.routes[0]

    risk_engine = RiskEngine()
    risk = risk_engine.assess_station_fuel_risk(
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
    )

    twin = DigitalTwin()
    impact = twin.evaluate(
        candidate_quantity=8000.0,
        route=route,
        station=station,
        fuel=FuelType.DIESEL,
        snapshot=snapshot,
        risk=risk,
        departure_tick=11,
        arrival_tick=13,
        is_timely=True,
    )

    assert impact.stockout_probability_before >= impact.stockout_probability_after
    assert impact.hours_of_cover_added > 0.0
    assert impact.score > 0.0
    assert "prob_reduction_pts" in impact.scoring_details
    assert "unmet_reduction_pts" in impact.scoring_details


def test_twin_planner_end_to_end_integration():
    """Verify ConstrainedPlanner uses DigitalTwin to evaluate and rank actions."""
    station = make_test_station("station-e2e", capacity_liters=20000.0, inventory_liters=1000.0)
    snapshot = make_test_snapshot(station, tick=10)

    twin = DigitalTwin()
    planner = ConstrainedPlanner(twin_evaluator=twin)
    plan = planner.generate_plan(snapshot=snapshot)

    assert plan.total_actions == 1
    action = plan.actions[0]
    assert action.quantity_liters > 0.0
    # Monte Carlo probabilities should be populated
    assert 0.0 <= action.impact.stockout_probability_before <= 1.0
    assert 0.0 <= action.impact.stockout_probability_after <= 1.0
    assert action.impact.stockout_probability_after <= action.impact.stockout_probability_before


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_twin_simulate_api_post_endpoint():
    """Test POST /twin/simulate endpoint executes Monte Carlo simulation."""
    app = create_app(start_poller=False)

    station = make_test_station("station-api", capacity_liters=20000.0, inventory_liters=1500.0)
    snapshot = make_test_snapshot(station, tick=10)
    app.state.store.update(snapshot)

    payload = {
        "station_id": "station-api",
        "fuel_type": "DIESEL",
        "candidate_shipment_quantity": 8000.0,
        "candidate_arrival_tick": 13,
        "trials": 150,
        "seed": 42,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/twin/simulate", json=payload)
        assert resp.status_code == 200
        data = resp.json()

        assert data["mode"] == "NORMAL"
        sim = data["simulation"]
        assert sim["station_id"] == "station-api"
        assert sim["fuel_type"] == "DIESEL"
        assert sim["trials_count"] == 150
        assert sim["random_seed"] == 42
        assert "baseline" in sim
        assert "with_shipment" in sim
        assert (
            sim["baseline"]["stockout_probability"] > sim["with_shipment"]["stockout_probability"]
        )
        assert "unmet_ci" in sim["baseline"]
        assert "median_trajectory" in sim["baseline"]


@pytest.mark.asyncio
async def test_twin_get_endpoint():
    """Test GET /twin/{station_id}/{fuel} convenience endpoint."""
    app = create_app(start_poller=False)

    station = make_test_station("station-get", capacity_liters=20000.0, inventory_liters=2000.0)
    snapshot = make_test_snapshot(station, tick=10)
    app.state.store.update(snapshot)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/twin/station-get/DIESEL?trials=100&seed=42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["simulation"]["station_id"] == "station-get"
        assert data["simulation"]["trials_count"] == 100


@pytest.mark.asyncio
async def test_twin_api_404_when_station_unknown():
    """Test POST /twin/simulate returns 404 for nonexistent station."""
    app = create_app(start_poller=False)
    station = make_test_station("station-1")
    snapshot = make_test_snapshot(station, tick=10)
    app.state.store.update(snapshot)

    payload = {"station_id": "nonexistent-station", "fuel_type": "DIESEL"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/twin/simulate", json=payload)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_twin_api_422_when_invalid_fuel():
    """Test POST /twin/simulate returns 422 for unsupported fuel type."""
    app = create_app(start_poller=False)
    station = make_test_station("station-1")
    snapshot = make_test_snapshot(station, tick=10)
    app.state.store.update(snapshot)

    payload = {"station_id": "station-1", "fuel_type": "KEROSENE"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/twin/simulate", json=payload)
        assert resp.status_code == 422
        assert "invalid fuel type" in resp.json()["detail"].lower()
