"""Unit tests for typed domain models and enums."""

from datetime import datetime

import pytest
from app.domain.models import (
    Allocation,
    AllocationCreate,
    AllocationStatus,
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
    SupplyArrival,
    SupplyArrivalStatus,
)
from pydantic import ValidationError


def test_fuel_type_enum():
    """Verify FuelType enum members and string equivalence."""
    assert FuelType.DIESEL == "DIESEL"
    assert FuelType.PETROL == "PETROL"
    assert FuelType.OCTANE == "OCTANE"
    assert len(FuelType) == 3


def test_fuel_map_operations():
    """Verify FuelMap access, calculation methods, and conversions."""
    fm = FuelMap(DIESEL=1000.0, PETROL=500.0, OCTANE=250.0)

    # Attribute access
    assert fm.DIESEL == 1000.0
    assert fm.PETROL == 500.0
    assert fm.OCTANE == 250.0

    # Dict-like and FuelType indexing
    assert fm["DIESEL"] == 1000.0
    assert fm[FuelType.PETROL] == 500.0
    assert fm.get("OCTANE") == 250.0
    assert fm.get("UNKNOWN", 42.0) == 42.0

    # Aggregation
    assert fm.total() == 1750.0

    # Conversion
    d = fm.as_dict()
    assert d[FuelType.DIESEL] == 1000.0


def test_depot_model():
    """Verify Depot model validation and calculation helpers."""
    data = {
        "id": "depot-gazipur",
        "name": "Gazipur Depot",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "dispatch_capacity_per_tick": 12000.0,
        "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
        "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
    }
    depot = Depot.model_validate(data)
    assert depot.id == "depot-gazipur"
    assert depot.status == DepotStatus.OPEN
    assert depot.free_capacity(FuelType.DIESEL) == 30000.0
    assert depot.available_to_dispatch(FuelType.DIESEL, reserve=10000.0) == 50000.0
    assert depot.available_to_dispatch(FuelType.DIESEL, reserve=70000.0) == 0.0


def test_station_model_and_effective_ullage():
    """Verify Station model and critical effective free capacity accounting."""
    data = {
        "id": "station-mirpur",
        "name": "Mirpur Fuel Station",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "demand_profile": "urban_high",
        "demand_multiplier": 1.25,
        "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
        "inventory": {"DIESEL": 9000, "PETROL": 9000, "OCTANE": 5000},
    }
    station = Station.model_validate(data)
    assert station.id == "station-mirpur"
    assert station.status == StationStatus.OPEN

    # Static free capacity: 15,000 - 9,000 = 6,000
    assert station.free_capacity(FuelType.DIESEL) == 6000.0

    # Effective free capacity with 4,000L in transit: 15,000 - (9,000 + 4,000) = 2,000
    assert station.effective_free_capacity(FuelType.DIESEL, in_transit_liters=4000.0) == 2000.0

    # Effective free capacity when in-transit exceeds ullage clips to 0.0
    assert station.effective_free_capacity(FuelType.DIESEL, in_transit_liters=7000.0) == 0.0


def test_route_model():
    """Verify Route validation."""
    data = {
        "id": "route-gazipur-mirpur",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "transit_ticks": 2,
        "max_shipment": 7000.0,
        "status": "AVAILABLE",
    }
    route = Route.model_validate(data)
    assert route.id == "route-gazipur-mirpur"
    assert route.status == RouteStatus.AVAILABLE
    assert route.transit_ticks == 2


def test_supply_arrival_model():
    """Verify SupplyArrival validation."""
    data = {
        "id": "supply-001",
        "depot_id": "depot-gazipur",
        "fuel_type": "DIESEL",
        "quantity": 18000.0,
        "planned_tick": 12,
        "actual_tick": None,
        "status": "SCHEDULED",
    }
    arrival = SupplyArrival.model_validate(data)
    assert arrival.fuel_type == FuelType.DIESEL
    assert arrival.status == SupplyArrivalStatus.SCHEDULED


def test_allocation_and_creation():
    """Verify Allocation and AllocationCreate validation constraints."""
    valid_create = AllocationCreate(
        idempotency_key="key-12345",
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type=FuelType.DIESEL,
        quantity=3000.0,
    )
    assert valid_create.quantity == 3000.0

    # Non-positive quantity should fail
    with pytest.raises(ValidationError):
        AllocationCreate(
            idempotency_key="key-12345",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            route_id="route-gazipur-mirpur",
            fuel_type=FuelType.DIESEL,
            quantity=0.0,
        )

    # Empty idempotency key should fail
    with pytest.raises(ValidationError):
        AllocationCreate(
            idempotency_key="",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            route_id="route-gazipur-mirpur",
            fuel_type=FuelType.DIESEL,
            quantity=100.0,
        )

    # Valid Allocation response
    alloc = Allocation.model_validate(
        {
            "id": 1,
            "idempotency_key": "key-12345",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 3000.0,
            "created_tick": 0,
            "departure_tick": None,
            "expected_arrival_tick": None,
            "actual_arrival_tick": None,
            "status": "PENDING",
            "failure_reason": None,
        }
    )
    assert alloc.status == AllocationStatus.PENDING


def test_demand_history_record():
    """Verify DemandHistoryRecord validation."""
    data = {
        "id": 1,
        "station_id": "station-mirpur",
        "fuel_type": "DIESEL",
        "tick": 5,
        "sim_time": "2026-01-01T01:15:00",
        "demand_liters": 88.5,
        "served_liters": 88.5,
        "unmet_liters": 0.0,
    }
    rec = DemandHistoryRecord.model_validate(data)
    assert rec.demand_liters == 88.5
    assert rec.fuel_type == FuelType.DIESEL


def test_simulator_snapshot():
    """Verify consolidated SimulatorSnapshot construction."""
    inst = InstanceInfo(
        id=1,
        scenario_id="baseline",
        scenario_version="1.0",
        seed=12345,
        sim_time="2026-01-01T00:00:00",
        tick=0,
        tick_minutes=15,
        status="PAUSED",
    )
    snapshot = SimulatorSnapshot(
        instance=inst,
        depots=[],
        stations=[],
        routes=[],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=0,
        sim_time="2026-01-01T00:00:00",
        fetched_at=datetime.now(),
        is_stale=False,
    )
    assert snapshot.tick == 0
    assert not snapshot.is_stale
