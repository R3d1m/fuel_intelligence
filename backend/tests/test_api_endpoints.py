"""Integration tests for FastAPI health and state endpoints."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from app.adapters.simulator_client import SimulatorClient
from app.api.main import create_app
from app.config import Settings
from app.decisions.audit import AuditLedger
from app.domain.models import (
    Depot,
    DepotStatus,
    FuelMap,
    InstanceInfo,
    OperationalMode,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.resilience.breaker import CircuitBreaker


def _make_dummy_snapshot(is_stale: bool = False, tick: int = 15) -> SimulatorSnapshot:
    return SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="bup-standard",
            scenario_version="1.0",
            seed=12345,
            sim_time="2026-11-22T10:00:00Z",
            tick=tick,
            tick_minutes=15,
            status="RUNNING",
        ),
        depots=[],
        stations=[],
        routes=[],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=tick,
        sim_time="2026-11-22T10:00:00Z",
        fetched_at=datetime.now(UTC),
        is_stale=is_stale,
    )


@pytest.fixture
def test_setup() -> dict[str, Any]:
    settings = Settings(
        simulator_base_url="http://test-sim:8000",
        poll_interval_seconds=1.0,
        stale_threshold_seconds=10.0,
        circuit_breaker_failure_threshold=3,
        circuit_breaker_recovery_timeout=5.0,
        audit_db_path=":memory:",
    )
    client = SimulatorClient(base_url="http://test-sim:8000")
    store = StateStore()
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=5.0, name="test-breaker")
    poller = SimulatorPoller(client=client, store=store, breaker=breaker, settings=settings)
    audit_ledger = AuditLedger(db_path=":memory:")
    app = create_app(
        settings=settings,
        client=client,
        store=store,
        breaker=breaker,
        poller=poller,
        audit_ledger=audit_ledger,
        start_poller=False,
    )
    return {
        "app": app,
        "store": store,
        "breaker": breaker,
        "poller": poller,
        "client": client,
        "audit_ledger": audit_ledger,
        "settings": settings,
    }


@pytest.mark.asyncio
async def test_health_endpoint_healthy(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    store.update(_make_dummy_snapshot(is_stale=False))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "healthy"
        assert data["mode"] == OperationalMode.NORMAL.value
        assert data["simulator"]["connected"] is True
        assert data["simulator"]["circuit_state"] == "CLOSED"
        assert data["store"]["has_snapshot"] is True
        assert data["store"]["is_stale"] is False
        assert data["store"]["total_updates"] == 1


@pytest.mark.asyncio
async def test_health_endpoint_degraded_when_breaker_open(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    breaker: CircuitBreaker = test_setup["breaker"]
    store: StateStore = test_setup["store"]
    store.update(_make_dummy_snapshot(is_stale=False))

    # Trip breaker
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state.value == "OPEN"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "degraded"
        assert data["mode"] == OperationalMode.DEGRADED_CACHED.value
        assert data["simulator"]["connected"] is False
        assert data["simulator"]["circuit_state"] == "OPEN"
        assert data["simulator"]["cooldown_remaining"] > 0.0


@pytest.mark.asyncio
async def test_state_endpoint_empty_store_returns_503(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/state")
        assert resp.status_code == 503
        data = resp.json()
        assert "No simulator snapshot available yet" in data["detail"]["message"]
        assert data["detail"]["mode"] == OperationalMode.DEGRADED_CACHED.value


@pytest.mark.asyncio
async def test_state_endpoint_serves_snapshot_in_normal_mode(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    store.update(_make_dummy_snapshot(is_stale=False, tick=42))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/state")
        assert resp.status_code == 200
        data = resp.json()

        assert data["mode"] == OperationalMode.NORMAL.value
        assert not data["is_stale"]
        assert data["circuit_state"] == "CLOSED"
        assert data["snapshot"]["tick"] == 42
        assert data["snapshot"]["instance"]["scenario_id"] == "bup-standard"


@pytest.mark.asyncio
async def test_state_endpoint_serves_cached_snapshot_during_breaker_trip(
    test_setup: dict[str, Any],
) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    breaker: CircuitBreaker = test_setup["breaker"]

    # Store a good snapshot
    store.update(_make_dummy_snapshot(is_stale=False, tick=88))

    # Simulate simulator outage tripping the circuit breaker
    for _ in range(3):
        breaker.record_failure()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/state")
        assert resp.status_code == 200
        data = resp.json()

        # CRITICAL RESILIENCE BEHAVIOR:
        # Platform answers from cached last-known-good state with mode DEGRADED_CACHED
        assert data["mode"] == OperationalMode.DEGRADED_CACHED.value
        assert data["circuit_state"] == "OPEN"
        assert data["snapshot"] is not None
        assert data["snapshot"]["tick"] == 88


@pytest.mark.asyncio
@respx.mock
async def test_sync_endpoint_success(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    base = "http://test-sim:8000"

    # Mock simulator endpoints
    inst_data = {
        "id": 1,
        "scenario_id": "bup-standard",
        "scenario_version": "1.0",
        "seed": 12345,
        "sim_time": "2026-11-22T08:00:00Z",
        "tick": 7,
        "tick_minutes": 15,
        "status": "RUNNING",
    }
    respx.get(f"{base}/v1/instance").respond(json=inst_data)
    respx.get(f"{base}/v1/depots").respond(json=[])
    respx.get(f"{base}/v1/stations").respond(json=[])
    respx.get(f"{base}/v1/routes").respond(json=[])
    respx.get(f"{base}/v1/supply-arrivals").respond(json=[])
    respx.get(f"{base}/v1/events").respond(json=[])
    respx.get(f"{base}/v1/allocations").respond(json=[])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["tick"] == 7
        assert "Successfully synchronized" in data["message"]


@pytest.mark.asyncio
async def test_sync_endpoint_rejected_when_circuit_open(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    breaker: CircuitBreaker = test_setup["breaker"]
    for _ in range(3):
        breaker.record_failure()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "Sync rejected" in data["message"]


@pytest.mark.asyncio
async def test_forecasts_endpoint_503_when_no_snapshot(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/forecasts")
        assert resp.status_code == 503
        assert "No simulation snapshot available" in resp.json()["detail"]


@pytest.mark.asyncio
@respx.mock
async def test_forecasts_endpoint_success_with_snapshot(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    base = "http://test-sim:8000"

    station = Station(
        id="station-mirpur",
        name="Mirpur Fuel Station",
        region_id="region-dhaka",
        status=StationStatus.OPEN,
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity=FuelMap(DIESEL=15000, PETROL=14000, OCTANE=9000),
        inventory=FuelMap(DIESEL=9000, PETROL=9000, OCTANE=5000),
    )
    snapshot = _make_dummy_snapshot()
    snapshot.stations = [station]
    store.update(snapshot)

    # Mock demand history response
    respx.get(f"{base}/v1/demand-history").respond(
        json=[
            {
                "id": 1,
                "station_id": "station-mirpur",
                "fuel_type": "DIESEL",
                "tick": 0,
                "sim_time": "2026-01-01T00:00:00",
                "demand_liters": 80.0,
                "served_liters": 80.0,
                "unmet_liters": 0.0,
            }
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/forecasts")
        assert resp.status_code == 200
        data = resp.json()

        assert data["mode"] == OperationalMode.NORMAL.value
        assert "station-mirpur" in data["forecasts"]
        assert "DIESEL" in data["forecasts"]["station-mirpur"]
        assert data["total_fleet_demand_per_hour"] > 0.0
        assert data["active_anomalies_count"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_station_forecast_endpoint(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    base = "http://test-sim:8000"

    station = Station(
        id="station-tongi",
        name="Tongi Station",
        region_id="region-dhaka",
        status=StationStatus.OPEN,
        demand_profile="industrial",
        demand_multiplier=1.8,
        capacity=FuelMap(DIESEL=18000, PETROL=9000, OCTANE=6000),
        inventory=FuelMap(DIESEL=11000, PETROL=6000, OCTANE=3500),
    )
    snapshot = _make_dummy_snapshot()
    snapshot.stations = [station]
    store.update(snapshot)

    respx.get(f"{base}/v1/demand-history").respond(json=[])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Success for station-tongi
        resp = await client.get("/forecasts/station-tongi")
        assert resp.status_code == 200
        data = resp.json()
        assert data["station_id"] == "station-tongi"
        assert "DIESEL" in data["forecasts"]
        # Multiplier is 1.8 -> anomaly flagged
        assert len(data["anomalies"]) >= 1

        # 404 for unknown station
        resp_404 = await client.get("/forecasts/unknown-station")
        assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_risks_endpoint_503_when_no_snapshot(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/risks")
        assert resp.status_code == 503


@pytest.mark.asyncio
@respx.mock
async def test_risks_endpoint_success_with_snapshot(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    base = "http://test-sim:8000"

    station = Station(
        id="station-mirpur",
        name="Mirpur Fuel Station",
        region_id="region-dhaka",
        status=StationStatus.OPEN,
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity=FuelMap(DIESEL=15000, PETROL=14000, OCTANE=9000),
        inventory=FuelMap(DIESEL=9000, PETROL=9000, OCTANE=5000),
    )
    snapshot = _make_dummy_snapshot()
    snapshot.stations = [station]
    store.update(snapshot)

    respx.get(f"{base}/v1/demand-history").respond(json=[])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/risks")
        assert resp.status_code == 200
        data = resp.json()

        assert data["mode"] == OperationalMode.NORMAL.value
        assert "overall_severity" in data
        assert "station-mirpur" in data["station_risks"]
        assert "DIESEL" in data["station_risks"]["station-mirpur"]
        assert data["fleet_average_confidence"] > 0.0


@pytest.mark.asyncio
@respx.mock
async def test_station_risk_endpoint(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    base = "http://test-sim:8000"

    station = Station(
        id="station-tongi",
        name="Tongi Station",
        region_id="region-dhaka",
        status=StationStatus.OPEN,
        demand_profile="industrial",
        demand_multiplier=1.0,
        capacity=FuelMap(DIESEL=18000, PETROL=9000, OCTANE=6000),
        inventory=FuelMap(DIESEL=1500, PETROL=6000, OCTANE=3500),  # DIESEL is < 20% -> CRITICAL
    )
    snapshot = _make_dummy_snapshot()
    snapshot.stations = [station]
    store.update(snapshot)

    respx.get(f"{base}/v1/demand-history").respond(json=[])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/risks/station-tongi")
        assert resp.status_code == 200
        data = resp.json()

        assert data["station_id"] == "station-tongi"
        assert data["station_name"] == "Tongi Station"
        assert data["highest_severity"] == "CRITICAL"
        assert "DIESEL" in data["risks"]
        assert len(data["risks"]["DIESEL"]["trajectory"]) == 24
        assert len(data["alerts"]) >= 1

        # 404 for unknown station
        resp_404 = await client.get("/risks/unknown-station")
        assert resp_404.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_recommendations_generate_and_list_endpoints(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    base = "http://test-sim:8000"

    respx.get(f"{base}/v1/demand-history").respond(json=[])

    depot = Depot(
        id="depot-1",
        name="Central Depot",
        region_id="reg-1",
        status=DepotStatus.OPEN,
        capacity=FuelMap(DIESEL=50000, PETROL=30000, OCTANE=20000),
        inventory=FuelMap(DIESEL=30000, PETROL=20000, OCTANE=15000),
        dispatch_capacity_per_tick=20000.0,
    )
    station = Station(
        id="station-1",
        name="Mirpur Station",
        region_id="reg-1",
        status=StationStatus.OPEN,
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity=FuelMap(DIESEL=20000, PETROL=10000, OCTANE=8000),
        inventory=FuelMap(DIESEL=1200, PETROL=5000, OCTANE=4000),  # < 20% -> CRITICAL
    )
    route = Route(
        id="route-1",
        source_depot_id="depot-1",
        destination_station_id="station-1",
        transit_ticks=2,
        max_shipment=15000.0,
        status=RouteStatus.AVAILABLE,
    )

    snapshot = _make_dummy_snapshot()
    snapshot.depots = [depot]
    snapshot.stations = [station]
    snapshot.routes = [route]
    store.update(snapshot)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Initial empty list
        resp = await client.get("/recommendations")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

        # Generate recommendations
        gen_resp = await client.post("/recommendations/generate")
        assert gen_resp.status_code == 200
        gen_data = gen_resp.json()
        assert gen_data["total_count"] >= 1
        rec = gen_data["recommendations"][0]
        rec_id = rec["recommendation_id"]
        assert rec["station_id"] == "station-1"
        assert rec["fuel_type"] == "DIESEL"
        assert rec["status"] == "PROPOSED"

        # Fetch single recommendation by ID
        get_resp = await client.get(f"/recommendations/{rec_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["recommendation"]["recommendation_id"] == rec_id

        # 404 for unknown recommendation ID
        resp_404 = await client.get("/recommendations/rec_nonexistent")
        assert resp_404.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_recommendation_approve_and_reject_endpoints(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    base = "http://test-sim:8000"

    respx.get(f"{base}/v1/demand-history").respond(json=[])

    depot = Depot(
        id="depot-1",
        name="Central Depot",
        region_id="reg-1",
        status=DepotStatus.OPEN,
        capacity=FuelMap(DIESEL=50000, PETROL=30000, OCTANE=20000),
        inventory=FuelMap(DIESEL=30000, PETROL=20000, OCTANE=15000),
        dispatch_capacity_per_tick=20000.0,
    )
    station = Station(
        id="station-1",
        name="Mirpur Station",
        region_id="reg-1",
        status=StationStatus.OPEN,
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity=FuelMap(DIESEL=20000, PETROL=10000, OCTANE=8000),
        inventory=FuelMap(DIESEL=1200, PETROL=5000, OCTANE=4000),
    )
    route = Route(
        id="route-1",
        source_depot_id="depot-1",
        destination_station_id="station-1",
        transit_ticks=2,
        max_shipment=15000.0,
        status=RouteStatus.AVAILABLE,
    )
    snapshot = _make_dummy_snapshot()
    snapshot.depots = [depot]
    snapshot.stations = [station]
    snapshot.routes = [route]
    store.update(snapshot)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Generate recommendation
        gen_resp = await client.post("/recommendations/generate")
        recs = gen_resp.json()["recommendations"]
        rec_id = recs[0]["recommendation_id"]

        # Mock simulator allocation creation success
        respx.post(f"{base}/v1/allocations").respond(
            status_code=201,
            json={
                "id": 101,
                "idempotency_key": recs[0]["idempotency_key"],
                "source_depot_id": "depot-1",
                "destination_station_id": "station-1",
                "route_id": "route-1",
                "fuel_type": "DIESEL",
                "quantity": recs[0]["quantity_liters"],
                "created_tick": 15,
                "departure_tick": 16,
                "expected_arrival_tick": 18,
                "status": "PENDING",
            },
        )

        # Approve recommendation
        app_resp = await client.post(
            f"/recommendations/{rec_id}/approve",
            json={"operator_notes": "Approved for dispatch"},
        )
        assert app_resp.status_code == 200
        rec_approved = app_resp.json()["recommendation"]
        assert rec_approved["status"] == "SUBMITTED"
        assert rec_approved["simulator_allocation_id"] == 101

        # Reject should now fail with 400 because already submitted
        rej_resp = await client.post(
            f"/recommendations/{rec_id}/reject",
            json={"operator_notes": "Trying to reject submitted"},
        )
        assert rej_resp.status_code == 400


@pytest.mark.asyncio
async def test_decisions_audit_and_stats_endpoints(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Audit history endpoint
        audit_resp = await client.get("/decisions/audit")
        assert audit_resp.status_code == 200
        assert "events" in audit_resp.json()

        # Audit stats endpoint
        stats_resp = await client.get("/decisions/stats")
        assert stats_resp.status_code == 200
        stats = stats_resp.json()["stats"]
        assert "total_recommendations" in stats
        assert "rejection_rate" in stats


@pytest.mark.asyncio
async def test_degraded_cached_on_malformed_snapshot_rejection(
    test_setup: dict[str, Any],
) -> None:
    app = test_setup["app"]
    store = test_setup["store"]

    # Ingest a valid snapshot first
    valid_snap = _make_dummy_snapshot()
    store.update(valid_snap)

    # Trigger malformed snapshot rejection
    store.record_malformed_rejection(
        errors=["Negative fuel volume in STA-99"],
        tick=99,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Check /health
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        assert health_data["mode"] == "DEGRADED_CACHED"
        assert len(health_data["alerts"]) >= 1
        assert "Negative fuel volume" in health_data["alerts"][0]

        # Check /state
        state_resp = await client.get("/state")
        assert state_resp.status_code == 200
        state_data = state_resp.json()
        assert state_data["mode"] == "DEGRADED_CACHED"
        assert len(state_data["alerts"]) >= 1
        # Snapshot returned is the last known good snapshot
        assert state_data["snapshot"] is not None
        assert state_data["snapshot"]["tick"] == valid_snap.tick


@pytest.mark.asyncio
async def test_degraded_fallback_generates_fallback_recommendations_with_human_review(
    test_setup: dict[str, Any],
) -> None:
    app = test_setup["app"]
    store = test_setup["store"]
    resilience_mgr = app.state.resilience_manager

    # Build network where station tank is at 10% (< 20%)
    depot = Depot(
        id="depot-emergency",
        name="Emergency Depot",
        region_id="reg-1",
        status=DepotStatus.OPEN,
        capacity=FuelMap(DIESEL=50000, PETROL=30000, OCTANE=20000),
        inventory=FuelMap(DIESEL=30000, PETROL=20000, OCTANE=15000),
        dispatch_capacity_per_tick=20000.0,
    )
    station = Station(
        id="station-emergency",
        name="Critical Highway Station",
        region_id="reg-1",
        status=StationStatus.OPEN,
        demand_profile="highway",
        demand_multiplier=1.0,
        capacity=FuelMap(DIESEL=20000, PETROL=10000, OCTANE=8000),
        inventory=FuelMap(DIESEL=1500, PETROL=5000, OCTANE=4000),  # 7.5% fill -> CRITICAL
    )
    route = Route(
        id="route-emergency",
        source_depot_id="depot-emergency",
        destination_station_id="station-emergency",
        transit_ticks=2,
        max_shipment=10000.0,
        status=RouteStatus.AVAILABLE,
    )

    snapshot = _make_dummy_snapshot()
    snapshot.depots = [depot]
    snapshot.stations = [station]
    snapshot.routes = [route]
    store.update(snapshot)

    # Set forecaster failure in resilience manager
    resilience_mgr.record_forecaster_failure("Test simulated intelligence model crash")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Check /health reports DEGRADED_FALLBACK
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["mode"] == "DEGRADED_FALLBACK"

        # Generate recommendations under fallback mode
        gen_resp = await client.post("/recommendations/generate")
        assert gen_resp.status_code == 200
        data = gen_resp.json()
        assert data["mode"] == "DEGRADED_FALLBACK"
        assert data["total_count"] >= 1

        rec = data["recommendations"][0]
        assert rec["needs_human_review"] is True
        assert rec["confidence_score"] == 0.25
        assert rec["confidence_level"] == "LOW"
        assert "FALLBACK HEURISTIC" in rec["explanation"]
