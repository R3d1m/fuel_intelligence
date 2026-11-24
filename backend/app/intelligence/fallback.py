"""Fallback Rule-Based Dispatch Policy.

Implements a deterministic, safety-oriented heuristic planner for degraded operational modes:
- Triggered when ML forecasting or digital twin simulation throws, fails, or times out.
- Plain Rule Logic:
  1. Any station fuel tank with effective fill ratio < 20% is classified as CRITICAL.
  2. Any station fuel tank with effective fill ratio < 35% is classified as WATCH.
  3. Sorts at-risk tanks by urgency (lowest fill ratio first).
  4. For each at-risk tank, considers all AVAILABLE corridors from OPEN/CONSTRAINED depots.
  5. Computes the largest feasible shipment bounded by:
     min(route_max, depot_stock - reserve, depot_dispatch, tank_space, deficit).
  6. Sets confidence_score = 0.25 (LOW) and mandates needs_human_review = True.
  7. Retains top candidate action and up to 2 runner-up alternatives with explicit constraints.
  8. Enforces shared multi-station capacity bookkeeping across all dispatches.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

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
from app.intelligence.planner import (
    AppliedConstraints,
    CandidateImpact,
    ConstraintType,
    DispatchPlan,
    MitigationType,
    PlanAction,
    PlanActionAlternative,
    SharedCapacitySummary,
    UnmetTarget,
)

logger = logging.getLogger(__name__)


class FallbackPlanner:
    """Deterministic, rule-based emergency planner for degraded fallback modes."""

    def __init__(
        self,
        settings: Settings | None = None,
        critical_fill_ratio: float | None = None,
        watch_fill_ratio: float | None = None,
        depot_reserve_liters: float | None = None,
        min_shipment_liters: float | None = None,
        target_fill_ratio: float | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.critical_fill_ratio = (
            critical_fill_ratio
            if critical_fill_ratio is not None
            else getattr(self.settings, "fallback_critical_fill_ratio", 0.20)
        )
        self.watch_fill_ratio = (
            watch_fill_ratio
            if watch_fill_ratio is not None
            else getattr(self.settings, "fallback_watch_fill_ratio", 0.35)
        )
        self.depot_reserve_liters = (
            depot_reserve_liters
            if depot_reserve_liters is not None
            else self.settings.planner_depot_reserve_liters
        )
        self.min_shipment_liters = (
            min_shipment_liters
            if min_shipment_liters is not None
            else self.settings.planner_min_shipment_liters
        )
        self.target_fill_ratio = (
            target_fill_ratio
            if target_fill_ratio is not None
            else self.settings.planner_target_fill_ratio
        )

    def generate_plan(
        self,
        snapshot: SimulatorSnapshot,
        fallback_reason: str = "Fallback rule policy activated due to degraded models",
    ) -> DispatchPlan:
        """Generate a deterministic, rule-based emergency dispatch plan.

        Args:
            snapshot: Authoritative current or last-known-good network snapshot.
            fallback_reason: Human-readable rationale for why fallback was activated.

        Returns:
            DispatchPlan marked with is_fallback=True, low confidence, and human review mandated.
        """
        current_tick = snapshot.instance.tick
        plan_id = f"fallback-plan-t{current_tick}-{uuid.uuid4().hex[:8]}"
        logger.warning(
            "Generating emergency fallback dispatch plan '%s' (Reason: %s)",
            plan_id,
            fallback_reason,
        )

        depot_map: dict[str, Depot] = {d.id: d for d in snapshot.depots}
        route_map: dict[str, Route] = {r.id: r for r in snapshot.routes}

        # ---------------------------------------------------------------------
        # 1. Evaluate Station Tank Fill Ratios & Identify At-Risk Stocks
        # ---------------------------------------------------------------------
        # Calculate existing in-transit volume per station and fuel
        in_transit_by_target: dict[tuple[str, FuelType], float] = {}
        for alloc in snapshot.allocations:
            if alloc.status in (AllocationStatus.IN_TRANSIT, AllocationStatus.PENDING):
                route = route_map.get(alloc.route_id)
                # Ignore in-transit shipments stranded on disrupted routes
                if route and route.status == RouteStatus.DISRUPTED:
                    continue
                key = (alloc.destination_station_id, alloc.fuel_type)
                in_transit_by_target[key] = in_transit_by_target.get(key, 0.0) + alloc.quantity

        at_risk_targets: list[dict[str, Any]] = []

        for station in snapshot.stations:
            # Skip closed or outage stations
            if station.status in (StationStatus.CLOSED, StationStatus.OUTAGE, "OUTAGE", "CLOSED"):
                continue

            for fuel in FuelType:
                cap = station.capacity.get(fuel, 0.0)
                if cap <= 0.0:
                    continue

                inv = station.inventory.get(fuel, 0.0)
                in_transit = in_transit_by_target.get((station.id, fuel), 0.0)
                effective_inv = inv + in_transit
                fill_ratio = effective_inv / cap

                if fill_ratio < self.critical_fill_ratio:
                    severity = RiskSeverity.CRITICAL
                    urgency_score = 100.0 - (fill_ratio * 100.0)
                elif fill_ratio < self.watch_fill_ratio:
                    severity = RiskSeverity.WATCH
                    urgency_score = 50.0 - (fill_ratio * 100.0)
                else:
                    continue  # Safe inventory level

                at_risk_targets.append(
                    {
                        "station": station,
                        "fuel_type": fuel,
                        "severity": severity,
                        "urgency_score": urgency_score,
                        "current_inv": inv,
                        "effective_inv": effective_inv,
                        "capacity": cap,
                        "fill_ratio": fill_ratio,
                    }
                )

        # Sort at-risk targets by urgency score descending (most depleted tanks first)
        at_risk_targets.sort(key=lambda t: t["urgency_score"], reverse=True)

        # ---------------------------------------------------------------------
        # 2. Shared Bookkeeping Across the Entire Plan
        # ---------------------------------------------------------------------
        # A. Depot Dispatch Capacity: deduct any pending dispatches for next tick
        committed_next_tick: dict[str, float] = {d.id: 0.0 for d in snapshot.depots}
        for alloc in snapshot.allocations:
            if alloc.status == AllocationStatus.PENDING:
                dep_tick = (
                    alloc.departure_tick if alloc.departure_tick is not None else current_tick + 1
                )
                if dep_tick == current_tick + 1 and alloc.source_depot_id in committed_next_tick:
                    committed_next_tick[alloc.source_depot_id] += alloc.quantity

        initial_dispatch_cap: dict[str, float] = {}
        remaining_dispatch: dict[str, float] = {}
        for depot in snapshot.depots:
            initial_dispatch_cap[depot.id] = depot.dispatch_capacity_per_tick
            remaining_dispatch[depot.id] = max(
                0.0, depot.dispatch_capacity_per_tick - committed_next_tick[depot.id]
            )

        # B. Depot Inventory Available Above Reserve
        initial_depot_stock: dict[tuple[str, FuelType], float] = {}
        remaining_depot_stock: dict[tuple[str, FuelType], float] = {}
        for depot in snapshot.depots:
            for fuel in FuelType:
                inv = depot.inventory.get(fuel, 0.0)
                avail = max(0.0, inv - self.depot_reserve_liters)
                initial_depot_stock[(depot.id, fuel)] = avail
                remaining_depot_stock[(depot.id, fuel)] = avail

        # C. Station Effective Tank Ullage (avoiding tank overflow)
        remaining_station_ullage: dict[tuple[str, FuelType], float] = {}
        for station in snapshot.stations:
            # Skip closed or outage stations
            if station.status in (StationStatus.CLOSED, StationStatus.OUTAGE, "OUTAGE", "CLOSED"):
                continue

            for fuel in FuelType:
                cap = station.capacity.get(fuel, 0.0)
                eff_inv = station.inventory.get(fuel, 0.0) + in_transit_by_target.get(
                    (station.id, fuel), 0.0
                )
                free_space = max(0.0, cap - eff_inv)
                remaining_station_ullage[(station.id, fuel)] = free_space

        # ---------------------------------------------------------------------
        # 3. Greedy Feasibility Evaluation & Action Generation
        # ---------------------------------------------------------------------
        actions: list[PlanAction] = []
        unmet_targets: list[UnmetTarget] = []

        for target in at_risk_targets:
            station: Station = target["station"]
            fuel: FuelType = target["fuel_type"]
            severity: RiskSeverity = target["severity"]
            urgency_score: float = target["urgency_score"]
            cap: float = target["capacity"]
            eff_inv: float = target["effective_inv"]
            fill_ratio: float = target["fill_ratio"]

            # Target deficit needed to reach target fill ratio
            target_stock = cap * self.target_fill_ratio
            deficit_needed = max(0.0, target_stock - eff_inv)

            station_ullage = remaining_station_ullage.get((station.id, fuel), 0.0)
            if station_ullage < self.min_shipment_liters:
                unmet_targets.append(
                    UnmetTarget(
                        station_id=station.id,
                        station_name=station.name,
                        fuel_type=fuel,
                        severity=severity,
                        urgency_score=urgency_score,
                        hours_to_stockout=None,
                        reason_code="ULLAGE_INSUFFICIENT",
                        explanation=(
                            "Station tank ullage insufficient for minimum shipment under fallback"
                        ),
                    )
                )
                continue

            # Candidate routes: AVAILABLE, ending at station, source depot OPEN or CONSTRAINED
            candidate_routes = [
                r
                for r in snapshot.routes
                if r.destination_station_id == station.id
                and r.status == RouteStatus.AVAILABLE
                and r.source_depot_id in depot_map
                and depot_map[r.source_depot_id].status
                in (DepotStatus.OPEN, DepotStatus.CONSTRAINED)
            ]

            if not candidate_routes:
                unmet_targets.append(
                    UnmetTarget(
                        station_id=station.id,
                        station_name=station.name,
                        fuel_type=fuel,
                        severity=severity,
                        urgency_score=urgency_score,
                        hours_to_stockout=None,
                        reason_code="NO_AVAILABLE_ROUTE",
                        explanation="No available corridor from an open depot to this station",
                    )
                )
                continue

            evaluated_candidates: list[dict[str, Any]] = []

            for route in candidate_routes:
                depot = depot_map[route.source_depot_id]
                route_max = route.max_shipment
                depot_stock = remaining_depot_stock.get((depot.id, fuel), 0.0)
                depot_dispatch = remaining_dispatch.get(depot.id, 0.0)

                # Compute feasible shipment as the minimum of the 5 binding constraints
                feasible_qty = min(
                    route_max,
                    depot_stock,
                    depot_dispatch,
                    station_ullage,
                    deficit_needed,
                )

                # Determine binding constraint
                if feasible_qty == route_max:
                    bottleneck = ConstraintType.ROUTE_MAX_SHIPMENT
                elif feasible_qty == depot_stock:
                    bottleneck = ConstraintType.DEPOT_STOCK_RESERVE
                elif feasible_qty == depot_dispatch:
                    bottleneck = ConstraintType.DEPOT_DISPATCH_CAPACITY
                elif feasible_qty == station_ullage:
                    bottleneck = ConstraintType.STATION_TANK_SPACE
                else:
                    bottleneck = ConstraintType.DEFICIT_NEEDED

                applied = AppliedConstraints(
                    route_max_shipment=route_max,
                    depot_available_inventory=depot_stock,
                    depot_remaining_dispatch_capacity=depot_dispatch,
                    station_effective_free_space=station_ullage,
                    amount_needed=deficit_needed,
                    bottleneck_constraint=bottleneck,
                    allocated_quantity=feasible_qty,
                )

                evaluated_candidates.append(
                    {
                        "route": route,
                        "depot": depot,
                        "quantity": feasible_qty,
                        "constraints": applied,
                        "bottleneck": bottleneck,
                    }
                )

            # Filter candidates that can ship at least minimum threshold
            valid_candidates = [
                c for c in evaluated_candidates if c["quantity"] >= self.min_shipment_liters
            ]

            if not valid_candidates:
                unmet_targets.append(
                    UnmetTarget(
                        station_id=station.id,
                        station_name=station.name,
                        fuel_type=fuel,
                        severity=severity,
                        urgency_score=urgency_score,
                        hours_to_stockout=None,
                        reason_code="MIN_SHIPMENT_UNMET",
                        explanation=(
                            f"All candidates bounded below minimum shipment threshold "
                            f"({self.min_shipment_liters:.0f} L) by supply bottlenecks"
                        ),
                    )
                )
                continue

            # In fallback mode (no ML twin), rank by:
            # 1. Largest feasible quantity (mitigate maximum deficit immediately)
            # 2. Shortest transit ticks (fastest delivery)
            # 3. Source depot stock depth
            valid_candidates.sort(
                key=lambda c: (
                    c["quantity"],
                    -c["route"].transit_ticks,
                    remaining_depot_stock.get((c["depot"].id, fuel), 0.0),
                ),
                reverse=True,
            )

            chosen = valid_candidates[0]
            chosen_route: Route = chosen["route"]
            chosen_depot: Depot = chosen["depot"]
            chosen_qty: float = chosen["quantity"]
            chosen_constraints: AppliedConstraints = chosen["constraints"]

            # Runner-up alternatives (up to 2)
            alternatives: list[PlanActionAlternative] = []
            for alt in valid_candidates[1:3]:
                alt_route: Route = alt["route"]
                alt_depot: Depot = alt["depot"]
                alt_qty: float = alt["quantity"]
                alt_bottle: ConstraintType = alt["bottleneck"]

                reason = (
                    f"Yields lower volume ({alt_qty:,.0f} L vs {chosen_qty:,.0f} L) "
                    f"bounded by {alt_bottle.value}"
                    if alt_qty < chosen_qty
                    else (
                        f"Higher transit latency ({alt_route.transit_ticks} ticks "
                        f"vs {chosen_route.transit_ticks} ticks)"
                    )
                )

                alternatives.append(
                    PlanActionAlternative(
                        route_id=alt_route.id,
                        source_depot_id=alt_depot.id,
                        source_depot_name=alt_depot.name,
                        quantity_liters=alt_qty,
                        departure_tick=current_tick + 1,
                        arrival_tick=current_tick + 1 + alt_route.transit_ticks,
                        transit_ticks=alt_route.transit_ticks,
                        is_timely=True,
                        mitigation_type=(
                            MitigationType.FULL_PREVENTION
                            if alt_qty >= deficit_needed * 0.9
                            else MitigationType.PARTIAL_MITIGATION
                        ),
                        bottleneck_constraint=alt_bottle,
                        score=alt_qty,
                        reason_not_chosen=reason,
                    )
                )

            mitigation_type = (
                MitigationType.FULL_PREVENTION
                if chosen_qty >= deficit_needed * 0.9
                else MitigationType.PARTIAL_MITIGATION
            )

            p_before = 1.0 if severity == RiskSeverity.CRITICAL else 0.60
            p_after = 0.20 if mitigation_type == MitigationType.FULL_PREVENTION else 0.45

            impact = CandidateImpact(
                expected_unmet_reduction_liters=chosen_qty,
                hours_of_cover_added=3.5,
                stockout_probability_before=p_before,
                stockout_probability_after=p_after,
                score=chosen_qty,
                scoring_details={"fallback_allocation": chosen_qty},
            )

            action_id = f"fallback_act_t{current_tick}_{station.id}_{fuel.value}"
            fill_pct = fill_ratio * 100.0

            action = PlanAction(
                action_id=action_id,
                station_id=station.id,
                station_name=station.name,
                fuel_type=fuel,
                urgency_score=urgency_score,
                severity=severity,
                route_id=chosen_route.id,
                source_depot_id=chosen_depot.id,
                source_depot_name=chosen_depot.name,
                quantity_liters=chosen_qty,
                departure_tick=current_tick + 1,
                arrival_tick=current_tick + 1 + chosen_route.transit_ticks,
                transit_ticks=chosen_route.transit_ticks,
                is_timely=True,
                mitigation_type=mitigation_type,
                unavoidable_shortage_hours=None,
                constraints=chosen_constraints,
                impact=impact,
                alternatives=alternatives,
                explanation=(
                    f"FALLBACK HEURISTIC: Station '{station.name}' {fuel.value} fill ratio is "
                    f"{fill_pct:.1f}% (<{self.critical_fill_ratio * 100:.0f}% threshold). "
                    f"Dispatched largest feasible quantity ({chosen_qty:,.0f} L) from "
                    f"{chosen_depot.name} via {chosen_route.id}. "
                    "Intelligence models offline/degraded; operator review mandated."
                ),
                confidence_score=0.25,
                confidence_level="LOW",
                needs_human_review=True,
                is_fallback=True,
            )
            actions.append(action)

            # Update shared capacity bookkeeping
            remaining_dispatch[chosen_depot.id] = max(
                0.0, remaining_dispatch[chosen_depot.id] - chosen_qty
            )
            remaining_depot_stock[(chosen_depot.id, fuel)] = max(
                0.0, remaining_depot_stock[(chosen_depot.id, fuel)] - chosen_qty
            )
            remaining_station_ullage[(station.id, fuel)] = max(
                0.0, remaining_station_ullage[(station.id, fuel)] - chosen_qty
            )

        # ---------------------------------------------------------------------
        # 4. Construct Shared Capacity Summary
        # ---------------------------------------------------------------------
        total_dispatched = sum(a.quantity_liters for a in actions)
        dispatch_util: dict[str, dict[str, float]] = {}
        for d in snapshot.depots:
            committed = initial_dispatch_cap[d.id] - remaining_dispatch[d.id]
            dispatch_util[d.id] = {
                "initial_capacity": round(initial_dispatch_cap[d.id], 2),
                "committed_prior": round(committed_next_tick.get(d.id, 0.0), 2),
                "allocated_this_plan": round(committed, 2),
                "remaining_capacity": round(remaining_dispatch[d.id], 2),
            }

        inv_util: dict[str, dict[str, dict[str, float]]] = {}
        for d in snapshot.depots:
            inv_util[d.id] = {}
            for f in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                init_inv = d.inventory.get(f, 0.0)
                rem_stock = remaining_depot_stock.get((d.id, f), 0.0)
                alloc = initial_depot_stock.get((d.id, f), 0.0) - rem_stock
                inv_util[d.id][f.value] = {
                    "initial_inventory": round(init_inv, 2),
                    "operational_reserve": round(self.depot_reserve_liters, 2),
                    "committed_prior": 0.0,
                    "allocated_this_plan": round(alloc, 2),
                    "remaining_available": round(rem_stock, 2),
                }

        ullage_util: dict[str, dict[str, dict[str, float]]] = {}
        for s in snapshot.stations:
            ullage_util[s.id] = {}
            for f in (FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE):
                rem_free = remaining_station_ullage.get((s.id, f), 0.0)
                ullage_util[s.id][f.value] = {
                    "tank_capacity": round(s.capacity.get(f, 0.0), 2),
                    "current_inventory": round(s.inventory.get(f, 0.0), 2),
                    "in_transit_committed": round(in_transit_by_target.get((s.id, f), 0.0), 2),
                    "allocated_this_plan": round(
                        max(0.0, (s.capacity.get(f, 0.0) - s.inventory.get(f, 0.0)) - rem_free), 2
                    ),
                    "remaining_free_space": round(rem_free, 2),
                }

        shared_summary = SharedCapacitySummary(
            depot_dispatch_utilization=dispatch_util,
            depot_inventory_utilization=inv_util,
            station_ullage_utilization=ullage_util,
        )

        plan_summary = (
            f"Fallback Rule Plan: Generated {len(actions)} emergency dispatches totaling "
            f"{total_dispatched:,.0f} L across {len(snapshot.stations)} stations. "
            f"Low confidence (0.25); human review mandated on all proposals."
        )

        return DispatchPlan(
            plan_id=plan_id,
            created_tick=current_tick,
            total_actions=len(actions),
            total_liters_dispatched=total_dispatched,
            actions=actions,
            unmet_targets=unmet_targets,
            shared_capacity_summary=shared_summary,
            summary=plan_summary,
            is_fallback=True,
            fallback_reason=fallback_reason,
        )


def generate_fallback_plan(
    snapshot: SimulatorSnapshot,
    settings: Settings | None = None,
    fallback_reason: str = "Fallback rule policy activated",
) -> DispatchPlan:
    """Convenience functional helper to generate a fallback emergency plan."""
    planner = FallbackPlanner(settings=settings)
    return planner.generate_plan(snapshot=snapshot, fallback_reason=fallback_reason)
