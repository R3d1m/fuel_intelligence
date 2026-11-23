"""Fuel Inventory Risk Engine and Forward-Projection Evaluator.

Projects station and depot fuel inventory levels over future operational horizons:
inventory + scheduled arrivals + in-transit shipments - forecast demand.

Calculates:
- Hours to stockout and tick of first stockout
- Severity classification: OK, WATCH, CRITICAL
- Confidence score (0.0 to 1.0) derived from forecast error (MAPE) and data freshness
- Unavoidable stockout detection based on route transit times
- Multi-signal risk drivers and explainable diagnostic narratives
- Fleet-wide risk aggregation and urgency-ranked operational alerts
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.domain.models import (
    Allocation,
    AllocationStatus,
    DemandHistoryRecord,
    Depot,
    FuelType,
    RiskSeverity,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    SupplyArrivalStatus,
)
from app.intelligence.forecast import (
    DemandForecaster,
    FleetDemandForecast,
    StationFuelForecast,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


class InventoryTrajectoryPoint(BaseModel):
    """Single tick point in projected inventory trajectory."""

    model_config = ConfigDict(extra="ignore")

    step: int = Field(ge=1, description="Projection step index (1..horizon)")
    tick: int = Field(ge=0, description="Future simulation tick number")
    sim_time: str | None = Field(None, description="Projected simulation timestamp if available")
    projected_inventory: float = Field(ge=0.0, description="Liters remaining in tank at tick end")
    fill_ratio: float = Field(ge=0.0, le=1.0, description="Projected inventory / capacity")
    incoming_liters: float = Field(ge=0.0, description="Replenishment fuel arriving this tick")
    forecast_demand_liters: float = Field(ge=0.0, description="Projected consumption this tick")
    unmet_demand_liters: float = Field(
        ge=0.0, description="Liters of demand unserved due to stockout"
    )
    projected_overflow_liters: float = Field(
        default=0.0, ge=0.0, description="Fuel volume exceeding tank capacity"
    )
    is_stockout: bool = Field(default=False, description="Whether inventory reached zero")


class RiskDrivers(BaseModel):
    """Diagnostic signals and inputs that drove the risk evaluation."""

    model_config = ConfigDict(extra="ignore")

    initial_inventory_liters: float = Field(ge=0.0)
    tank_capacity_liters: float = Field(ge=0.0)
    initial_fill_ratio: float = Field(ge=0.0, le=1.0)
    is_low_inventory: bool = False
    is_demand_spike: bool = False
    demand_rate_liters_per_hour: float = Field(ge=0.0)
    in_transit_shipments_count: int = Field(ge=0)
    in_transit_liters_total: float = Field(ge=0.0)
    pending_shipments_count: int = Field(ge=0)
    pending_liters_total: float = Field(ge=0.0)
    available_supply_routes_count: int = Field(ge=0)
    total_supply_routes_count: int = Field(ge=0)
    all_supply_routes_disrupted: bool = False
    min_transit_ticks: int | None = None
    forecast_mape: float = Field(ge=0.0)
    data_age_seconds: float = Field(ge=0.0)
    is_stale_data: bool = False
    summary_reasons: list[str] = Field(default_factory=list)


class RiskAlert(BaseModel):
    """Operational alert for an at-risk fuel stock at a station, ranked by urgency."""

    model_config = ConfigDict(extra="ignore")

    station_id: str
    station_name: str
    fuel_type: FuelType
    severity: RiskSeverity
    hours_to_stockout: float | None = None
    first_stockout_tick: int | None = None
    current_fill_ratio: float = Field(ge=0.0, le=1.0)
    urgency_score: float = Field(
        description="Normalized sorting score; higher indicates greater urgency"
    )
    headline: str
    explanation: str
    created_tick: int


class StationFuelRisk(BaseModel):
    """Complete risk assessment and inventory projection for a single station and fuel."""

    model_config = ConfigDict(extra="ignore")

    station_id: str
    station_name: str
    fuel_type: FuelType
    severity: RiskSeverity
    confidence: float = Field(ge=0.0, le=1.0, description="Overall risk confidence score (0-1)")
    confidence_level: str = Field(description="Categorical confidence: HIGH, MEDIUM, LOW")
    needs_human_review: bool = Field(
        default=False, description="Flagged when confidence is low or risk is critical"
    )
    current_inventory_liters: float = Field(ge=0.0)
    capacity_liters: float = Field(gt=0.0)
    current_fill_ratio: float = Field(ge=0.0, le=1.0)
    hours_to_stockout: float | None = Field(
        None, description="Hours until first stockout; None if no stockout in horizon"
    )
    first_stockout_tick: int | None = Field(
        None, description="Simulation tick of first stockout; None if no stockout"
    )
    ticks_to_stockout: int | None = Field(
        None, description="Number of ticks until first stockout occurs"
    )
    is_unavoidable_stockout: bool = Field(
        default=False,
        description="True if stockout occurs before the fastest replenishment could arrive",
    )
    unavoidable_shortage_hours: float | None = Field(
        None,
        description="Estimated duration in hours station sits empty before earliest arrival",
    )
    total_unmet_liters: float = Field(
        default=0.0, ge=0.0, description="Total projected unmet demand over horizon"
    )
    min_projected_inventory: float = Field(ge=0.0)
    min_projected_fill_ratio: float = Field(ge=0.0, le=1.0)
    burn_rate_liters_per_hour: float = Field(ge=0.0)
    runout_hours_no_arrivals: float | None = Field(
        None, description="Run-out time at current burn rate assuming zero new arrivals"
    )
    drivers: RiskDrivers
    trajectory: list[InventoryTrajectoryPoint] = Field(default_factory=list)
    explanation: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DepotFuelRisk(BaseModel):
    """Risk assessment for upstream depot fuel reserves."""

    model_config = ConfigDict(extra="ignore")

    depot_id: str
    depot_name: str
    fuel_type: FuelType
    severity: RiskSeverity
    current_inventory_liters: float = Field(ge=0.0)
    capacity_liters: float = Field(gt=0.0)
    current_fill_ratio: float = Field(ge=0.0, le=1.0)
    pending_dispatch_liters: float = Field(ge=0.0)
    scheduled_replenishment_liters: float = Field(ge=0.0)
    effective_available_liters: float = Field(ge=0.0)
    explanation: str


class FleetRiskAssessment(BaseModel):
    """Aggregate fleet-wide risk report across all stations, depots, and fuels."""

    model_config = ConfigDict(extra="ignore")

    overall_severity: RiskSeverity
    critical_count: int = Field(ge=0)
    watch_count: int = Field(ge=0)
    ok_count: int = Field(ge=0)
    total_projected_unmet_liters: float = Field(ge=0.0)
    earliest_stockout_station_id: str | None = None
    earliest_stockout_fuel: FuelType | None = None
    earliest_stockout_hours: float | None = None
    fleet_average_confidence: float = Field(ge=0.0, le=1.0)
    active_alerts: list[RiskAlert] = Field(default_factory=list)
    station_risks: dict[str, dict[str, StationFuelRisk]] = Field(default_factory=dict)
    depot_risks: dict[str, dict[str, DepotFuelRisk]] = Field(default_factory=dict)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Utility & Heuristic Helpers
# ---------------------------------------------------------------------------


def calculate_allocation_arrival_tick(
    alloc: Allocation,
    routes: Sequence[Route],
    current_tick: int,
) -> int:
    """Calculate expected tick at which an allocation arrives at destination station."""
    if alloc.expected_arrival_tick is not None:
        return alloc.expected_arrival_tick

    route = next((r for r in routes if r.id == alloc.route_id), None)
    transit = route.transit_ticks if route else 2

    if alloc.departure_tick is not None:
        return alloc.departure_tick + transit

    return current_tick + 1 + transit


def compute_confidence_score(
    data_age_seconds: float,
    is_stale: bool,
    stale_threshold_seconds: float,
    forecast_mape: float,
    sample_count: int,
) -> tuple[float, str, bool]:
    """Compute confidence score (0.0 to 1.0), categorical level, and human review flag."""
    if is_stale:
        c_freshness = 0.40
    elif data_age_seconds > stale_threshold_seconds:
        age_ratio = min(1.0, data_age_seconds / max(1.0, stale_threshold_seconds * 3.0))
        c_freshness = max(0.20, 1.0 - (0.80 * age_ratio))
    else:
        c_freshness = 1.0

    if sample_count >= 3:
        mape_penalty = min(0.80, forecast_mape / 100.0)
        c_forecast = max(0.20, 1.0 - mape_penalty)
    else:
        c_forecast = 0.70

    confidence = round(c_freshness * c_forecast, 3)
    confidence = max(0.05, min(1.0, confidence))

    if confidence >= 0.80:
        level = "HIGH"
    elif confidence >= 0.50:
        level = "MEDIUM"
    else:
        level = "LOW"

    needs_review = confidence < 0.60
    return confidence, level, needs_review


def calculate_urgency_score(
    severity: RiskSeverity,
    hours_to_stockout: float | None,
    current_fill_ratio: float,
    is_demand_spike: bool,
) -> float:
    """Compute continuous scalar urgency score for priority ranking of operator alerts."""
    base_weight = {
        RiskSeverity.CRITICAL: 1000.0,
        RiskSeverity.WATCH: 200.0,
        RiskSeverity.OK: 0.0,
    }.get(severity, 0.0)

    proximity_score = 0.0
    if hours_to_stockout is not None:
        proximity_score = 500.0 / max(0.1, hours_to_stockout)

    tank_emptiness = (1.0 - current_fill_ratio) * 100.0
    spike_bonus = 50.0 if is_demand_spike else 0.0

    return round(base_weight + proximity_score + tank_emptiness + spike_bonus, 2)


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------


class RiskEngine:
    """Evaluates stock-flow inventory projections and operational risks."""

    def __init__(
        self,
        settings: Settings | None = None,
        forecaster: DemandForecaster | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.forecaster = forecaster or DemandForecaster(settings=self.settings)

    def assess_station_fuel_risk(
        self,
        station: Station,
        fuel: FuelType,
        snapshot: SimulatorSnapshot,
        forecast: StationFuelForecast | None = None,
        demand_history: Sequence[DemandHistoryRecord] = (),
        data_age_seconds: float = 0.0,
        is_stale: bool = False,
        horizon_ticks: int | None = None,
    ) -> StationFuelRisk:
        """Project inventory and determine stockout risk for a specific station and fuel."""
        h_ticks = horizon_ticks or self.settings.risk_horizon_ticks
        current_tick = snapshot.instance.tick
        tick_minutes = max(1, snapshot.instance.tick_minutes)
        capacity = max(1.0, station.capacity.get(fuel, 1.0))
        current_inventory = max(0.0, station.inventory.get(fuel, 0.0))
        current_fill_ratio = round(current_inventory / capacity, 4)

        if forecast is None:
            forecast = self.forecaster.forecast_station_fuel(
                station=station,
                fuel=fuel,
                history=demand_history,
                tick_minutes=tick_minutes,
                horizon_ticks=h_ticks,
                current_tick=current_tick,
            )

        routes_to_station = [r for r in snapshot.routes if r.destination_station_id == station.id]
        available_routes = [
            r
            for r in routes_to_station
            if (
                isinstance(r.status, RouteStatus)
                and r.status == RouteStatus.AVAILABLE
                or str(r.status).upper() == "AVAILABLE"
            )
        ]
        all_routes_disrupted = len(routes_to_station) > 0 and len(available_routes) == 0
        min_transit_ticks = (
            min((r.transit_ticks for r in available_routes), default=None)
            if available_routes
            else None
        )

        incoming_by_tick: dict[int, float] = {}
        in_transit_liters = 0.0
        in_transit_count = 0
        pending_liters = 0.0
        pending_count = 0
        disrupted_pending_shipments: list[str] = []

        for alloc in snapshot.allocations:
            if alloc.destination_station_id != station.id or alloc.fuel_type != fuel:
                continue

            status_str = (
                alloc.status.value
                if isinstance(alloc.status, AllocationStatus)
                else str(alloc.status)
            )

            if status_str == "IN_TRANSIT":
                arr_tick = calculate_allocation_arrival_tick(alloc, snapshot.routes, current_tick)
                incoming_by_tick[arr_tick] = incoming_by_tick.get(arr_tick, 0.0) + alloc.quantity
                in_transit_liters += alloc.quantity
                in_transit_count += 1

            elif status_str == "PENDING":
                alloc_route = next((r for r in snapshot.routes if r.id == alloc.route_id), None)
                route_status = (
                    (
                        alloc_route.status.value
                        if isinstance(alloc_route.status, RouteStatus)
                        else str(alloc_route.status)
                    )
                    if alloc_route
                    else "AVAILABLE"
                )

                if route_status == "DISRUPTED":
                    disrupted_pending_shipments.append(
                        f"Allocation #{alloc.id} ({alloc.quantity:,.0f} L) "
                        f"pending on DISRUPTED route '{alloc.route_id}'"
                    )
                else:
                    arr_tick = calculate_allocation_arrival_tick(
                        alloc, snapshot.routes, current_tick
                    )
                    incoming_by_tick[arr_tick] = (
                        incoming_by_tick.get(arr_tick, 0.0) + alloc.quantity
                    )
                    pending_liters += alloc.quantity
                    pending_count += 1

        trajectory: list[InventoryTrajectoryPoint] = []
        projected_inv = current_inventory
        first_stockout_tick: int | None = None
        ticks_to_stockout: int | None = None
        total_unmet_liters = 0.0
        min_projected_inv = current_inventory

        forecast_map = {pt.tick: pt.forecast_liters_per_tick for pt in forecast.trajectory}

        for step in range(1, h_ticks + 1):
            target_tick = current_tick + step
            incoming = incoming_by_tick.get(target_tick, 0.0)
            demand = forecast_map.get(target_tick, forecast.forecast_per_tick)

            inv_pre_demand = projected_inv + incoming
            overflow = max(0.0, inv_pre_demand - capacity)
            inv_capped = min(capacity, inv_pre_demand)

            if inv_capped >= demand:
                projected_inv = inv_capped - demand
                unmet = 0.0
                is_stockout = False
            else:
                unmet = demand - inv_capped
                projected_inv = 0.0
                total_unmet_liters += unmet
                is_stockout = True

                if first_stockout_tick is None:
                    first_stockout_tick = target_tick
                    ticks_to_stockout = step

            min_projected_inv = min(min_projected_inv, projected_inv)

            trajectory.append(
                InventoryTrajectoryPoint(
                    step=step,
                    tick=target_tick,
                    projected_inventory=round(projected_inv, 2),
                    fill_ratio=round(min(1.0, max(0.0, projected_inv / capacity)), 4),
                    incoming_liters=round(incoming, 2),
                    forecast_demand_liters=round(demand, 2),
                    unmet_demand_liters=round(unmet, 2),
                    projected_overflow_liters=round(overflow, 2),
                    is_stockout=is_stockout,
                )
            )

        hours_to_stockout = (
            round((ticks_to_stockout * tick_minutes) / 60.0, 2)
            if ticks_to_stockout is not None
            else None
        )

        is_unavoidable = False
        unavoidable_hours: float | None = None
        if first_stockout_tick is not None and min_transit_ticks is not None:
            earliest_possible_delivery = current_tick + 1 + min_transit_ticks
            if first_stockout_tick < earliest_possible_delivery:
                is_unavoidable = True
                unavoidable_ticks = earliest_possible_delivery - first_stockout_tick
                unavoidable_hours = round((unavoidable_ticks * tick_minutes) / 60.0, 2)

        burn_rate_per_hour = forecast.forecast_per_hour
        runout_no_arrivals = (
            round(current_inventory / max(0.1, burn_rate_per_hour), 2)
            if burn_rate_per_hour > 0.0
            else None
        )

        is_low_inventory = current_fill_ratio <= self.settings.risk_critical_fill_ratio
        is_watch_inventory = current_fill_ratio <= self.settings.risk_watch_fill_ratio
        is_spike = forecast.anomaly.is_spike

        if (
            (
                hours_to_stockout is not None
                and hours_to_stockout <= self.settings.risk_critical_hours
            )
            or is_low_inventory
            or is_unavoidable
        ):
            severity = RiskSeverity.CRITICAL
        elif (
            (hours_to_stockout is not None and hours_to_stockout <= self.settings.risk_watch_hours)
            or is_watch_inventory
            or is_spike
            or all_routes_disrupted
        ):
            severity = RiskSeverity.WATCH
        else:
            severity = RiskSeverity.OK

        confidence, conf_level, needs_review = compute_confidence_score(
            data_age_seconds=data_age_seconds,
            is_stale=is_stale,
            stale_threshold_seconds=self.settings.stale_threshold_seconds,
            forecast_mape=forecast.metrics.mape,
            sample_count=forecast.metrics.sample_count,
        )
        if severity == RiskSeverity.CRITICAL:
            needs_review = True

        summary_reasons: list[str] = []

        if current_fill_ratio <= self.settings.risk_critical_fill_ratio:
            summary_reasons.append(
                f"Tank inventory critically depleted: {current_fill_ratio:.1%} "
                f"({current_inventory:,.0f} L / {capacity:,.0f} L)."
            )
        elif current_fill_ratio <= self.settings.risk_watch_fill_ratio:
            summary_reasons.append(
                f"Tank inventory in watch territory: {current_fill_ratio:.1%} "
                f"({current_inventory:,.0f} L / {capacity:,.0f} L)."
            )

        if is_spike:
            summary_reasons.append(
                f"Active demand spike detected: multiplier "
                f"{forecast.anomaly.demand_multiplier:.2f}x "
                f"({forecast.forecast_per_hour:,.1f} L/h rate)."
            )

        if is_unavoidable:
            req_ticks = (min_transit_ticks or 0) + 1
            req_hrs = (req_ticks * tick_minutes) / 60.0
            summary_reasons.append(
                f"Unavoidable stockout alert: depletion occurs at tick {first_stockout_tick} "
                f"({hours_to_stockout:.1f}h), but fastest route requires at least "
                f"{req_ticks} ticks ({req_hrs:.1f}h) to deliver."
            )
        elif first_stockout_tick is not None:
            summary_reasons.append(
                f"First stockout projected at tick {first_stockout_tick} "
                f"({hours_to_stockout:.1f}h) with {total_unmet_liters:,.0f} L "
                f"unmet demand over horizon."
            )

        if all_routes_disrupted:
            summary_reasons.append(
                "All inbound replenishment routes are DISRUPTED (station isolated)."
            )

        for d_warn in disrupted_pending_shipments:
            summary_reasons.append(f"WARNING: {d_warn}; will fail at departure.")

        if in_transit_count > 0:
            summary_reasons.append(
                f"{in_transit_count} shipment(s) in transit ({in_transit_liters:,.0f} L arriving)."
            )

        if pending_count > 0:
            summary_reasons.append(
                f"{pending_count} valid shipment(s) pending dispatch "
                f"({pending_liters:,.0f} L scheduled)."
            )

        if is_stale or data_age_seconds > self.settings.stale_threshold_seconds:
            summary_reasons.append(
                f"Data staleness warning: snapshot is {data_age_seconds:.1f}s old; "
                f"confidence penalized."
            )

        if not summary_reasons:
            runout_val = runout_no_arrivals or 0.0
            summary_reasons.append(
                f"Nominal operations: {current_fill_ratio:.1%} full, "
                f"{runout_val:.1f}h reserve runout at {burn_rate_per_hour:,.1f} L/h demand."
            )

        explanation = " | ".join(summary_reasons)

        drivers = RiskDrivers(
            initial_inventory_liters=round(current_inventory, 2),
            tank_capacity_liters=round(capacity, 2),
            initial_fill_ratio=current_fill_ratio,
            is_low_inventory=is_low_inventory,
            is_demand_spike=is_spike,
            demand_rate_liters_per_hour=round(burn_rate_per_hour, 2),
            in_transit_shipments_count=in_transit_count,
            in_transit_liters_total=round(in_transit_liters, 2),
            pending_shipments_count=pending_count,
            pending_liters_total=round(pending_liters, 2),
            available_supply_routes_count=len(available_routes),
            total_supply_routes_count=len(routes_to_station),
            all_supply_routes_disrupted=all_routes_disrupted,
            min_transit_ticks=min_transit_ticks,
            forecast_mape=round(forecast.metrics.mape, 2),
            data_age_seconds=round(data_age_seconds, 2),
            is_stale_data=is_stale,
            summary_reasons=summary_reasons,
        )

        return StationFuelRisk(
            station_id=station.id,
            station_name=station.name,
            fuel_type=fuel,
            severity=severity,
            confidence=confidence,
            confidence_level=conf_level,
            needs_human_review=needs_review,
            current_inventory_liters=round(current_inventory, 2),
            capacity_liters=round(capacity, 2),
            current_fill_ratio=current_fill_ratio,
            hours_to_stockout=hours_to_stockout,
            first_stockout_tick=first_stockout_tick,
            ticks_to_stockout=ticks_to_stockout,
            is_unavoidable_stockout=is_unavoidable,
            unavoidable_shortage_hours=unavoidable_hours,
            total_unmet_liters=round(total_unmet_liters, 2),
            min_projected_inventory=round(min_projected_inv, 2),
            min_projected_fill_ratio=round(min_projected_inv / capacity, 4),
            burn_rate_liters_per_hour=round(burn_rate_per_hour, 2),
            runout_hours_no_arrivals=runout_no_arrivals,
            drivers=drivers,
            trajectory=trajectory,
            explanation=explanation,
        )

    def assess_depot_fuel_risk(
        self,
        depot: Depot,
        fuel: FuelType,
        snapshot: SimulatorSnapshot,
        horizon_ticks: int | None = None,
    ) -> DepotFuelRisk:
        """Evaluate inventory, incoming replenishment, and pending commitments for a bulk depot."""
        h_ticks = horizon_ticks or self.settings.risk_horizon_ticks
        current_tick = snapshot.instance.tick
        capacity = max(1.0, depot.capacity.get(fuel, 1.0))
        current_inv = max(0.0, depot.inventory.get(fuel, 0.0))
        fill_ratio = round(current_inv / capacity, 4)

        scheduled_in = sum(
            sa.quantity
            for sa in snapshot.supply_arrivals
            if (
                sa.depot_id == depot.id
                and sa.fuel_type == fuel
                and (
                    sa.status == SupplyArrivalStatus.SCHEDULED
                    or str(sa.status).upper() == "SCHEDULED"
                )
                and sa.planned_tick <= current_tick + h_ticks
            )
        )

        pending_out = sum(
            al.quantity
            for al in snapshot.allocations
            if (
                al.source_depot_id == depot.id
                and al.fuel_type == fuel
                and (al.status == AllocationStatus.PENDING or str(al.status).upper() == "PENDING")
            )
        )

        effective_available = max(0.0, current_inv + scheduled_in - pending_out)
        effective_ratio = effective_available / capacity

        if effective_ratio <= 0.15:
            severity = RiskSeverity.CRITICAL
            explanation = (
                f"Depot {fuel.value} critically constrained: {effective_ratio:.1%} effective "
                f"available ({effective_available:,.0f} L) after commitments."
            )
        elif effective_ratio <= 0.30:
            severity = RiskSeverity.WATCH
            explanation = (
                f"Depot {fuel.value} in watch territory: {effective_ratio:.1%} effective "
                f"available ({effective_available:,.0f} L)."
            )
        else:
            severity = RiskSeverity.OK
            explanation = (
                f"Depot {fuel.value} healthy: {fill_ratio:.1%} full ({current_inv:,.0f} L), "
                f"{scheduled_in:,.0f} L scheduled replenishment."
            )

        return DepotFuelRisk(
            depot_id=depot.id,
            depot_name=depot.name,
            fuel_type=fuel,
            severity=severity,
            current_inventory_liters=round(current_inv, 2),
            capacity_liters=round(capacity, 2),
            current_fill_ratio=fill_ratio,
            pending_dispatch_liters=round(pending_out, 2),
            scheduled_replenishment_liters=round(scheduled_in, 2),
            effective_available_liters=round(effective_available, 2),
            explanation=explanation,
        )

    def assess_fleet_risk(
        self,
        snapshot: SimulatorSnapshot,
        fleet_forecast: FleetDemandForecast | None = None,
        demand_history: Sequence[DemandHistoryRecord] = (),
        data_age_seconds: float = 0.0,
        is_stale: bool = False,
        horizon_ticks: int | None = None,
    ) -> FleetRiskAssessment:
        """Run comprehensive fleet-wide risk assessment across all stations, depots, and fuels."""
        h_ticks = horizon_ticks or self.settings.risk_horizon_ticks

        if fleet_forecast is None:
            fleet_forecast = self.forecaster.forecast_all(
                snapshot=snapshot,
                demand_history=demand_history,
                horizon_ticks=h_ticks,
            )

        station_risks: dict[str, dict[str, StationFuelRisk]] = {}
        alerts: list[RiskAlert] = []
        critical_count = 0
        watch_count = 0
        ok_count = 0
        total_unmet = 0.0
        earliest_station_id: str | None = None
        earliest_fuel: FuelType | None = None
        earliest_hours: float | None = None
        confidences: list[float] = []

        for station in snapshot.stations:
            station_risks[station.id] = {}
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                fc = fleet_forecast.forecasts.get(station.id, {}).get(fuel.value)
                risk = self.assess_station_fuel_risk(
                    station=station,
                    fuel=fuel,
                    snapshot=snapshot,
                    forecast=fc,
                    demand_history=demand_history,
                    data_age_seconds=data_age_seconds,
                    is_stale=is_stale,
                    horizon_ticks=h_ticks,
                )
                station_risks[station.id][fuel.value] = risk
                confidences.append(risk.confidence)
                total_unmet += risk.total_unmet_liters

                if risk.severity == RiskSeverity.CRITICAL:
                    critical_count += 1
                elif risk.severity == RiskSeverity.WATCH:
                    watch_count += 1
                else:
                    ok_count += 1

                if risk.hours_to_stockout is not None:
                    if earliest_hours is None or risk.hours_to_stockout < earliest_hours:
                        earliest_hours = risk.hours_to_stockout
                        earliest_station_id = station.id
                        earliest_fuel = fuel

                if risk.severity in (RiskSeverity.CRITICAL, RiskSeverity.WATCH):
                    urgency = calculate_urgency_score(
                        severity=risk.severity,
                        hours_to_stockout=risk.hours_to_stockout,
                        current_fill_ratio=risk.current_fill_ratio,
                        is_demand_spike=risk.drivers.is_demand_spike,
                    )
                    if (
                        risk.severity == RiskSeverity.CRITICAL
                        and risk.hours_to_stockout is not None
                    ):
                        headline = (
                            f"CRITICAL: {station.name} {fuel.value} "
                            f"stockout in {risk.hours_to_stockout:.1f}h"
                        )
                    else:
                        headline = (
                            f"{risk.severity.value}: {station.name} {fuel.value} "
                            f"at {risk.current_fill_ratio:.1%} capacity"
                        )
                    alerts.append(
                        RiskAlert(
                            station_id=station.id,
                            station_name=station.name,
                            fuel_type=fuel,
                            severity=risk.severity,
                            hours_to_stockout=risk.hours_to_stockout,
                            first_stockout_tick=risk.first_stockout_tick,
                            current_fill_ratio=risk.current_fill_ratio,
                            urgency_score=urgency,
                            headline=headline,
                            explanation=risk.explanation,
                            created_tick=snapshot.instance.tick,
                        )
                    )

        alerts.sort(key=lambda a: a.urgency_score, reverse=True)

        depot_risks: dict[str, dict[str, DepotFuelRisk]] = {}
        for depot in snapshot.depots:
            depot_risks[depot.id] = {}
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                depot_risks[depot.id][fuel.value] = self.assess_depot_fuel_risk(
                    depot=depot,
                    fuel=fuel,
                    snapshot=snapshot,
                    horizon_ticks=h_ticks,
                )

        if critical_count > 0:
            overall_sev = RiskSeverity.CRITICAL
        elif watch_count > 0:
            overall_sev = RiskSeverity.WATCH
        else:
            overall_sev = RiskSeverity.OK

        avg_conf = round(sum(confidences) / max(1, len(confidences)), 3)

        return FleetRiskAssessment(
            overall_severity=overall_sev,
            critical_count=critical_count,
            watch_count=watch_count,
            ok_count=ok_count,
            total_projected_unmet_liters=round(total_unmet, 2),
            earliest_stockout_station_id=earliest_station_id,
            earliest_stockout_fuel=earliest_fuel,
            earliest_stockout_hours=earliest_hours,
            fleet_average_confidence=avg_conf,
            active_alerts=alerts,
            station_risks=station_risks,
            depot_risks=depot_risks,
        )
