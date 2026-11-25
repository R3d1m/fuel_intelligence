"""Offline Policy Evaluation Harness for the Fuel Intelligence Platform.

Runs controlled, reproducible A/B experiments across standardized scenarios
and policies against the BUP Fuel Supply Simulator (using /admin endpoints):
- Policies:
  1. `no_action`: Baseline with zero replenishment dispatches.
  2. `naive`: Rule-based refill when tank < 30% from the nearest available depot.
  3. `fuel_intelligence`: Full ConstrainedPlanner + DemandForecaster + RiskEngine + DigitalTwin.
- Scenarios:
  1. `normal_day`: Standard baseline demand without exogenous shocks.
  2. `demand_spike`: 3.5x demand surge on a primary urban station.
  3. `route_disruption`: Primary corridor disabled mid-horizon.
  4. `shipment_delay`: Supply/transit delay injected across the network.
  5. `combined_crisis`: Simultaneous demand spike, route disruption, and shipment delay.

EVALUATION MODE ONLY:
This script auto-approves policy-generated allocations directly against the simulator
to measure closed-loop policy performance. Production operation requires human-in-the-loop
approval via DecisionService.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# Ensure backend package is importable when invoked from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import httpx
from app.adapters.errors import SimulatorError
from app.adapters.simulator_client import SimulatorClient
from app.config import Settings, get_settings
from app.domain.models import (
    AllocationCreate,
    DemandHistoryRecord,
    DepotStatus,
    FuelType,
    RouteStatus,
    SimulatorMetrics,
    SimulatorSnapshot,
    StationStatus,
)
from app.intelligence.fallback import FallbackPlanner
from app.intelligence.forecast import DemandForecaster
from app.intelligence.planner import ConstrainedPlanner
from app.intelligence.risk import RiskEngine
from app.intelligence.twin import DigitalTwin

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("evaluate")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioEventSpec:
    """Event to inject into the simulator for a scenario."""

    event_type: str
    start_tick: int
    duration_ticks: int
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ScenarioSpec:
    """Definition of an evaluation scenario."""

    id: str
    name: str
    description: str
    ticks: int = 96


@dataclass
class EvaluationResult:
    """Recorded performance metrics for a single (scenario, policy) run."""

    scenario_id: str
    scenario_name: str
    policy_id: str
    policy_name: str
    seed: int
    ticks_executed: int
    service_level_pct: float
    unmet_demand_liters: float
    allocation_failures: int
    station_hours_unmet: float
    allocations_submitted: int
    submission_errors: int


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


class EvaluationPolicy(Protocol):
    """Interface for dispatch policies evaluated by the harness."""

    policy_id: str
    policy_name: str

    def decide(
        self,
        snapshot: SimulatorSnapshot,
        demand_history: Sequence[DemandHistoryRecord],
        scenario_id: str,
    ) -> list[AllocationCreate]:
        """Return allocation requests to submit for the current tick."""
        ...


class NoActionPolicy:
    """Baseline policy that never dispatches fuel."""

    policy_id = "no_action"
    policy_name = "No-Action"

    def decide(
        self,
        snapshot: SimulatorSnapshot,
        demand_history: Sequence[DemandHistoryRecord],
        scenario_id: str,
    ) -> list[AllocationCreate]:
        return []


class NaivePolicy:
    """Baseline heuristic: refill when tank < 30% from the nearest available depot."""

    policy_id = "naive"
    policy_name = "Naive (Refill < 30%)"

    def __init__(self, threshold_ratio: float = 0.30) -> None:
        self.threshold_ratio = threshold_ratio

    def decide(
        self,
        snapshot: SimulatorSnapshot,
        demand_history: Sequence[DemandHistoryRecord],
        scenario_id: str,
    ) -> list[AllocationCreate]:
        allocations: list[AllocationCreate] = []
        tick = snapshot.instance.tick

        depot_map = {d.id: d for d in snapshot.depots}
        depot_inv_rem: dict[str, dict[FuelType, float]] = {
            d.id: {f: float(d.inventory[f]) for f in FuelType} for d in snapshot.depots
        }
        depot_disp_rem: dict[str, float] = {
            d.id: float(d.dispatch_capacity_per_tick) for d in snapshot.depots
        }

        for station in snapshot.stations:
            if station.status in (StationStatus.CLOSED, StationStatus.OUTAGE):
                continue

            for fuel in FuelType:
                cap = float(station.capacity[fuel])
                inv = float(station.inventory[fuel])
                if cap <= 0.0 or (inv / cap) >= self.threshold_ratio:
                    continue

                needed = max(0.0, cap - inv)
                if needed <= 0.0:
                    continue

                # Find nearest available route (minimum transit_ticks)
                candidate_routes = [
                    r
                    for r in snapshot.routes
                    if r.destination_station_id == station.id
                    and r.status != RouteStatus.DISRUPTED
                    and r.source_depot_id in depot_map
                    and depot_map[r.source_depot_id].status != DepotStatus.CLOSED
                ]
                candidate_routes.sort(key=lambda r: (r.transit_ticks, r.id))

                for route in candidate_routes:
                    depot_id = route.source_depot_id
                    avail_inv = depot_inv_rem[depot_id][fuel]
                    avail_disp = depot_disp_rem[depot_id]
                    qty = min(needed, float(route.max_shipment), avail_inv, avail_disp)
                    if qty <= 1e-6:
                        continue

                    allocations.append(
                        AllocationCreate(
                            idempotency_key=(
                                f"eval-{scenario_id}-{self.policy_id}-t{tick}-"
                                f"{station.id}-{fuel.value}-{len(allocations)}"
                            ),
                            source_depot_id=depot_id,
                            destination_station_id=station.id,
                            route_id=route.id,
                            fuel_type=fuel,
                            quantity=round(qty, 2),
                        )
                    )
                    depot_inv_rem[depot_id][fuel] -= qty
                    depot_disp_rem[depot_id] -= qty
                    break

        return allocations


class FuelIntelligencePolicy:
    """Platform policy using DemandForecaster, RiskEngine, DigitalTwin, and ConstrainedPlanner."""

    policy_id = "fuel_intelligence"
    policy_name = "Fuel Intelligence (Ours)"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.forecaster = DemandForecaster(settings=self.settings)
        self.risk_engine = RiskEngine(settings=self.settings, forecaster=self.forecaster)
        self.twin = DigitalTwin(settings=self.settings, forecaster=self.forecaster)
        self.fallback = FallbackPlanner(settings=self.settings)
        self.planner = ConstrainedPlanner(
            settings=self.settings,
            risk_engine=self.risk_engine,
            twin_evaluator=self.twin,
            fallback_planner=self.fallback,
        )

    def decide(
        self,
        snapshot: SimulatorSnapshot,
        demand_history: Sequence[DemandHistoryRecord],
        scenario_id: str,
    ) -> list[AllocationCreate]:
        fleet_risk = self.risk_engine.assess_fleet_risk(
            snapshot=snapshot,
            demand_history=demand_history,
            horizon_ticks=self.settings.risk_horizon_ticks,
        )
        plan = self.planner.generate_plan(snapshot=snapshot, fleet_risk=fleet_risk)

        tick = snapshot.instance.tick
        allocations: list[AllocationCreate] = []
        for idx, action in enumerate(plan.actions):
            if action.quantity_liters <= 0.0:
                continue
            allocations.append(
                AllocationCreate(
                    idempotency_key=(
                        f"eval-{scenario_id}-{self.policy_id}-t{tick}-"
                        f"{action.station_id}-{action.fuel_type.value}-{idx}"
                    ),
                    source_depot_id=action.source_depot_id,
                    destination_station_id=action.station_id,
                    route_id=action.route_id,
                    fuel_type=action.fuel_type,
                    quantity=round(action.quantity_liters, 2),
                )
            )
        return allocations


# ---------------------------------------------------------------------------
# Scenarios & Event Resolution
# ---------------------------------------------------------------------------

DEFAULT_SCENARIOS: list[ScenarioSpec] = [
    ScenarioSpec(
        id="normal_day",
        name="Normal Day",
        description="Baseline demand with no exogenous disruptions.",
    ),
    ScenarioSpec(
        id="demand_spike",
        name="Demand Spike",
        description="3.5x demand surge on primary urban station.",
    ),
    ScenarioSpec(
        id="route_disruption",
        name="Route Disruption",
        description="Primary transit corridor disabled mid-horizon.",
    ),
    ScenarioSpec(
        id="shipment_delay",
        name="Shipment Delay",
        description="Transit/supply delay of 3 ticks across active shipments.",
    ),
    ScenarioSpec(
        id="combined_crisis",
        name="Combined Crisis",
        description="Simultaneous demand spike, route disruption, and shipment delay.",
    ),
]


def build_scenario_events(
    scenario_id: str,
    snapshot: SimulatorSnapshot,
) -> list[ScenarioEventSpec]:
    """Resolve scenario events against the active network topology."""
    station_ids = [s.id for s in snapshot.stations]
    route_ids = [r.id for r in snapshot.routes]

    target_station = "station-mirpur" if "station-mirpur" in station_ids else (
        station_ids[0] if station_ids else "station-1"
    )
    target_route = "route-gazipur-mirpur" if "route-gazipur-mirpur" in route_ids else (
        route_ids[0] if route_ids else "route-1"
    )

    spike = ScenarioEventSpec(
        event_type="demand_spike",
        start_tick=4,
        duration_ticks=24,
        parameters={"station_ids": [target_station], "multiplier": 3.5},
    )
    disruption = ScenarioEventSpec(
        event_type="route_disruption",
        start_tick=4,
        duration_ticks=24,
        parameters={"route_ids": [target_route]},
    )
    delay = ScenarioEventSpec(
        event_type="shipment_delay",
        start_tick=4,
        duration_ticks=24,
        parameters={"delay_ticks": 3},
    )

    if scenario_id == "normal_day":
        return []
    if scenario_id == "demand_spike":
        return [spike]
    if scenario_id == "route_disruption":
        return [disruption]
    if scenario_id == "shipment_delay":
        return [delay]
    if scenario_id == "combined_crisis":
        return [spike, disruption, delay]

    raise ValueError(f"Unknown scenario_id: {scenario_id}")


# ---------------------------------------------------------------------------
# Metrics & Reporting
# ---------------------------------------------------------------------------


def compute_station_hours_unmet(
    demand_history: Sequence[DemandHistoryRecord],
    tick_minutes: int,
) -> float:
    """Compute total station-hours with unmet demand (> 0 L) across the run."""
    unmet_station_ticks = {
        (rec.station_id, rec.tick) for rec in demand_history if rec.unmet_liters > 1e-6
    }
    return round(len(unmet_station_ticks) * (max(1, tick_minutes) / 60.0), 2)


def format_markdown_table(results: Sequence[EvaluationResult]) -> str:
    """Format evaluation results as a Markdown comparison table."""
    lines = [
        "| Scenario | Policy | Service Level (%) | Unmet Demand (L) | "
        "Allocation Failures | Station-Hours Unmet |",
        "|---|---|---|---|---|---|",
    ]
    for res in results:
        failures = res.allocation_failures + res.submission_errors
        lines.append(
            f"| {res.scenario_name} | {res.policy_name} | {res.service_level_pct:.2f}% | "
            f"{res.unmet_demand_liters:,.1f} | {failures} | {res.station_hours_unmet:.2f} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Harness Runner
# ---------------------------------------------------------------------------


class EvaluationHarness:
    """Executes closed-loop policy evaluations against a simulator endpoint."""

    def __init__(
        self,
        simulator_url: str = "http://localhost:8000",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.simulator_url = simulator_url.rstrip("/")
        self._external_client = http_client is not None
        self._http = http_client or httpx.AsyncClient(base_url=self.simulator_url, timeout=15.0)
        self.sim_client = SimulatorClient(
            base_url=self.simulator_url,
            client=self._http,
            max_retries=1,
            backoff_factor=0.05,
        )

    async def close(self) -> None:
        """Close owned HTTP resources."""
        if not self._external_client:
            await self._http.aclose()

    async def fetch_snapshot(self) -> SimulatorSnapshot:
        """Fetch a consolidated SimulatorSnapshot using SimulatorClient."""
        (
            inst_resp,
            depots_resp,
            stations_resp,
            routes_resp,
            arrivals_resp,
            events_resp,
            allocs_resp,
        ) = await asyncio.gather(
            self.sim_client.get_instance(),
            self.sim_client.get_depots(),
            self.sim_client.get_stations(),
            self.sim_client.get_routes(),
            self.sim_client.get_supply_arrivals(),
            self.sim_client.get_events(),
            self.sim_client.get_allocations(),
        )
        return SimulatorSnapshot(
            instance=inst_resp.data,
            depots=depots_resp.data,
            stations=stations_resp.data,
            routes=routes_resp.data,
            supply_arrivals=arrivals_resp.data,
            events=events_resp.data,
            allocations=allocs_resp.data,
            tick=inst_resp.data.tick,
            sim_time=str(inst_resp.data.sim_time),
            fetched_at=datetime.now(UTC),
            is_stale=inst_resp.is_stale,
        )

    async def reset_and_pause(self) -> SimulatorSnapshot:
        """Clear faults, reset simulator, pause at tick 0, and return initial snapshot."""
        await self.sim_client.admin_clear_faults()
        await self.sim_client.admin_reset()
        await self.sim_client.admin_pause()
        return await self.fetch_snapshot()

    async def inject_events(self, events: Sequence[ScenarioEventSpec]) -> None:
        """Inject scenario events via /admin/events."""
        for ev in events:
            await self.sim_client.admin_inject_event(
                event_type=ev.event_type,
                start_tick=ev.start_tick,
                duration_ticks=ev.duration_ticks,
                parameters=ev.parameters,
            )

    async def evaluate_run(
        self,
        scenario: ScenarioSpec,
        policy: EvaluationPolicy,
        ticks_override: int | None = None,
    ) -> EvaluationResult:
        """Run one (scenario, policy) evaluation loop."""
        ticks = ticks_override if ticks_override is not None else scenario.ticks
        initial_snap = await self.reset_and_pause()
        seed = initial_snap.instance.seed
        tick_minutes = initial_snap.instance.tick_minutes

        events = build_scenario_events(scenario.id, initial_snap)
        if events:
            await self.inject_events(events)

        allocations_submitted = 0
        submission_errors = 0

        for _ in range(ticks):
            await self.sim_client.admin_step()
            snap = await self.fetch_snapshot()
            history_resp = await self.sim_client.get_demand_history(limit=500)

            # EVALUATION MODE ONLY: auto-approve policy allocations without human review
            proposals = policy.decide(
                snapshot=snap,
                demand_history=history_resp.data,
                scenario_id=scenario.id,
            )
            for alloc in proposals:
                try:
                    await self.sim_client.create_allocation(alloc)
                    allocations_submitted += 1
                except SimulatorError as exc:
                    submission_errors += 1
                    logger.debug(
                        "Allocation rejected (%s/%s tick=%d): %s",
                        scenario.id,
                        policy.policy_id,
                        snap.instance.tick,
                        exc,
                    )

        metrics: SimulatorMetrics = (await self.sim_client.get_metrics()).data
        all_history = (await self.sim_client.get_demand_history(limit=2000)).data
        station_hours_unmet = compute_station_hours_unmet(all_history, tick_minutes)

        return EvaluationResult(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            policy_id=policy.policy_id,
            policy_name=policy.policy_name,
            seed=seed,
            ticks_executed=ticks,
            service_level_pct=round(metrics.service_level * 100.0, 2),
            unmet_demand_liters=round(metrics.unmet_demand_liters, 2),
            allocation_failures=metrics.allocation_failures,
            station_hours_unmet=station_hours_unmet,
            allocations_submitted=allocations_submitted,
            submission_errors=submission_errors,
        )

    async def run_suite(
        self,
        scenarios: Sequence[ScenarioSpec],
        policies: Sequence[EvaluationPolicy],
        ticks_override: int | None = None,
    ) -> list[EvaluationResult]:
        """Run all (scenario, policy) pairs and return ordered results."""
        results: list[EvaluationResult] = []
        for scenario in scenarios:
            for policy in policies:
                logger.info(
                    "Running evaluation [scenario=%s, policy=%s] (EVALUATION MODE: auto-approve)",
                    scenario.id,
                    policy.policy_id,
                )
                res = await self.evaluate_run(
                    scenario=scenario,
                    policy=policy,
                    ticks_override=ticks_override,
                )
                results.append(res)
                logger.info(
                    "Completed [%s | %s]: service=%.2f%%, unmet=%.1f L, failures=%d, stn_hrs=%.2f",
                    scenario.id,
                    policy.policy_id,
                    res.service_level_pct,
                    res.unmet_demand_liters,
                    res.allocation_failures + res.submission_errors,
                    res.station_hours_unmet,
                )
        return results


def build_policies(policy_ids: Sequence[str] | None = None) -> list[EvaluationPolicy]:
    """Instantiate requested evaluation policies in canonical order."""
    available: dict[str, EvaluationPolicy] = {
        "no_action": NoActionPolicy(),
        "naive": NaivePolicy(),
        "fuel_intelligence": FuelIntelligencePolicy(),
    }
    if not policy_ids:
        return list(available.values())
    return [available[pid] for pid in policy_ids]


def build_scenarios(scenario_ids: Sequence[str] | None = None) -> list[ScenarioSpec]:
    """Select requested scenarios in canonical order."""
    if not scenario_ids:
        return list(DEFAULT_SCENARIOS)
    by_id = {s.id: s for s in DEFAULT_SCENARIOS}
    return [by_id[sid] for sid in scenario_ids]


async def async_main(args: argparse.Namespace) -> int:
    """Async entrypoint for CLI execution."""
    scenarios = build_scenarios(args.scenarios)
    policies = build_policies(args.policies)

    harness = EvaluationHarness(simulator_url=args.simulator_url)
    try:
        results = await harness.run_suite(
            scenarios=scenarios,
            policies=policies,
            ticks_override=args.ticks,
        )
    finally:
        await harness.close()

    table = format_markdown_table(results)
    print("\n" + table + "\n")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
        logger.info("Saved raw evaluation results to %s", out_path)

    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run controlled A/B policy evaluation against the fuel simulator."
    )
    parser.add_argument(
        "--simulator-url",
        default="http://localhost:8000",
        help="Base URL of the running BUP Fuel Supply Simulator.",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=96,
        help="Number of simulation ticks per scenario (default: 96).",
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=[s.id for s in DEFAULT_SCENARIOS],
        help="Subset of scenarios to run.",
    )
    parser.add_argument(
        "--policies",
        nargs="+",
        choices=["no_action", "naive", "fuel_intelligence"],
        help="Subset of policies to evaluate.",
    )
    parser.add_argument(
        "--output-json",
        help="Optional path to write JSON evaluation results.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
