"""Snapshot Data Integrity and Validation.

Validates semantic and structural invariants of incoming SimulatorSnapshot instances.
Enforces domain rules:
1. Valid instance metadata (non-negative tick, positive tick_minutes, valid time).
2. Stations sanity: unique IDs, non-negative fuel inventories, positive capacities.
3. Depots sanity: unique IDs, non-negative fuel inventories, non-negative dispatch capacities.
4. Routes sanity: positive transit_ticks, positive max_shipment, valid endpoint references.

Snapshots violating any rule are rejected, preserving the platform's last-known-good
state and raising an explicit DataIntegrityAlert.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.models import SimulatorSnapshot

logger = logging.getLogger(__name__)


class SnapshotValidationError(ValueError):
    """Raised when a SimulatorSnapshot fails domain validation checks."""

    def __init__(self, errors: list[str], tick: int | None = None) -> None:
        self.errors = errors
        self.tick = tick
        msg = f"Snapshot validation failed (tick={tick}): {'; '.join(errors)}"
        super().__init__(msg)


def validate_snapshot(snapshot: SimulatorSnapshot) -> list[str]:
    """Validate semantic and data integrity invariants on a SimulatorSnapshot.

    Args:
        snapshot: SimulatorSnapshot instance to inspect.

    Returns:
        List of validation error strings. Empty list indicates snapshot is completely valid.
    """
    errors: list[str] = []

    # 1. Instance metadata validation
    if snapshot.instance is None:
        errors.append("Snapshot instance info is missing (None).")
    else:
        if snapshot.instance.tick < 0:
            errors.append(f"Instance tick cannot be negative: {snapshot.instance.tick}.")
        if snapshot.instance.tick_minutes <= 0:
            errors.append(
                f"Instance tick_minutes must be positive: {snapshot.instance.tick_minutes}."
            )
        if not snapshot.instance.sim_time:
            errors.append("Instance sim_time is empty or missing.")

    # 2. Stations validation
    station_ids: set[str] = set()
    for station in snapshot.stations:
        if not station.id or not str(station.id).strip():
            errors.append("Station found with empty or missing ID.")
        elif station.id in station_ids:
            errors.append(f"Duplicate station ID detected: '{station.id}'.")
        else:
            station_ids.add(station.id)

        # Inventory check: physical fuel cannot be negative
        for fuel_type, qty in station.inventory.items():
            fuel_label = getattr(fuel_type, "value", str(fuel_type))
            if qty < 0.0:
                errors.append(
                    f"Station '{station.id}' ({station.name}) has negative inventory "
                    f"for {fuel_label}: {qty:.1f} L."
                )

        # Capacity check: cannot be negative, and total capacity must be positive
        total_st_cap = (
            station.capacity.total()
            if hasattr(station.capacity, "total")
            else sum(station.capacity.values())
        )
        if total_st_cap <= 0.0:
            errors.append(
                f"Station '{station.id}' ({station.name}) has non-positive "
                f"total capacity: {total_st_cap:.1f} L."
            )
        for fuel_type, cap in station.capacity.items():
            fuel_label = getattr(fuel_type, "value", str(fuel_type))
            if cap < 0.0:
                errors.append(
                    f"Station '{station.id}' ({station.name}) has negative capacity "
                    f"for {fuel_label}: {cap:.1f} L."
                )

    # 3. Depots validation
    depot_ids: set[str] = set()
    for depot in snapshot.depots:
        if not depot.id or not str(depot.id).strip():
            errors.append("Depot found with empty or missing ID.")
        elif depot.id in depot_ids:
            errors.append(f"Duplicate depot ID detected: '{depot.id}'.")
        else:
            depot_ids.add(depot.id)

        # Inventory check: physical fuel cannot be negative
        for fuel_type, qty in depot.inventory.items():
            fuel_label = getattr(fuel_type, "value", str(fuel_type))
            if qty < 0.0:
                errors.append(
                    f"Depot '{depot.id}' ({depot.name}) has negative inventory "
                    f"for {fuel_label}: {qty:.1f} L."
                )

        # Capacity check: cannot be negative, and total capacity must be positive
        total_dp_cap = (
            depot.capacity.total()
            if hasattr(depot.capacity, "total")
            else sum(depot.capacity.values())
        )
        if total_dp_cap <= 0.0:
            errors.append(
                f"Depot '{depot.id}' ({depot.name}) has non-positive "
                f"total capacity: {total_dp_cap:.1f} L."
            )
        for fuel_type, cap in depot.capacity.items():
            fuel_label = getattr(fuel_type, "value", str(fuel_type))
            if cap < 0.0:
                errors.append(
                    f"Depot '{depot.id}' ({depot.name}) has negative capacity "
                    f"for {fuel_label}: {cap:.1f} L."
                )

        # Dispatch capacity check
        if depot.dispatch_capacity_per_tick < 0.0:
            errors.append(
                f"Depot '{depot.id}' ({depot.name}) has negative dispatch_capacity_per_tick: "
                f"{depot.dispatch_capacity_per_tick:.1f} L."
            )

    # 4. Routes validation
    for route in snapshot.routes:
        if not route.id or not str(route.id).strip():
            errors.append("Route found with empty or missing ID.")
        if route.transit_ticks <= 0:
            errors.append(
                f"Route '{route.id}' has non-positive transit_ticks: {route.transit_ticks}."
            )
        if route.max_shipment <= 0.0:
            errors.append(
                f"Route '{route.id}' has non-positive max_shipment: {route.max_shipment:.1f} L."
            )
        # Check source and destination endpoints reference known nodes if topology present
        if depot_ids and route.source_depot_id not in depot_ids:
            errors.append(
                f"Route '{route.id}' references unknown source depot '{route.source_depot_id}'."
            )
        if station_ids and route.destination_station_id not in station_ids:
            errors.append(
                f"Route '{route.id}' references unknown destination station "
                f"'{route.destination_station_id}'."
            )

    return errors
