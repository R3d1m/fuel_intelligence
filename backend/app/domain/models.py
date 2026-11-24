"""Typed domain models and enums for the Fuel Intelligence Platform.

Defines Pydantic v2 data models for all resources interacting with the
BUP Fuel Supply Simulator and downstream platform modules.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FuelType(StrEnum):
    """Supported fuel commodity types."""

    DIESEL = "DIESEL"
    PETROL = "PETROL"
    OCTANE = "OCTANE"


class DepotStatus(StrEnum):
    """Operational status of a fuel depot."""

    OPEN = "OPEN"
    CONSTRAINED = "CONSTRAINED"
    CLOSED = "CLOSED"


class StationStatus(StrEnum):
    """Operational status of a fuel station."""

    OPEN = "OPEN"
    CONSTRAINED = "CONSTRAINED"
    CLOSED = "CLOSED"


class RouteStatus(StrEnum):
    """Operational status of a transit route."""

    AVAILABLE = "AVAILABLE"
    CONGESTED = "CONGESTED"
    DISRUPTED = "DISRUPTED"


class SupplyArrivalStatus(StrEnum):
    """Lifecycle status of an incoming supply arrival at a depot."""

    SCHEDULED = "SCHEDULED"
    ARRIVED = "ARRIVED"
    DELAYED = "DELAYED"
    CANCELLED = "CANCELLED"


class AllocationStatus(StrEnum):
    """Lifecycle status of a fuel dispatch allocation."""

    PENDING = "PENDING"
    IN_TRANSIT = "IN_TRANSIT"
    ARRIVED = "ARRIVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OperationalMode(StrEnum):
    """Explicit operational state of the Fuel Intelligence platform."""

    NORMAL = "NORMAL"
    DEGRADED_CACHED = "DEGRADED_CACHED"
    DEGRADED_FALLBACK = "DEGRADED_FALLBACK"


class EventType(StrEnum):
    """Types of simulation events injected into the environment."""

    DEMAND_SPIKE = "demand_spike"
    ROUTE_DISRUPTION = "route_disruption"
    SUPPLY_DELAY = "supply_delay"
    STATION_OUTAGE = "station_outage"
    DEPOT_CONSTRICTION = "depot_constriction"


class FaultType(StrEnum):
    """Chaos and reliability fault injection types."""

    LATENCY = "latency"
    UNAVAILABLE = "unavailable"
    ERROR_RATE = "error_rate"
    STALE_DATA = "stale_data"
    STREAM_DISCONNECT = "stream_disconnect"


class RiskSeverity(StrEnum):
    """Risk severity classification for fuel stockouts."""

    OK = "OK"
    WATCH = "WATCH"
    CRITICAL = "CRITICAL"


class FuelMap(BaseModel):
    """Mapping of fuel types to numeric quantities (liters)."""

    model_config = ConfigDict(extra="ignore")

    DIESEL: float = 0.0
    PETROL: float = 0.0
    OCTANE: float = 0.0

    def get(self, fuel: str | FuelType, default: float = 0.0) -> float:
        """Retrieve volume for given fuel type."""
        key = fuel.value if isinstance(fuel, FuelType) else str(fuel).upper()
        return getattr(self, key, default)

    def __getitem__(self, item: str | FuelType) -> float:
        """Allow dict-like indexing: fuel_map['DIESEL']."""
        return self.get(item)

    def __setitem__(self, item: str | FuelType, value: float) -> None:
        """Allow dict-like assignment: fuel_map['DIESEL'] = 100.0."""
        key = item.value if isinstance(item, FuelType) else str(item).upper()
        setattr(self, key, value)

    def total(self) -> float:
        """Return total liters across all fuels."""
        return self.DIESEL + self.PETROL + self.OCTANE

    def as_dict(self) -> dict[FuelType, float]:
        """Convert to typed dictionary."""
        return {
            FuelType.DIESEL: self.DIESEL,
            FuelType.PETROL: self.PETROL,
            FuelType.OCTANE: self.OCTANE,
        }

    def items(self) -> list[tuple[FuelType, float]]:
        """Return fuel type to quantity key-value pairs."""
        return list(self.as_dict().items())

    def keys(self) -> list[FuelType]:
        """Return list of fuel types."""
        return list(self.as_dict().keys())

    def values(self) -> list[float]:
        """Return list of fuel quantities."""
        return list(self.as_dict().values())


class InstanceInfo(BaseModel):
    """Simulation instance metadata and clock state."""

    model_config = ConfigDict(extra="ignore")

    id: int
    scenario_id: str
    scenario_version: str
    seed: int
    sim_time: datetime | str
    tick: int
    tick_minutes: int
    status: str


class Region(BaseModel):
    """Geographical administrative division."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    demand_factor: float = 1.0


