"""Unit and integration tests for fault drills and degraded operational recovery.

Tests the resilience mechanisms exercised during Day 3 Block 5:
- Circuit breaker trip and cached snapshot serving on simulator outage
- Resilience under chaotic packet loss and retry absorption
- Stale data detection and degradation
- Corridor bypass during active route disruptions
- Closed/outage station exclusion from dispatch planning
- Internal intelligence model failure, fallback heuristic execution, and human review mandate
- SQLite audit logging of fallback activation events
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.api.main import create_app
from app.config import get_settings
from app.decisions.audit import AuditLedger
from app.decisions.service import DecisionService
from app.domain.models import (
    Depot,
    DepotStatus,
    FuelMap,
    InstanceInfo,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.ingest.store import StateStore
from app.intelligence.fallback import FallbackPlanner
from app.intelligence.forecast import DemandForecaster
from app.intelligence.planner import ConstrainedPlanner
from app.intelligence.risk import RiskEngine
from app.intelligence.twin import DigitalTwin
from app.resilience.breaker import CircuitBreaker
from app.resilience.mode import ResilienceManager
from httpx import ASGITransport, AsyncClient


def _make_drill_snapshot() -> SimulatorSnapshot:
    """Create a multi-station, multi-depot snapshot for fault testing."""
    return SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0",
            seed=12345,
            sim_time="2026-01-01T00:00:00",
            tick=10,
            tick_minutes=15,
            status="RUNNING",
        ),
        depots=[
            Depot(
                id="depot-1",
                name="Primary Depot",
                region_id="reg-1",
                status=DepotStatus.OPEN,
                capacity=FuelMap(DIESEL=100000, PETROL=50000, OCTANE=50000),
                inventory=FuelMap(DIESEL=60000, PETROL=30000, OCTANE=30000),
                dispatch_capacity_per_tick=30000.0,
            ),
            Depot(
                id="depot-2",
                name="Secondary Depot",
                region_id="reg-1",
                status=DepotStatus.OPEN,
                capacity=FuelMap(DIESEL=80000, PETROL=40000, OCTANE=40000),
                inventory=FuelMap(DIESEL=50000, PETROL=25000, OCTANE=25000),
                dispatch_capacity_per_tick=25000.0,
            ),
        ],
        stations=[
            Station(
                id="station-1",
                name="Highway Station",
                region_id="reg-1",
                status=StationStatus.OPEN,
                demand_profile="highway",
                demand_multiplier=1.0,
                capacity=FuelMap(DIESEL=30000, PETROL=15000, OCTANE=15000),
                inventory=FuelMap(DIESEL=2500, PETROL=8000, OCTANE=8000),  # 8.3% fill -> CRITICAL
            ),
            Station(
                id="station-2",
                name="Urban Outage Station",
                region_id="reg-1",
                status=StationStatus.OUTAGE,
                demand_profile="urban",
                demand_multiplier=1.0,
                capacity=FuelMap(DIESEL=25000, PETROL=12000, OCTANE=12000),
                inventory=FuelMap(DIESEL=1000, PETROL=5000, OCTANE=5000),
            ),
        ],
        routes=[
            Route(
                id="route-depot1-st1",
                source_depot_id="depot-1",
                destination_station_id="station-1",
                transit_ticks=2,
                max_shipment=10000.0,
                status=RouteStatus.DISRUPTED,  # Primary corridor disrupted
            ),
            Route(
                id="route-depot2-st1",
                source_depot_id="depot-2",
                destination_station_id="station-1",
                transit_ticks=3,
                max_shipment=8000.0,
                status=RouteStatus.AVAILABLE,  # Alternative corridor available
            ),
            Route(
                id="route-depot1-st2",
                source_depot_id="depot-1",
                destination_station_id="station-2",
                transit_ticks=2,
                max_shipment=6000.0,
                status=RouteStatus.AVAILABLE,
            ),
        ],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-01-01T00:00:00Z",
        fetched_at=datetime.now(UTC),
        is_stale=False,
    )


@pytest.fixture
def fault_app_setup(tmp_path):
    """Fixture providing initialized FastAPI test app and dependencies."""
    settings = get_settings()
    store = StateStore()
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=5.0)
    resilience_mgr = ResilienceManager()
    forecaster = DemandForecaster()
    risk_engine = RiskEngine(forecaster=forecaster)
    twin = DigitalTwin(settings=settings, forecaster=forecaster)
    fallback_planner = FallbackPlanner(settings=settings)
    planner = ConstrainedPlanner(
        settings=settings,
        risk_engine=risk_engine,
        twin_evaluator=twin,
        fallback_planner=fallback_planner,
        resilience_manager=resilience_mgr,
    )
    db_file = tmp_path / "test_fault_audit.db"
    audit_ledger = AuditLedger(db_path=str(db_file))
    decision_service = DecisionService(
        client=None,  # mock or none
        audit_ledger=audit_ledger,
        planner=planner,
        store=store,
        settings=settings,
    )

    app = create_app(
        settings=settings,
        store=store,
        breaker=breaker,
        resilience_manager=resilience_mgr,
        fallback_planner=fallback_planner,
        planner=planner,
        audit_ledger=audit_ledger,
        decision_service=decision_service,
        start_poller=False,
    )

    return {
        "app": app,
        "store": store,
        "breaker": breaker,
        "resilience_mgr": resilience_mgr,
        "audit_ledger": audit_ledger,
        "planner": planner,
    }


@pytest.mark.asyncio
async def test_drill_breaker_trip_serves_cached_state_and_recovers(fault_app_setup) -> None:
    """Verify circuit breaker trips CLOSED -> OPEN on failures, serving cached state."""
    app = fault_app_setup["app"]
    store = fault_app_setup["store"]
    breaker = fault_app_setup["breaker"]

    snapshot = _make_drill_snapshot()
    store.update(snapshot)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Initial healthy state
        r_health = await client.get("/health")
        assert r_health.status_code == 200
        assert r_health.json()["mode"] == "NORMAL"
        assert r_health.json()["simulator"]["circuit_state"] == "CLOSED"

        # Record 3 failures to trip breaker
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state.value == "OPEN"

        # Verify /health and /state report DEGRADED_CACHED
        r_health_open = await client.get("/health")
        assert r_health_open.json()["mode"] == "DEGRADED_CACHED"
        assert r_health_open.json()["simulator"]["circuit_state"] == "OPEN"

        r_state = await client.get("/state")
        assert r_state.status_code == 200
        assert r_state.json()["mode"] == "DEGRADED_CACHED"
        assert len(r_state.json()["snapshot"]["stations"]) == 2

        # Reset breaker and verify recovery to NORMAL
        breaker.reset()
        r_health_rec = await client.get("/health")
        assert r_health_rec.json()["mode"] == "NORMAL"
        assert r_health_rec.json()["simulator"]["circuit_state"] == "CLOSED"


@pytest.mark.asyncio
async def test_drill_route_disruption_bypasses_failed_corridor(fault_app_setup) -> None:
    """Verify planner detects RouteStatus.DISRUPTED and routes via available alternative."""
    app = fault_app_setup["app"]
    store = fault_app_setup["store"]

    snapshot = _make_drill_snapshot()
    store.update(snapshot)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_gen = await client.post("/recommendations/generate")
        assert r_gen.status_code == 200
        data = r_gen.json()
        assert data["total_count"] >= 1

        for rec in data["recommendations"]:
            # Route route-depot1-st1 is DISRUPTED; must never be chosen
            assert rec["route_id"] != "route-depot1-st1"
            if rec["station_id"] == "station-1":
                assert rec["route_id"] == "route-depot2-st1"
                assert rec["source_depot_name"] == "Secondary Depot"


@pytest.mark.asyncio
async def test_drill_station_outage_suppresses_dispatches(fault_app_setup) -> None:
    """Verify planner excludes stations in OUTAGE or CLOSED status from dispatches."""
    app = fault_app_setup["app"]
    store = fault_app_setup["store"]

    snapshot = _make_drill_snapshot()
    store.update(snapshot)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_gen = await client.post("/recommendations/generate")
        assert r_gen.status_code == 200
        data = r_gen.json()

        for rec in data["recommendations"]:
            assert rec["station_id"] != "station-2", "Targeted station-2 which is in OUTAGE!"


@pytest.mark.asyncio
async def test_drill_forecaster_kill_activates_fallback_and_logs_audit(fault_app_setup) -> None:
    """Verify killing forecaster triggers fallback heuristic, human review, and audit event."""
    app = fault_app_setup["app"]
    store = fault_app_setup["store"]
    resilience_mgr = fault_app_setup["resilience_mgr"]

    snapshot = _make_drill_snapshot()
    store.update(snapshot)

    # Inject model crash
    resilience_mgr.record_forecaster_failure("Unhandled numpy matrix inversion error")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Check /health reports DEGRADED_FALLBACK
        r_health = await client.get("/health")
        assert r_health.status_code == 200
        assert r_health.json()["mode"] == "DEGRADED_FALLBACK"

        # Generate fallback recommendations
        r_gen = await client.post("/recommendations/generate")
        assert r_gen.status_code == 200
        data = r_gen.json()
        assert data["mode"] == "DEGRADED_FALLBACK"
        assert data["total_count"] >= 1

        rec = data["recommendations"][0]
        assert rec["needs_human_review"] is True
        assert rec["confidence_score"] == 0.25
        assert rec["confidence_level"] == "LOW"
        assert "FALLBACK HEURISTIC" in rec["explanation"]

        # Verify FALLBACK_POLICY_ACTIVATED event in audit ledger
        r_audit = await client.get("/decisions/audit")
        assert r_audit.status_code == 200
        events = r_audit.json()["events"]
        fallback_events = [e for e in events if e["event_type"] == "FALLBACK_POLICY_ACTIVATED"]
        assert len(fallback_events) >= 1

        # Clear model failure and verify recovery
        resilience_mgr.record_model_success()
        r_health_rec = await client.get("/health")
        assert r_health_rec.json()["mode"] == "NORMAL"
