"""Unit tests for snapshot domain validation and malformed data rejection."""

from datetime import UTC, datetime

from app.domain.models import (
    Depot,
    DepotStatus,
    FuelType,
    InstanceInfo,
    OperationalMode,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.domain.validation import validate_snapshot
from app.ingest.store import StateStore
from app.resilience.mode import diagnose_operational_mode


def _make_valid_snapshot(tick: int = 10, is_stale: bool = False) -> SimulatorSnapshot:
    """Helper to build a valid complete snapshot."""
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
        depots=[
            Depot(
                id="DEP-01",
                name="Central Depot",
                region_id="central",
                capacity={FuelType.DIESEL: 100000.0, FuelType.OCTANE: 80000.0},
                inventory={FuelType.DIESEL: 50000.0, FuelType.OCTANE: 40000.0},
                dispatch_capacity_per_tick=15000.0,
                status=DepotStatus.OPEN,
            )
        ],
        stations=[
            Station(
                id="STA-01",
                name="Downtown Station",
                region_id="central",
                demand_profile="urban",
                capacity={FuelType.DIESEL: 20000.0, FuelType.OCTANE: 15000.0},
                inventory={FuelType.DIESEL: 2500.0, FuelType.OCTANE: 8000.0},
                demand_multiplier=1.0,
                status=StationStatus.OPEN,
            )
        ],
        routes=[
            Route(
                id="RT-01",
                source_depot_id="DEP-01",
                destination_station_id="STA-01",
                transit_ticks=2,
                max_shipment=9000.0,
                status=RouteStatus.AVAILABLE,
            )
        ],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=tick,
        sim_time="2026-11-22T10:00:00Z",
        fetched_at=datetime.now(UTC),
        is_stale=is_stale,
    )


def test_validate_snapshot_valid() -> None:
    snapshot = _make_valid_snapshot(tick=5)
    errors = validate_snapshot(snapshot)
    assert errors == []


def test_validate_snapshot_negative_instance_tick() -> None:
    snapshot = _make_valid_snapshot()
    snapshot.instance.tick = -1
    errors = validate_snapshot(snapshot)
    assert any("Instance tick cannot be negative" in e for e in errors)


def test_validate_snapshot_negative_station_inventory() -> None:
    snapshot = _make_valid_snapshot()
    snapshot.stations[0].inventory[FuelType.DIESEL] = -150.0
    errors = validate_snapshot(snapshot)
    assert any("negative inventory" in e for e in errors)


def test_validate_snapshot_non_positive_station_capacity() -> None:
    snapshot = _make_valid_snapshot()
    snapshot.stations[0].capacity[FuelType.DIESEL] = -10.0
    errors = validate_snapshot(snapshot)
    assert any("negative capacity" in e for e in errors)


def test_validate_snapshot_negative_depot_inventory_and_dispatch() -> None:
    snapshot = _make_valid_snapshot()
    snapshot.depots[0].inventory[FuelType.OCTANE] = -20.0
    snapshot.depots[0].dispatch_capacity_per_tick = -500.0
    errors = validate_snapshot(snapshot)
    assert any("negative inventory" in e for e in errors)
    assert any("negative dispatch_capacity_per_tick" in e for e in errors)


def test_validate_snapshot_non_positive_route_transit_or_max_shipment() -> None:
    snapshot = _make_valid_snapshot()
    snapshot.routes[0].transit_ticks = 0
    snapshot.routes[0].max_shipment = -100.0
    errors = validate_snapshot(snapshot)
    assert any("non-positive transit_ticks" in e for e in errors)
    assert any("non-positive max_shipment" in e for e in errors)


def test_validate_snapshot_unknown_route_endpoints() -> None:
    snapshot = _make_valid_snapshot()
    snapshot.routes[0].source_depot_id = "UNKNOWN-DEPOT"
    snapshot.routes[0].destination_station_id = "UNKNOWN-STATION"
    errors = validate_snapshot(snapshot)
    assert any("references unknown source depot" in e for e in errors)
    assert any("references unknown destination station" in e for e in errors)


def test_state_store_rejects_malformed_snapshot_and_preserves_last_good() -> None:
    store = StateStore()

    # 1. Ingest a valid, fresh snapshot at tick 10
    valid_snap = _make_valid_snapshot(tick=10, is_stale=False)
    success = store.update(valid_snap)
    assert success is True
    assert store.get_latest() == valid_snap
    assert store.get_last_good() == valid_snap
    assert not store.has_malformed_alert
    assert store.rejected_snapshots_count == 0

    # 2. Attempt to ingest a malformed snapshot at tick 11 with negative station inventory
    corrupt_snap = _make_valid_snapshot(tick=11, is_stale=False)
    corrupt_snap.stations[0].inventory[FuelType.DIESEL] = -999.0

    success_corrupt = store.update(corrupt_snap)
    assert success_corrupt is False  # Snapshot rejected!

    # 3. Verify state store preserved last known good snapshot
    assert store.rejected_snapshots_count == 1
    assert store.has_malformed_alert is True
    assert store.malformed_alert is not None
    assert store.malformed_alert.tick == 11
    assert any("negative inventory" in e for e in store.malformed_alert.errors)

    # 4. Effective snapshot and get_latest() still serve the last-known-good snapshot
    assert store.get_effective_snapshot() == valid_snap
    assert store.get_latest() == valid_snap
    assert store.get_last_good() == valid_snap

    # 5. Operational mode resolver reports DEGRADED_CACHED due to rejected snapshot
    diagnosis = diagnose_operational_mode(store=store)
    assert diagnosis.mode == OperationalMode.DEGRADED_CACHED
    assert diagnosis.malformed_rejected is True
    assert "rejected due to malformed data" in diagnosis.reason
    assert len(diagnosis.active_alerts) == 1


def test_state_store_clears_malformed_alert_on_subsequent_valid_snapshot() -> None:
    store = StateStore()
    valid_snap_1 = _make_valid_snapshot(tick=10)
    store.update(valid_snap_1)

    # Ingest bad snapshot
    bad_snap = _make_valid_snapshot(tick=11)
    bad_snap.routes[0].transit_ticks = 0
    store.update(bad_snap)
    assert store.has_malformed_alert is True

    # Ingest next valid snapshot at tick 12
    valid_snap_2 = _make_valid_snapshot(tick=12)
    success = store.update(valid_snap_2)
    assert success is True
    assert store.has_malformed_alert is False
    assert store.malformed_alert is None
    assert store.get_latest() == valid_snap_2
    assert store.get_last_good() == valid_snap_2

    # Operational mode returns to NORMAL
    diagnosis = diagnose_operational_mode(store=store)
    assert diagnosis.mode == OperationalMode.NORMAL