class Depot(BaseModel):
    """Upstream fuel storage depot."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    region_id: str
    status: DepotStatus | str
    dispatch_capacity_per_tick: float
    capacity: FuelMap
    inventory: FuelMap

    def free_capacity(self, fuel: str | FuelType) -> float:
        """Calculate remaining tank capacity for a specific fuel type."""
        return max(0.0, self.capacity[fuel] - self.inventory[fuel])

    def available_to_dispatch(self, fuel: str | FuelType, reserve: float = 0.0) -> float:
        """Calculate dispatchable fuel volume considering an operational reserve."""
        return max(0.0, self.inventory[fuel] - reserve)


class Station(BaseModel):
    """Downstream retail fuel station."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    region_id: str
    status: StationStatus | str
    demand_profile: str
    demand_multiplier: float = 1.0
    capacity: FuelMap
    inventory: FuelMap

    def free_capacity(self, fuel: str | FuelType) -> float:
        """Calculate current static tank ullage."""
        return max(0.0, self.capacity[fuel] - self.inventory[fuel])

    def effective_free_capacity(
        self,
        fuel: str | FuelType,
        in_transit_liters: float = 0.0,
    ) -> float:
        """Calculate effective free space considering in-flight fuel shipments.

        Empirical verification showed simulator accepts allocations exceeding tank ullage
        if multiple shipments are pending/in-transit. This method protects against overflow.
        """
        return max(0.0, self.capacity[fuel] - (self.inventory[fuel] + in_transit_liters))


class Route(BaseModel):
    """Transit corridor connecting a depot to a station."""

    model_config = ConfigDict(extra="ignore")

    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int
    max_shipment: float
    status: RouteStatus | str


class SupplyArrival(BaseModel):
    """Scheduled upstream fuel replenishment arriving at a depot."""

    model_config = ConfigDict(extra="ignore")

    id: str
    depot_id: str
    fuel_type: FuelType
    quantity: float
    planned_tick: int
    actual_tick: int | None = None
    status: SupplyArrivalStatus | str


class SimEvent(BaseModel):
    """Active or scheduled simulation event affecting operations."""

    model_config = ConfigDict(extra="ignore")

    id: int
    type: str
    start_tick: int
    end_tick: int | None = None
    status: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class Allocation(BaseModel):
    """Fuel shipment allocation from a depot to a destination station."""

    model_config = ConfigDict(extra="ignore")

    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: FuelType
    quantity: float
    created_tick: int
    departure_tick: int | None = None
    expected_arrival_tick: int | None = None
    actual_arrival_tick: int | None = None
    status: AllocationStatus | str
    failure_reason: str | None = None


class AllocationCreate(BaseModel):
    """Request payload to dispatch a new fuel allocation."""

    idempotency_key: str = Field(
        min_length=1,
        max_length=150,
        description="Unique client idempotency token preventing duplicate dispatches.",
    )
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: FuelType
    quantity: float = Field(gt=0.0, description="Volume of fuel in liters to allocate.")


class DemandHistoryRecord(BaseModel):
    """Historical demand observation for a station and fuel at a specific tick."""

    model_config = ConfigDict(extra="ignore")

    id: int
    station_id: str
    fuel_type: FuelType
    tick: int
    sim_time: datetime | str
    demand_liters: float
    served_liters: float
    unmet_liters: float


class SimulatorHealth(BaseModel):
    """Simulator system health and simulation status."""

    model_config = ConfigDict(extra="ignore")

    status: str
    database: str
    simulation: dict[str, Any]


class SimulatorMetrics(BaseModel):
    """Simulator performance and service metrics."""

    model_config = ConfigDict(extra="ignore")

    served_demand_liters: float
    unmet_demand_liters: float
    service_level: float
    allocation_liters: float
    allocation_failures: int


class SimulatorSnapshot(BaseModel):
    """Complete consolidated state snapshot captured from a single polling cycle."""

    model_config = ConfigDict(extra="ignore")

    instance: InstanceInfo
    depots: list[Depot]
    stations: list[Station]
    routes: list[Route]
    supply_arrivals: list[SupplyArrival]
    events: list[SimEvent]
    allocations: list[Allocation]
    tick: int
    sim_time: str
    fetched_at: datetime
    is_stale: bool = False

    @property
    def current_tick(self) -> int:
        """Convenience alias for simulation clock tick."""
        return self.tick


class DataIntegrityAlert(BaseModel):
    """Alert raised when incoming simulator snapshot data fails validation or has missing fields."""

    model_config = ConfigDict(extra="ignore")

    alert_id: str
    tick: int | None = None
    severity: RiskSeverity = RiskSeverity.CRITICAL
    headline: str
    errors: list[str] = Field(default_factory=list)
    action_taken: str = "Rejected snapshot; preserved last-known-good state"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
