"""Constrained Greedy Fuel Dispatch Planner and Optimization Engine.

Formulates and generates feasible, urgency-ranked fuel allocation plans:
- Identifies at-risk (station, fuel) pairs ordered strictly by urgency score.
- Evaluates candidate routes and depots enforcing 5 simultaneous physical constraints:
  1. Route maximum shipment tanker capacity (route.max_shipment)
  2. Available depot inventory above operational reserve
  3. Shared depot dispatch capacity per tick across the plan
  4. Station effective free tank ullage (accounting for in-transit shipments)
  5. Station fuel deficit / replenishment requirement
- Evaluates feasibility and arrival timing against projected stockout ticks:
  - FULL_PREVENTION: Arrival occurs before or at stockout tick
  - PARTIAL_MITIGATION: Arrival occurs after stockout, terminating shortage
  - PREVENTATIVE_RESTOCK: Preemptive replenishment for low-tank WATCH status
- Ranks candidate allocations by simulated risk reduction (with pluggable Digital Twin interface)
- Returns primary action plus top 2 alternative candidates and all applied constraints.
- Tracks shared capacity utilization and records unserved targets with root-cause diagnostics.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.domain.models import (
    AllocationStatus,
    Depot,
    DepotStatus,
    FuelType,
    RiskSeverity,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.intelligence.risk import (
    FleetRiskAssessment,
    RiskEngine,
    StationFuelRisk,
    calculate_urgency_score,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Domain Models
# ---------------------------------------------------------------------------


class ConstraintType(StrEnum):
    """Types of operational constraints that can bottleneck an allocation."""

    ROUTE_MAX_SHIPMENT = "ROUTE_MAX_SHIPMENT"
    DEPOT_INVENTORY = "DEPOT_INVENTORY"
    DEPOT_DISPATCH_CAPACITY = "DEPOT_DISPATCH_CAPACITY"
    STATION_ULLAGE = "STATION_ULLAGE"
    AMOUNT_NEEDED = "AMOUNT_NEEDED"
    MIN_SHIPMENT_THRESHOLD = "MIN_SHIPMENT_THRESHOLD"


class MitigationType(StrEnum):
    """Categorical assessment of replenishment arrival timing vs stockout."""

    FULL_PREVENTION = "FULL_PREVENTION"
    PARTIAL_MITIGATION = "PARTIAL_MITIGATION"
    PREVENTATIVE_RESTOCK = "PREVENTATIVE_RESTOCK"


class CandidateImpact(BaseModel):
    """Expected operational benefit and risk reduction of a candidate shipment."""

    model_config = ConfigDict(extra="ignore")

    expected_unmet_reduction_liters: float = Field(
        ge=0.0, description="Projected liters of unmet customer demand prevented by shipment"
    )
    hours_of_cover_added: float = Field(
        ge=0.0, description="Additional operational run-time hours provided by allocated fuel"
    )
    stockout_probability_before: float = Field(
        ge=0.0, le=1.0, description="Estimated baseline stockout probability without intervention"
    )
    stockout_probability_after: float = Field(
        ge=0.0, le=1.0, description="Estimated residual stockout probability after shipment arrives"
    )
    score: float = Field(description="Scalar ranking score for greedy candidate prioritization")
    scoring_details: dict[str, float] = Field(
        default_factory=dict, description="Breakdown of factors contributing to ranking score"
    )


class AppliedConstraints(BaseModel):
    """Audit breakdown of all 5 physical and operational constraints evaluated."""

    model_config = ConfigDict(extra="ignore")

    route_max_shipment: float = Field(
        ge=0.0, description="Maximum tanker shipment capacity on route"
    )
    depot_available_inventory: float = Field(
        ge=0.0, description="Depot stock above reserve available for this fuel"
    )
    depot_remaining_dispatch_capacity: float = Field(
        ge=0.0, description="Shared depot dispatch volume remaining in this departure tick"
    )
    station_effective_free_space: float = Field(
        ge=0.0, description="Free tank ullage at station accounting for in-flight fuel"
    )
    amount_needed: float = Field(
        ge=0.0, description="Calculated fuel deficit to meet demand or reach target fill level"
    )
    bottleneck_constraint: ConstraintType = Field(
        description="The binding constraint that dictated the final allocated volume"
    )
    allocated_quantity: float = Field(
        ge=0.0, description="Final allocated shipment volume in liters"
    )


class PlanActionAlternative(BaseModel):
    """Feasible alternative allocation route and source depot evaluated but not selected."""

    model_config = ConfigDict(extra="ignore")

    route_id: str
    source_depot_id: str
    source_depot_name: str
    quantity_liters: float = Field(ge=0.0)
    departure_tick: int
    arrival_tick: int
    transit_ticks: int
    is_timely: bool
    mitigation_type: MitigationType
    bottleneck_constraint: ConstraintType
    score: float
    reason_not_chosen: str


class PlanAction(BaseModel):
    """Primary recommended fuel allocation to mitigate an at-risk station stock."""

    model_config = ConfigDict(extra="ignore")

    action_id: str
    station_id: str
    station_name: str
    fuel_type: FuelType
    urgency_score: float
    severity: RiskSeverity
    route_id: str
    source_depot_id: str
    source_depot_name: str
    quantity_liters: float = Field(gt=0.0)
    departure_tick: int
    arrival_tick: int
    transit_ticks: int
    is_timely: bool
    mitigation_type: MitigationType
    unavoidable_shortage_hours: float | None = None
    constraints: AppliedConstraints
    impact: CandidateImpact
    alternatives: list[PlanActionAlternative] = Field(default_factory=list)
    explanation: str
    confidence_score: float = 0.95
    confidence_level: str = "HIGH"
    needs_human_review: bool = False
    is_fallback: bool = False


class UnmetTarget(BaseModel):
    """At-risk station stock that could not be allocated fuel during this planning cycle."""

    model_config = ConfigDict(extra="ignore")

    station_id: str
    station_name: str
    fuel_type: FuelType
    severity: RiskSeverity
    urgency_score: float
    hours_to_stockout: float | None = None
    reason_code: str
    explanation: str


class SharedCapacitySummary(BaseModel):
    """Bookkeeping audit of shared depot dispatch, depot inventory, and station capacities."""

    model_config = ConfigDict(extra="ignore")

    depot_dispatch_utilization: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="Depot dispatch capacity: initial, committed, allocated, remaining",
    )
    depot_inventory_utilization: dict[str, dict[str, dict[str, float]]] = Field(
        default_factory=dict,
        description="Depot fuel stock: initial, reserve, allocated, remaining",
    )
    station_ullage_utilization: dict[str, dict[str, dict[str, float]]] = Field(
        default_factory=dict,
        description="Station tank space: capacity, initial_inv, in_transit, allocated, free",
    )


class DispatchPlan(BaseModel):
    """Consolidated constrained dispatch plan for the entire fuel distribution network."""

    model_config = ConfigDict(extra="ignore")

    plan_id: str
    created_tick: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    total_actions: int = Field(ge=0)
    total_liters_dispatched: float = Field(ge=0.0)
    actions: list[PlanAction] = Field(default_factory=list)
    unmet_targets: list[UnmetTarget] = Field(default_factory=list)
    shared_capacity_summary: SharedCapacitySummary
    summary: str
    is_fallback: bool = False
    fallback_reason: str | None = None


# ---------------------------------------------------------------------------
# Twin Evaluator Protocol & Heuristic Implementation
# ---------------------------------------------------------------------------


class TwinEvaluatorProtocol(Protocol):
    """Protocol for digital twin candidate impact evaluation."""

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
        """Evaluate risk reduction and score for a candidate allocation."""
        ...


class HeuristicTwinEvaluator:
    """Deterministic heuristic evaluator assessing candidate allocation benefit.

    Serves as the baseline risk reduction ranker for Block 1, adhering to
    TwinEvaluatorProtocol so that Block 2's Monte Carlo DigitalTwin can drop in seamlessly.
    """

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
        """Compute estimated unmet reduction, cover extension, and candidate score."""
        unmet_reduction = min(candidate_quantity, risk.total_unmet_liters)
        burn_rate = max(1.0, risk.burn_rate_liters_per_hour)
        hours_cover = round(candidate_quantity / burn_rate, 2)

        # Baseline stockout probability before intervention
        if risk.hours_to_stockout is not None:
            p_before = 1.0 if risk.hours_to_stockout <= 4.0 else 0.85
        elif risk.severity == RiskSeverity.WATCH:
            p_before = 0.40
        else:
            p_before = 0.10

        # Residual stockout probability after intervention
        if is_timely:
            # If shipment is timely and covers projected deficit
            if candidate_quantity >= (risk.total_unmet_liters or (station.capacity[fuel] * 0.25)):
                p_after = 0.05
            else:
                p_after = 0.25
        else:
            # Partial mitigation: stockout occurs briefly before arrival
            p_after = 0.45

        # Multi-factor candidate prioritization score:
        # 1. Unmet demand reduction (heavily weighted: 3.0 pts per liter)
        unmet_pts = unmet_reduction * 3.0

        # 2. Hours of cover added (50 pts per hour)
        cover_pts = hours_cover * 50.0

        # 3. Timely delivery bonus vs late arrival penalty
        if is_timely:
            timeliness_pts = 400.0
        else:
            ticks_late = max(1, arrival_tick - (risk.first_stockout_tick or arrival_tick))
            timeliness_pts = -50.0 * ticks_late

        # 4. Route speed penalty (prefer faster delivery to minimize risk window)
        transit_pts = -15.0 * route.transit_ticks

        total_score = round(unmet_pts + cover_pts + timeliness_pts + transit_pts, 2)

        return CandidateImpact(
            expected_unmet_reduction_liters=round(unmet_reduction, 2),
            hours_of_cover_added=hours_cover,
            stockout_probability_before=p_before,
            stockout_probability_after=p_after,
            score=total_score,
            scoring_details={
                "unmet_pts": round(unmet_pts, 2),
                "cover_pts": round(cover_pts, 2),
                "timeliness_pts": round(timeliness_pts, 2),
                "transit_pts": round(transit_pts, 2),
            },
        )


# ---------------------------------------------------------------------------
# Candidate Helper Structures
# ---------------------------------------------------------------------------


class _CandidateEvaluation:
    """Internal helper tracking an evaluated candidate route/depot option."""

    def __init__(
        self,
        route: Route,
        depot: Depot,
        station: Station,
        fuel: FuelType,
        quantity: float,
        constraints: AppliedConstraints,
        departure_tick: int,
        arrival_tick: int,
        is_timely: bool,
        mitigation_type: MitigationType,
        unavoidable_shortage_hours: float | None,
        impact: CandidateImpact,
    ) -> None:
        self.route = route
        self.depot = depot
        self.station = station
        self.fuel = fuel
        self.quantity = quantity
        self.constraints = constraints
        self.departure_tick = departure_tick
        self.arrival_tick = arrival_tick
        self.is_timely = is_timely
        self.mitigation_type = mitigation_type
        self.unavoidable_shortage_hours = unavoidable_shortage_hours
        self.impact = impact


# ---------------------------------------------------------------------------
# Constrained Planner Implementation
# ---------------------------------------------------------------------------


class ConstrainedPlanner:
    """Constrained greedy fuel dispatch planner with multi-constraint bookkeeping.

    Implements optimal greedy allocation across at-risk retail stations:
    - Sorts at-risk demands by urgency score.
    - Evaluates all available routes and active depots.
    - Applies the 5-constraint minimum rule.
    - Performs feasibility and timing analysis against stockout projections.
    - Updates shared capacity state (depot dispatch, depot stock, station tank space)
      to prevent over-allocation or post-arrival tank overflow.
    - Ranks candidate options by simulated impact and retains top choice + alternatives.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        risk_engine: RiskEngine | None = None,
        twin_evaluator: TwinEvaluatorProtocol | None = None,
        fallback_planner: Any = None,
        resilience_manager: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.risk_engine = risk_engine or RiskEngine(settings=self.settings)
        self.twin_evaluator = twin_evaluator or HeuristicTwinEvaluator()
        self._heuristic_twin = HeuristicTwinEvaluator()
        self.resilience_manager = resilience_manager

        if fallback_planner is not None:
            self.fallback_planner = fallback_planner
        else:
            from app.intelligence.fallback import FallbackPlanner

            self.fallback_planner = FallbackPlanner(settings=self.settings)

    def generate_plan(
        self,
        snapshot: SimulatorSnapshot,
        fleet_risk: FleetRiskAssessment | None = None,
        depot_reserve_liters: float | None = None,
        min_shipment_liters: float | None = None,
        target_fill_ratio: float | None = None,
        include_watch: bool = True,
        force_fallback: bool = False,
    ) -> DispatchPlan:
        """Generate a complete constrained dispatch plan for the current simulation state."""
        # Check if model degradation or force_fallback necessitates fallback heuristic
        is_model_failed = self.resilience_manager is not None and getattr(
            self.resilience_manager, "is_model_failed", False
        )
        if force_fallback or is_model_failed:
            logger.warning(
                "Model degradation detected (or force_fallback=True). "
                "Delegating dispatch planning to FallbackPlanner."
            )
            return self.fallback_planner.generate_plan(
                snapshot=snapshot,
                fallback_reason=(
                    "Intelligence models offline/degraded; active fallback policy triggered"
                ),
            )

        current_tick = snapshot.instance.tick
        tick_minutes = snapshot.instance.tick_minutes
        plan_id = f"plan-t{current_tick}-{uuid.uuid4().hex[:8]}"

        reserve = (
            depot_reserve_liters
            if depot_reserve_liters is not None
            else self.settings.planner_depot_reserve_liters
        )
        min_shipment = (
            min_shipment_liters
            if min_shipment_liters is not None
            else self.settings.planner_min_shipment_liters
        )
        target_fill = (
            target_fill_ratio
            if target_fill_ratio is not None
            else self.settings.planner_target_fill_ratio
        )

        # 1. Obtain or compute fleet-wide risk assessment with fail-safe guard
        if fleet_risk is None:
            try:
                fleet_risk = self.risk_engine.assess_fleet_risk(
                    snapshot=snapshot,
                    horizon_ticks=self.settings.risk_horizon_ticks,
                )
            except Exception as exc:
                logger.warning(
                    "Fleet risk assessment failed in ConstrainedPlanner (%s: %s). "
                    "Falling back to emergency policy.",
                    type(exc).__name__,
                    exc,
                )
                if self.resilience_manager:
                    self.resilience_manager.record_forecaster_failure(str(exc))
                return self.fallback_planner.generate_plan(
                    snapshot=snapshot,
                    fallback_reason=f"Risk engine/forecaster failed: {exc}",
                )

        # 2. Build index maps for fast lookup
        depot_map: dict[str, Depot] = {d.id: d for d in snapshot.depots}

        # 3. Initialize shared bookkeeping state
        # A. Shared Depot Dispatch Capacity per tick
        # Deduct existing PENDING allocations departing at current_tick + 1
        initial_dispatch_cap: dict[str, float] = {}
        committed_dispatch: dict[str, float] = {d.id: 0.0 for d in snapshot.depots}
        for alloc in snapshot.allocations:
            if alloc.status == AllocationStatus.PENDING:
                dep_tick = (
                    alloc.departure_tick if alloc.departure_tick is not None else current_tick + 1
                )
                if dep_tick == current_tick + 1 and alloc.source_depot_id in committed_dispatch:
                    committed_dispatch[alloc.source_depot_id] += alloc.quantity

        remaining_dispatch: dict[str, float] = {}
        for depot in snapshot.depots:
            initial_dispatch_cap[depot.id] = depot.dispatch_capacity_per_tick
            remaining_dispatch[depot.id] = max(
                0.0, depot.dispatch_capacity_per_tick - committed_dispatch[depot.id]
            )

        # B. Shared Depot Inventory per fuel
        # Deduct operational reserve and existing PENDING allocations from this depot
        initial_depot_inv: dict[tuple[str, FuelType], float] = {}
        committed_depot_inv: dict[tuple[str, FuelType], float] = {}
        for depot in snapshot.depots:
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                key = (depot.id, fuel)
                initial_depot_inv[key] = depot.inventory[fuel]
                committed_depot_inv[key] = 0.0

        for alloc in snapshot.allocations:
            if alloc.status == AllocationStatus.PENDING:
                k = (alloc.source_depot_id, alloc.fuel_type)
                if k in committed_depot_inv:
                    committed_depot_inv[k] += alloc.quantity

        remaining_depot_inv: dict[tuple[str, FuelType], float] = {}
        for depot in snapshot.depots:
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                k = (depot.id, fuel)
                available_above_reserve = max(
                    0.0, initial_depot_inv[k] - committed_depot_inv[k] - reserve
                )
                remaining_depot_inv[k] = available_above_reserve

        # C. Shared Station Effective Free Space (Ullage)
        # Deduct in-transit and valid pending allocations to prevent overflow
        in_transit_by_station_fuel: dict[tuple[str, FuelType], float] = {}
        for station in snapshot.stations:
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                in_transit_by_station_fuel[(station.id, fuel)] = 0.0

        for alloc in snapshot.allocations:
            if alloc.status in (AllocationStatus.IN_TRANSIT, AllocationStatus.PENDING):
                route = next((r for r in snapshot.routes if r.id == alloc.route_id), None)
                # Ignore pending on disrupted routes as established in Day 1 empirical notes
                if (
                    route
                    and route.status == RouteStatus.DISRUPTED
                    and alloc.status == AllocationStatus.PENDING
                ):
                    continue
                k = (alloc.destination_station_id, alloc.fuel_type)
                if k in in_transit_by_station_fuel:
                    in_transit_by_station_fuel[k] += alloc.quantity

        initial_station_free: dict[tuple[str, FuelType], float] = {}
        remaining_station_free: dict[tuple[str, FuelType], float] = {}
        for station in snapshot.stations:
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                k = (station.id, fuel)
                in_trans = in_transit_by_station_fuel[k]
                free_space = station.effective_free_capacity(fuel, in_transit_liters=in_trans)
                initial_station_free[k] = free_space
                remaining_station_free[k] = free_space

        # 4. Identify and Order At-Risk Targets
        # Each target is a (station, fuel, StationFuelRisk, urgency_score)
        targets: list[tuple[Station, FuelType, StationFuelRisk, float]] = []

        for station in snapshot.stations:
            # Skip completely closed stations
            if station.status == StationStatus.CLOSED:
                continue

            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                st_risks = fleet_risk.station_risks.get(station.id, {})
                risk = st_risks.get(fuel.value)
                if risk is None:
                    continue

                # Qualifying at-risk criteria
                is_at_risk = (
                    risk.severity == RiskSeverity.CRITICAL
                    or (include_watch and risk.severity == RiskSeverity.WATCH)
                    or risk.hours_to_stockout is not None
                    or risk.current_fill_ratio < self.settings.risk_watch_fill_ratio
                )

                if is_at_risk:
                    # Calculate or use urgency score
                    urgency = calculate_urgency_score(
                        severity=risk.severity,
                        hours_to_stockout=risk.hours_to_stockout,
                        current_fill_ratio=risk.current_fill_ratio,
                        is_demand_spike=risk.drivers.is_demand_spike,
                    )
                    targets.append((station, fuel, risk, urgency))

        # Sort targets strictly by urgency score descending
        # Ties broken by earliest stockout hours, then total unmet demand
        targets.sort(
            key=lambda t: (
                t[3],  # urgency_score descending
                -(
                    t[2].hours_to_stockout if t[2].hours_to_stockout is not None else 999.0
                ),  # earlier stockout first
                t[2].total_unmet_liters,
            ),
            reverse=True,
        )

        planned_actions: list[PlanAction] = []
        unmet_targets: list[UnmetTarget] = []
        depot_dispatch_allocated: dict[str, float] = {d.id: 0.0 for d in snapshot.depots}
        depot_inv_allocated: dict[tuple[str, FuelType], float] = {
            (d.id, f): 0.0
            for d in snapshot.depots
            for f in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE)
        }
        station_ullage_allocated: dict[tuple[str, FuelType], float] = {
            (s.id, f): 0.0
            for s in snapshot.stations
            for f in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE)
        }

        # 5. Greedy Allocation Loop over Ordered Targets
        for station, fuel, risk, urgency in targets:
            station_key = (station.id, fuel)

            # Check station available free space
            current_free_ullage = remaining_station_free[station_key]
            if current_free_ullage < min_shipment:
                unmet_targets.append(
                    UnmetTarget(
                        station_id=station.id,
                        station_name=station.name,
                        fuel_type=fuel,
                        severity=risk.severity,
                        urgency_score=urgency,
                        hours_to_stockout=risk.hours_to_stockout,
                        reason_code="STATION_FULL",
                        explanation=(
                            f"Station {station.name} tank ullage ({current_free_ullage:,.0f} L) "
                            f"below minimum shipment threshold ({min_shipment:,.0f} L)."
                        ),
                    )
                )
                continue

            # Candidate route filtering:
            # - route ends at station
            # - route is AVAILABLE
            candidate_routes = [
                r
                for r in snapshot.routes
                if r.destination_station_id == station.id and r.status == RouteStatus.AVAILABLE
            ]

            if not candidate_routes:
                all_connecting = [
                    r for r in snapshot.routes if r.destination_station_id == station.id
                ]
                disrupted = [r for r in all_connecting if r.status == RouteStatus.DISRUPTED]
                code = (
                    "ALL_ROUTES_DISRUPTED"
                    if (all_connecting and len(disrupted) == len(all_connecting))
                    else "NO_AVAILABLE_ROUTES"
                )
                unmet_targets.append(
                    UnmetTarget(
                        station_id=station.id,
                        station_name=station.name,
                        fuel_type=fuel,
                        severity=risk.severity,
                        urgency_score=urgency,
                        hours_to_stockout=risk.hours_to_stockout,
                        reason_code=code,
                        explanation=(
                            f"No available routes connect to {station.name}. "
                            f"{len(disrupted)} of {len(all_connecting)} routes are DISRUPTED."
                        ),
                    )
                )
                continue

            # Evaluate each eligible route & source depot
            evaluated_candidates: list[_CandidateEvaluation] = []
            route_rejection_reasons: list[str] = []

            for route in candidate_routes:
                depot = depot_map.get(route.source_depot_id)
                if not depot:
                    continue

                # Depot status check: must be OPEN or CONSTRAINED
                if depot.status not in (
                    DepotStatus.OPEN,
                    DepotStatus.CONSTRAINED,
                    "OPEN",
                    "CONSTRAINED",
                ):
                    route_rejection_reasons.append(
                        f"Route {route.id}: Depot {depot.name} is {depot.status}."
                    )
                    continue

                depot_key = (depot.id, fuel)
                avail_depot_stock = remaining_depot_inv[depot_key]
                avail_dispatch_cap = remaining_dispatch[depot.id]

                # Check if depot has any stock or dispatch capacity
                if avail_depot_stock < min_shipment:
                    route_rejection_reasons.append(
                        f"Route {route.id}: Depot {depot.name} has insufficient {fuel.value} "
                        f"above reserve ({avail_depot_stock:,.0f} L < {min_shipment:,.0f} L)."
                    )
                    continue

                if avail_dispatch_cap < min_shipment:
                    route_rejection_reasons.append(
                        f"Route {route.id}: Depot {depot.name} dispatch capacity exhausted "
                        f"({avail_dispatch_cap:,.0f} L remaining < {min_shipment:,.0f} L)."
                    )
                    continue

                # Compute Amount Needed:
                # 1. Volume to cover projected unmet customer demand
                unmet_deficit = risk.total_unmet_liters
                # 2. Volume to restore tank to nominal safety target fill ratio (e.g. 85%)
                target_volume = station.capacity[fuel] * target_fill
                target_deficit = max(0.0, target_volume - station.inventory[fuel])

                amount_needed = max(unmet_deficit, target_deficit)

                if amount_needed < min_shipment:
                    # Station already near target fill level
                    route_rejection_reasons.append(
                        f"Route {route.id}: Calculated fuel requirement ({amount_needed:,.0f} L) "
                        f"below minimum threshold."
                    )
                    continue

                # 5 Simultaneous Physical & Operational Constraints:
                c_route_max = route.max_shipment
                c_depot_stock = avail_depot_stock
                c_dispatch_cap = avail_dispatch_cap
                c_station_ullage = current_free_ullage
                c_amount_needed = amount_needed

                allocated_qty = min(
                    c_route_max,
                    c_depot_stock,
                    c_dispatch_cap,
                    c_station_ullage,
                    c_amount_needed,
                )

                if allocated_qty < min_shipment:
                    route_rejection_reasons.append(
                        f"Route {route.id}: Constrained allocation ({allocated_qty:,.0f} L) "
                        f"below minimum threshold."
                    )
                    continue

                # Identify the bottleneck constraint
                if allocated_qty == c_route_max:
                    bottleneck = ConstraintType.ROUTE_MAX_SHIPMENT
                elif allocated_qty == c_depot_stock:
                    bottleneck = ConstraintType.DEPOT_INVENTORY
                elif allocated_qty == c_dispatch_cap:
                    bottleneck = ConstraintType.DEPOT_DISPATCH_CAPACITY
                elif allocated_qty == c_station_ullage:
                    bottleneck = ConstraintType.STATION_ULLAGE
                else:
                    bottleneck = ConstraintType.AMOUNT_NEEDED

                applied_constraints = AppliedConstraints(
                    route_max_shipment=round(c_route_max, 2),
                    depot_available_inventory=round(c_depot_stock, 2),
                    depot_remaining_dispatch_capacity=round(c_dispatch_cap, 2),
                    station_effective_free_space=round(c_station_ullage, 2),
                    amount_needed=round(c_amount_needed, 2),
                    bottleneck_constraint=bottleneck,
                    allocated_quantity=round(allocated_qty, 2),
                )

                # Timing & Feasibility Analysis
                departure_tick = current_tick + 1
                arrival_tick = departure_tick + route.transit_ticks

                if risk.first_stockout_tick is not None:
                    if arrival_tick <= risk.first_stockout_tick:
                        is_timely = True
                        mitigation_type = MitigationType.FULL_PREVENTION
                        unavoidable_hours: float | None = 0.0
                    else:
                        is_timely = False
                        mitigation_type = MitigationType.PARTIAL_MITIGATION
                        ticks_late = arrival_tick - risk.first_stockout_tick
                        unavoidable_hours = round(ticks_late * tick_minutes / 60.0, 2)
                else:
                    is_timely = True
                    mitigation_type = MitigationType.PREVENTATIVE_RESTOCK
                    unavoidable_hours = None

                # Twin / Heuristic Benefit Evaluation with defensive fallback
                try:
                    impact = self.twin_evaluator.evaluate(
                        candidate_quantity=allocated_qty,
                        route=route,
                        station=station,
                        fuel=fuel,
                        snapshot=snapshot,
                        risk=risk,
                        departure_tick=departure_tick,
                        arrival_tick=arrival_tick,
                        is_timely=is_timely,
                    )
                except Exception as exc:
                    logger.warning(
                        "Twin evaluation failed for route %s to %s (%s: %s). "
                        "Falling back to heuristic evaluation.",
                        route.id,
                        station.id,
                        type(exc).__name__,
                        exc,
                    )
                    if self.resilience_manager:
                        self.resilience_manager.record_twin_failure(str(exc))
                    impact = self._heuristic_twin.evaluate(
                        candidate_quantity=allocated_qty,
                        route=route,
                        station=station,
                        fuel=fuel,
                        snapshot=snapshot,
                        risk=risk,
                        departure_tick=departure_tick,
                        arrival_tick=arrival_tick,
                        is_timely=is_timely,
                    )

                evaluated_candidates.append(
                    _CandidateEvaluation(
                        route=route,
                        depot=depot,
                        station=station,
                        fuel=fuel,
                        quantity=allocated_qty,
                        constraints=applied_constraints,
                        departure_tick=departure_tick,
                        arrival_tick=arrival_tick,
                        is_timely=is_timely,
                        mitigation_type=mitigation_type,
                        unavoidable_shortage_hours=unavoidable_hours,
                        impact=impact,
                    )
                )

            if not evaluated_candidates:
                unmet_targets.append(
                    UnmetTarget(
                        station_id=station.id,
                        station_name=station.name,
                        fuel_type=fuel,
                        severity=risk.severity,
                        urgency_score=urgency,
                        hours_to_stockout=risk.hours_to_stockout,
                        reason_code="DEPOT_OR_ROUTE_CONSTRAINED",
                        explanation=(
                            f"Could not allocate {fuel.value} to {station.name}. "
                            + (
                                "; ".join(route_rejection_reasons[:2])
                                if route_rejection_reasons
                                else "All candidate routes constrained."
                            )
                        ),
                    )
                )
                continue

            # Rank candidates by impact score descending
            # In case of tie, prefer timely arrival, then higher quantity, then shorter transit
            evaluated_candidates.sort(
                key=lambda c: (
                    c.impact.score,
                    1 if c.is_timely else 0,
                    c.quantity,
                    -c.route.transit_ticks,
                ),
                reverse=True,
            )

            # Pick top choice as primary action
            primary = evaluated_candidates[0]

            # Up to next 2 choices become alternatives
            alternatives: list[PlanActionAlternative] = []
            for alt in evaluated_candidates[1:3]:
                if alt.impact.score < primary.impact.score:
                    diff = primary.impact.score - alt.impact.score
                    reason = f"Lower simulated risk reduction (-{diff:.1f} score pts)"
                elif alt.route.transit_ticks > primary.route.transit_ticks:
                    reason = (
                        f"Slower transit ({alt.route.transit_ticks} vs "
                        f"{primary.route.transit_ticks} ticks)"
                    )
                elif not alt.is_timely and primary.is_timely:
                    reason = "Arrives after projected stockout"
                elif alt.quantity < primary.quantity:
                    reason = (
                        f"Smaller dispatch volume ({alt.quantity:,.0f} L vs "
                        f"{primary.quantity:,.0f} L)"
                    )
                else:
                    reason = "Secondary candidate option"

                alternatives.append(
                    PlanActionAlternative(
                        route_id=alt.route.id,
                        source_depot_id=alt.depot.id,
                        source_depot_name=alt.depot.name,
                        quantity_liters=round(alt.quantity, 2),
                        departure_tick=alt.departure_tick,
                        arrival_tick=alt.arrival_tick,
                        transit_ticks=alt.route.transit_ticks,
                        is_timely=alt.is_timely,
                        mitigation_type=alt.mitigation_type,
                        bottleneck_constraint=alt.constraints.bottleneck_constraint,
                        score=round(alt.impact.score, 2),
                        reason_not_chosen=reason,
                    )
                )

            # Deduct allocated volume from shared bookkeeping
            chosen_qty = primary.quantity
            chosen_depot_key = (primary.depot.id, fuel)

            remaining_dispatch[primary.depot.id] = max(
                0.0, remaining_dispatch[primary.depot.id] - chosen_qty
            )
            remaining_depot_inv[chosen_depot_key] = max(
                0.0, remaining_depot_inv[chosen_depot_key] - chosen_qty
            )
            remaining_station_free[station_key] = max(
                0.0, remaining_station_free[station_key] - chosen_qty
            )

            depot_dispatch_allocated[primary.depot.id] += chosen_qty
            depot_inv_allocated[chosen_depot_key] += chosen_qty
            station_ullage_allocated[station_key] += chosen_qty

            # Compose clear human-readable narrative
            if primary.mitigation_type == MitigationType.FULL_PREVENTION:
                narrative = (
                    f"Dispatch {chosen_qty:,.0f} L of {fuel.value} from {primary.depot.name} "
                    f"to {station.name} via {primary.route.id}. Departs tick "
                    f"{primary.departure_tick}, arrives tick {primary.arrival_tick} before "
                    f"stockout (tick {risk.first_stockout_tick}). "
                    f"Bottleneck: {primary.constraints.bottleneck_constraint.value}. "
                    f"Adds {primary.impact.hours_of_cover_added:.1f}h runway."
                )
            elif primary.mitigation_type == MitigationType.PARTIAL_MITIGATION:
                narrative = (
                    f"Dispatch {chosen_qty:,.0f} L of {fuel.value} from {primary.depot.name} "
                    f"to {station.name} via {primary.route.id}. Departs tick "
                    f"{primary.departure_tick}, arrives tick {primary.arrival_tick} "
                    f"({primary.unavoidable_shortage_hours:.1f}h after stockout). "
                    f"Fastest physical route terminates stockout. "
                    f"Bottleneck: {primary.constraints.bottleneck_constraint.value}."
                )
            else:
                narrative = (
                    f"Preventative restock: Dispatch {chosen_qty:,.0f} L of {fuel.value} from "
                    f"{primary.depot.name} to {station.name} via {primary.route.id}. "
                    f"Arrives tick {primary.arrival_tick}, maintaining safety buffer. "
                    f"Bottleneck: {primary.constraints.bottleneck_constraint.value}."
                )

            action = PlanAction(
                action_id=f"act-{station.id}-{fuel.value.lower()}-{uuid.uuid4().hex[:6]}",
                station_id=station.id,
                station_name=station.name,
                fuel_type=fuel,
                urgency_score=round(urgency, 2),
                severity=risk.severity,
                route_id=primary.route.id,
                source_depot_id=primary.depot.id,
                source_depot_name=primary.depot.name,
                quantity_liters=round(chosen_qty, 2),
                departure_tick=primary.departure_tick,
                arrival_tick=primary.arrival_tick,
                transit_ticks=primary.route.transit_ticks,
                is_timely=primary.is_timely,
                mitigation_type=primary.mitigation_type,
                unavoidable_shortage_hours=primary.unavoidable_shortage_hours,
                constraints=primary.constraints,
                impact=primary.impact,
                alternatives=alternatives,
                explanation=narrative,
            )
            planned_actions.append(action)

        # 6. Compile Shared Capacity Audit Summary
        dispatch_util: dict[str, dict[str, float]] = {}
        for d in snapshot.depots:
            dispatch_util[d.id] = {
                "initial_capacity": round(initial_dispatch_cap[d.id], 2),
                "committed_prior": round(committed_dispatch[d.id], 2),
                "allocated_this_plan": round(depot_dispatch_allocated[d.id], 2),
                "remaining_capacity": round(remaining_dispatch[d.id], 2),
            }

        inv_util: dict[str, dict[str, dict[str, float]]] = {}
        for d in snapshot.depots:
            inv_util[d.id] = {}
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                k = (d.id, fuel)
                inv_util[d.id][fuel.value] = {
                    "initial_inventory": round(initial_depot_inv[k], 2),
                    "operational_reserve": round(reserve, 2),
                    "committed_prior": round(committed_depot_inv[k], 2),
                    "allocated_this_plan": round(depot_inv_allocated[k], 2),
                    "remaining_available": round(remaining_depot_inv[k], 2),
                }

        ullage_util: dict[str, dict[str, dict[str, float]]] = {}
        for s in snapshot.stations:
            ullage_util[s.id] = {}
            for fuel in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                k = (s.id, fuel)
                ullage_util[s.id][fuel.value] = {
                    "tank_capacity": round(s.capacity[fuel], 2),
                    "current_inventory": round(s.inventory[fuel], 2),
                    "in_transit_committed": round(in_transit_by_station_fuel[k], 2),
                    "allocated_this_plan": round(station_ullage_allocated[k], 2),
                    "remaining_free_space": round(remaining_station_free[k], 2),
                }

        shared_summary = SharedCapacitySummary(
            depot_dispatch_utilization=dispatch_util,
            depot_inventory_utilization=inv_util,
            station_ullage_utilization=ullage_util,
        )

        total_liters = sum(a.quantity_liters for a in planned_actions)
        summary_text = (
            f"Plan {plan_id} (tick {current_tick}): Generated {len(planned_actions)} allocation(s) "
            f"totaling {total_liters:,.0f} L across {len(targets)} at-risk target(s). "
            f"{len(unmet_targets)} target(s) could not be served due to network constraints."
        )

        return DispatchPlan(
            plan_id=plan_id,
            created_tick=current_tick,
            total_actions=len(planned_actions),
            total_liters_dispatched=round(total_liters, 2),
            actions=planned_actions,
            unmet_targets=unmet_targets,
            shared_capacity_summary=shared_summary,
            summary=summary_text,
        )
