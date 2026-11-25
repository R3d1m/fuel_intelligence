"""Unit and integration tests for the offline evaluation harness (tools/evaluate.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app.domain.models import (
    DemandHistoryRecord,
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

from tools.evaluate import (
    EvaluationHarness,
    EvaluationResult,
    FuelIntelligencePolicy,
    NaivePolicy,
    NoActionPolicy,
    build_policies,
    build_scenario_events,
    build_scenarios,
    compute_station_hours_unmet,
    format_markdown_table,
)


def _make_snapshot(
    tick: int = 0,
    station_diesel: float = 2000.0,
    route_1_status: RouteStatus = RouteStatus.AVAILABLE,
    station_status: StationStatus = StationStatus.OPEN,
) -> SimulatorSnapshot:
    return SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0",
            seed=42,
            sim_time="2026-01-01T00:00:00",
            tick=tick,
            tick_minutes=15,
            status="PAUSED",
        ),
        depots=[
            Depot(
                id="depot-1",
                name="Primary Depot",
                region_id="reg-1",
                status=DepotStatus.OPEN,
                capacity=FuelMap(DIESEL=100000, PETROL=50000, OCTANE=50000),
                inventory=FuelMap(DIESEL=80000, PETROL=40000, OCTANE=40000),
                dispatch_capacity_per_tick=30000.0,
            ),
            Depot(
                id="depot-2",
                name="Far Depot",
                region_id="reg-1",
                status=DepotStatus.OPEN,
                capacity=FuelMap(DIESEL=100000, PETROL=50000, OCTANE=50000),
                inventory=FuelMap(DIESEL=80000, PETROL=40000, OCTANE=40000),
                dispatch_capacity_per_tick=30000.0,
            ),
        ],
        stations=[
            Station(
                id="station-mirpur",
                name="Mirpur Station",
                region_id="reg-1",
                status=station_status,
                demand_profile="urban_high",
                demand_multiplier=1.0,
                capacity=FuelMap(DIESEL=15000, PETROL=10000, OCTANE=10000),
                inventory=FuelMap(DIESEL=station_diesel, PETROL=8000, OCTANE=8000),
            ),
        ],
        routes=[
            Route(
                id="route-gazipur-mirpur",
                source_depot_id="depot-1",
                destination_station_id="station-mirpur",
                transit_ticks=2,
                max_shipment=5000.0,
                status=route_1_status,
            ),
            Route(
                id="route-far-mirpur",
                source_depot_id="depot-2",
                destination_station_id="station-mirpur",
                transit_ticks=4,
                max_shipment=5000.0,
                status=RouteStatus.AVAILABLE,
            ),
        ],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=tick,
        sim_time="2026-01-01T00:00:00",
        fetched_at=datetime.now(UTC),
    )


def test_no_action_policy_returns_empty() -> None:
    snap = _make_snapshot(station_diesel=1000.0)
    policy = NoActionPolicy()
    assert policy.decide(snap, [], "normal_day") == []


def test_naive_policy_picks_nearest_available_depot_below_30_percent() -> None:
    # 2000 / 15000 = 13.3% < 30% -> should trigger refill from depot-1 (transit_ticks=2)
    snap = _make_snapshot(station_diesel=2000.0)
    policy = NaivePolicy(threshold_ratio=0.30)
    allocs = policy.decide(snap, [], "normal_day")
    assert len(allocs) == 1
    assert allocs[0].source_depot_id == "depot-1"
    assert allocs[0].route_id == "route-gazipur-mirpur"
    assert allocs[0].fuel_type == FuelType.DIESEL
    assert allocs[0].quantity == 5000.0


def test_naive_policy_skips_above_threshold_and_reroutes_around_disruption() -> None:
    # Above 30% -> no dispatch
    snap_ok = _make_snapshot(station_diesel=6000.0)
    assert NaivePolicy().decide(snap_ok, [], "normal_day") == []

    # Disrupted primary route -> falls back to depot-2
    snap_disrupted = _make_snapshot(
        station_diesel=2000.0,
        route_1_status=RouteStatus.DISRUPTED,
    )
    allocs = NaivePolicy().decide(snap_disrupted, [], "route_disruption")
    assert len(allocs) == 1
    assert allocs[0].source_depot_id == "depot-2"
    assert allocs[0].route_id == "route-far-mirpur"


def test_fuel_intelligence_policy_generates_allocations_for_critical_tank() -> None:
    snap = _make_snapshot(station_diesel=1200.0)
    policy = FuelIntelligencePolicy()
    allocs = policy.decide(snap, [], "demand_spike")
    assert len(allocs) >= 1
    assert allocs[0].destination_station_id == "station-mirpur"
    assert allocs[0].fuel_type == FuelType.DIESEL
    assert allocs[0].quantity > 0.0


def test_build_scenario_events_covers_all_five_scenarios() -> None:
    snap = _make_snapshot()
    assert build_scenario_events("normal_day", snap) == []
    assert [e.event_type for e in build_scenario_events("demand_spike", snap)] == ["demand_spike"]
    assert [e.event_type for e in build_scenario_events("route_disruption", snap)] == [
        "route_disruption"
    ]
    assert [e.event_type for e in build_scenario_events("shipment_delay", snap)] == [
        "shipment_delay"
    ]
    assert [e.event_type for e in build_scenario_events("combined_crisis", snap)] == [
        "demand_spike",
        "route_disruption",
        "shipment_delay",
    ]
    with pytest.raises(ValueError, match="Unknown scenario_id"):
        build_scenario_events("invalid_scenario", snap)


def test_compute_station_hours_unmet_deduplicates_station_ticks() -> None:
    records = [
        DemandHistoryRecord(
            id=1,
            station_id="station-mirpur",
            fuel_type=FuelType.DIESEL,
            tick=5,
            sim_time="2026-01-01T01:15:00",
            demand_liters=100.0,
            served_liters=60.0,
            unmet_liters=40.0,
        ),
        DemandHistoryRecord(
            id=2,
            station_id="station-mirpur",
            fuel_type=FuelType.PETROL,
            tick=5,
            sim_time="2026-01-01T01:15:00",
            demand_liters=50.0,
            served_liters=40.0,
            unmet_liters=10.0,
        ),
        DemandHistoryRecord(
            id=3,
            station_id="station-mirpur",
            fuel_type=FuelType.DIESEL,
            tick=6,
            sim_time="2026-01-01T01:30:00",
            demand_liters=100.0,
            served_liters=100.0,
            unmet_liters=0.0,
        ),
    ]
    # 1 unique (station, tick) with unmet > 0 * (15 / 60) = 0.25 hours
    assert compute_station_hours_unmet(records, tick_minutes=15) == 0.25


def test_format_markdown_table() -> None:
    table = format_markdown_table(
        [
            EvaluationResult(
                scenario_id="normal_day",
                scenario_name="Normal Day",
                policy_id="fuel_intelligence",
                policy_name="Fuel Intelligence (Ours)",
                seed=42,
                ticks_executed=96,
                service_level_pct=99.5,
                unmet_demand_liters=120.0,
                allocation_failures=1,
                station_hours_unmet=0.5,
                allocations_submitted=10,
                submission_errors=1,
            )
        ]
    )
    assert "| Normal Day | Fuel Intelligence (Ours) | 99.50% | 120.0 | 2 | 0.50 |" in table


class _MockSimulator:
    """Minimal stateful mock of the BUP Fuel Supply Simulator for harness tests."""

    def __init__(self) -> None:
        self.tick = 0
        self.injected_events: list[dict[str, Any]] = []
        self.allocations_created: list[dict[str, Any]] = []
        self.station_diesel = 2000.0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "POST" and path == "/admin/faults/clear":
            return httpx.Response(200, json={"cleared": True})
        if method == "POST" and path == "/admin/reset":
            self.tick = 0
            self.injected_events.clear()
            self.allocations_created.clear()
            self.station_diesel = 2000.0
            return httpx.Response(200, json={"reset": True})
        if method == "POST" and path == "/admin/pause":
            return httpx.Response(200, json={"status": "PAUSED"})
        if method == "POST" and path == "/admin/events":
            self.injected_events.append(json.loads(request.content.decode()))
            return httpx.Response(201, json={"created": True})
        if method == "POST" and path == "/admin/step":
            self.tick += 1
            self.station_diesel = max(0.0, self.station_diesel - 100.0)
            return httpx.Response(200, json={"tick": self.tick})

        snap = _make_snapshot(tick=self.tick, station_diesel=self.station_diesel)
        if method == "GET" and path == "/v1/instance":
            return httpx.Response(200, json=snap.instance.model_dump(mode="json"))
        if method == "GET" and path == "/v1/depots":
            return httpx.Response(200, json=[d.model_dump(mode="json") for d in snap.depots])
        if method == "GET" and path == "/v1/stations":
            return httpx.Response(200, json=[s.model_dump(mode="json") for s in snap.stations])
        if method == "GET" and path == "/v1/routes":
            return httpx.Response(200, json=[r.model_dump(mode="json") for r in snap.routes])
        if method == "GET" and path == "/v1/supply-arrivals":
            return httpx.Response(200, json=[])
        if method == "GET" and path == "/v1/events":
            return httpx.Response(200, json=[])
        if method == "GET" and path == "/v1/allocations":
            return httpx.Response(200, json=[])
        if method == "POST" and path == "/v1/allocations":
            payload = json.loads(request.content.decode())
            self.allocations_created.append(payload)
            self.station_diesel = min(15000.0, self.station_diesel + payload["quantity"])
            return httpx.Response(
                201,
                json={
                    "id": len(self.allocations_created),
                    "idempotency_key": payload["idempotency_key"],
                    "source_depot_id": payload["source_depot_id"],
                    "destination_station_id": payload["destination_station_id"],
                    "route_id": payload["route_id"],
                    "fuel_type": payload["fuel_type"],
                    "quantity": payload["quantity"],
                    "status": "PENDING",
                    "created_tick": self.tick,
                },
            )
        if method == "GET" and path == "/v1/demand-history":
            records = [
                {
                    "id": t,
                    "station_id": "station-mirpur",
                    "fuel_type": "DIESEL",
                    "tick": t,
                    "sim_time": "2026-01-01T00:15:00",
                    "demand_liters": 100.0,
                    "served_liters": 100.0 if self.allocations_created else 0.0,
                    "unmet_liters": 0.0 if self.allocations_created else 100.0,
                }
                for t in range(1, self.tick + 1)
            ]
            return httpx.Response(200, json=records)
        if method == "GET" and path == "/v1/metrics":
            unmet = 0.0 if self.allocations_created else 300.0
            served = 300.0 if self.allocations_created else 0.0
            return httpx.Response(
                200,
                json={
                    "served_demand_liters": served,
                    "unmet_demand_liters": unmet,
                    "service_level": 1.0 if self.allocations_created else 0.0,
                    "allocation_liters": sum(a["quantity"] for a in self.allocations_created),
                    "allocation_failures": 0,
                },
            )

        return httpx.Response(404, json={"detail": f"Unhandled path {path}"})


async def test_evaluation_harness_runs_all_policies_against_mock_simulator() -> None:
    sim = _MockSimulator()
    transport = httpx.MockTransport(sim.handle)
    async with httpx.AsyncClient(base_url="http://mock-sim", transport=transport) as client:
        harness = EvaluationHarness(simulator_url="http://mock-sim", http_client=client)
        results = await harness.run_suite(
            scenarios=build_scenarios(["normal_day", "demand_spike"]),
            policies=build_policies(["no_action", "naive", "fuel_intelligence"]),
            ticks_override=3,
        )

    assert len(results) == 6
    by_key = {(r.scenario_id, r.policy_id): r for r in results}
    assert by_key[("normal_day", "no_action")].service_level_pct == 0.0
    assert by_key[("normal_day", "no_action")].station_hours_unmet == 0.75
    assert by_key[("normal_day", "naive")].allocations_submitted >= 1
    assert by_key[("demand_spike", "fuel_intelligence")].allocations_submitted >= 1
    assert by_key[("demand_spike", "fuel_intelligence")].service_level_pct == 100.0
