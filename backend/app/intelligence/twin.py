"""Monte Carlo Digital Twin Simulation Engine.

Steps inventory forward tick-by-tick using forecast demand plus Gaussian
stochastic noise calibrated from empirical out-of-sample forecast residuals.

Evaluates forward inventory distributions with and without candidate dispatches:
- Vectorized Monte Carlo stepping with NumPy for high-throughput performance.
- Configurable trial count (200..500) and fixed random seeds for byte-identical determinism.
- Outputs stockout probabilities before and after intervention.
- Computes expected unmet liters with empirical confidence intervals (e.g. 95% CI).
- Generates percentile inventory trajectories (P10 conservative, P50 median, P90 optimistic).
- Implements TwinEvaluatorProtocol for drop-in planner integration.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.domain.models import (
    AllocationStatus,
    FuelType,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
)
from app.intelligence.forecast import DemandForecaster, StationFuelForecast
from app.intelligence.planner import CandidateImpact, TwinEvaluatorProtocol
from app.intelligence.risk import StationFuelRisk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


class ConfidenceInterval(BaseModel):
    """Empirical confidence interval derived from Monte Carlo sample percentiles."""

    model_config = ConfigDict(extra="ignore")

    ci_percent: float = Field(
        ge=50.0, lt=100.0, description="Confidence level percentage (e.g. 95.0)"
    )
    lower: float = Field(
        ge=0.0, description="Lower percentile bound (e.g. 2.5th percentile for 95% CI)"
    )
    median: float = Field(ge=0.0, description="50th percentile median point estimate")
    upper: float = Field(
        ge=0.0, description="Upper percentile bound (e.g. 97.5th percentile for 95% CI)"
    )


class ScenarioSimulation(BaseModel):
    """Aggregated statistical outcome of Monte Carlo trials for a single scenario."""

    model_config = ConfigDict(extra="ignore")

    label: str = Field(description="Scenario identifier: baseline vs with_shipment")
    has_shipment: bool = Field(description="Whether a candidate shipment was applied")
    shipment_arrival_tick: int | None = Field(
        None, description="Simulation tick candidate fuel arrives"
    )
    shipment_quantity_liters: float = Field(default=0.0, ge=0.0)
    stockout_probability: float = Field(
        ge=0.0, le=1.0, description="Fraction of Monte Carlo trials experiencing stockout"
    )
    expected_unmet_liters: float = Field(
        ge=0.0, description="Mean unmet customer demand volume in liters across trials"
    )
    unmet_ci: ConfidenceInterval = Field(
        description="Confidence interval for cumulative unmet demand volume"
    )
    min_inventory_ci: ConfidenceInterval = Field(
        description="Confidence interval for minimum inventory level reached across horizon"
    )
    median_trajectory: list[float] = Field(
        default_factory=list, description="Median (P50) inventory level across forward ticks"
    )
    p10_trajectory: list[float] = Field(
        default_factory=list, description="P10 conservative trajectory (lower 10th percentile)"
    )
    p90_trajectory: list[float] = Field(
        default_factory=list, description="P90 optimistic trajectory (upper 90th percentile)"
    )


class TwinSimulationResult(BaseModel):
    """Consolidated comparative digital twin simulation before and after allocation."""

    model_config = ConfigDict(extra="ignore")

    station_id: str
    station_name: str
    fuel_type: FuelType
    current_tick: int
    horizon_ticks: int
    trials_count: int
    random_seed: int
    residual_std: float
    baseline: ScenarioSimulation
    with_shipment: ScenarioSimulation
    stockout_probability_reduction: float = Field(
        ge=0.0,
        le=1.0,
        description="Absolute reduction in stockout probability (P_before - P_after)",
    )
    expected_unmet_reduction_liters: float = Field(
        ge=0.0, description="Expected volume of unmet demand prevented by the shipment"
    )
    relative_risk_reduction_percent: float = Field(
        ge=0.0, description="Percentage of baseline stockout risk eliminated by shipment"
    )
    hours_of_cover_added: float = Field(ge=0.0, description="Hours of operational runway added")
    candidate_score: float = Field(description="Scalar prioritization score for greedy ranking")
    explanation: str = Field(description="Human-readable executive summary of the twin simulation")
    simulated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Digital Twin Implementation
# ---------------------------------------------------------------------------


class DigitalTwin(TwinEvaluatorProtocol):
    """Vectorized Monte Carlo simulation engine modeling retail tank forward states.

    Steps inventory forward tick-by-tick across hundreds of stochastic trials:
    - Ingests demand forecasts and observed backtest residual variance (sigma).
    - Ingests existing in-flight shipments and scheduled supply arrivals.
    - Simulates two scenarios:
      1. Baseline: No intervention (counterfactual).
      2. Treatment: Candidate shipment arriving at destination tick.
    - Accurately captures tank ullage caps (preventing phantom inventory overflow).
    - Derives empirical probability distributions, percentiles, and confidence intervals.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        forecaster: DemandForecaster | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.forecaster = forecaster or DemandForecaster(settings=self.settings)

    def simulate_single_scenario(
        self,
        initial_inventory: float,
        capacity: float,
        base_demand: Sequence[float],
        scheduled_arrivals: dict[int, float],
        horizon_ticks: int,
        trials: int,
        residual_std: float,
        seed: int,
        label: str,
        ci_percent: float = 95.0,
        shipment_arrival_step: int | None = None,
        shipment_quantity: float = 0.0,
    ) -> ScenarioSimulation:
        """Run vectorized Monte Carlo forward simulation for a single scenario."""
        rng = np.random.default_rng(seed)

        # 1. Generate Gaussian noise matrix of shape (trials, horizon_ticks)
        sigma = max(0.5, residual_std)
        noise = rng.normal(loc=0.0, scale=sigma, size=(trials, horizon_ticks))

        # 2. Demand matrix: base forecast per step + random residual noise, clamped >= 0
        base_dem_arr = np.array(base_demand[:horizon_ticks], dtype=np.float64)
        if len(base_dem_arr) < horizon_ticks:
            # Pad with last rate if forecast shorter than horizon
            pad_val = base_dem_arr[-1] if len(base_dem_arr) > 0 else 50.0
            padding = np.full(horizon_ticks - len(base_dem_arr), pad_val)
            base_dem_arr = np.concatenate([base_dem_arr, padding])

        simulated_demand = np.maximum(0.0, base_dem_arr + noise)

        # 3. Construct 1D arrival vector per step (1..horizon_ticks)
        arrival_vector = np.zeros(horizon_ticks, dtype=np.float64)
        for step_idx in range(1, horizon_ticks + 1):
            if step_idx in scheduled_arrivals:
                arrival_vector[step_idx - 1] += scheduled_arrivals[step_idx]

        if shipment_arrival_step is not None and 1 <= shipment_arrival_step <= horizon_ticks:
            arrival_vector[shipment_arrival_step - 1] += shipment_quantity

        # 4. Step forward inventory across all trials concurrently
        inv = np.full(trials, max(0.0, initial_inventory), dtype=np.float64)
        cap = float(capacity)

        inv_trajectories = np.zeros((trials, horizon_ticks), dtype=np.float64)
        unmet_matrix = np.zeros((trials, horizon_ticks), dtype=np.float64)

        for step in range(horizon_ticks):
            # A. Ingest scheduled replenishment fuel, capped by physical tank ullage
            inv = np.minimum(cap, inv + arrival_vector[step])

            # B. Subtract simulated stochastic consumption
            net = inv - simulated_demand[:, step]

            # C. Record unmet customer demand
            unmet_step = np.maximum(0.0, -net)
            unmet_matrix[:, step] = unmet_step

            # D. Clamp tank level to non-negative inventory
            inv = np.maximum(0.0, net)
            inv_trajectories[:, step] = inv

        # 5. Extract Statistics across Trials
        # Stockout occurrence: trial hit 0 inventory or had unmet demand
        has_stockout_per_trial = (np.min(inv_trajectories, axis=1) == 0.0) | (
            np.sum(unmet_matrix, axis=1) > 0.0
        )
        stockout_prob = float(np.mean(has_stockout_per_trial))

        # Total unmet liters per trial
        total_unmet_trials = np.sum(unmet_matrix, axis=1)
        expected_unmet = float(np.mean(total_unmet_trials))

        # Minimum inventory per trial
        min_inv_trials = np.min(inv_trajectories, axis=1)

        # Percentile calculations for Confidence Intervals
        alpha = (100.0 - ci_percent) / 2.0
        lower_pct = alpha
        upper_pct = 100.0 - alpha

        unmet_lower = float(np.percentile(total_unmet_trials, lower_pct))
        unmet_median = float(np.percentile(total_unmet_trials, 50.0))
        unmet_upper = float(np.percentile(total_unmet_trials, upper_pct))

        min_inv_lower = float(np.percentile(min_inv_trials, lower_pct))
        min_inv_median = float(np.percentile(min_inv_trials, 50.0))
        min_inv_upper = float(np.percentile(min_inv_trials, upper_pct))

        unmet_ci = ConfidenceInterval(
            ci_percent=ci_percent,
            lower=round(unmet_lower, 2),
            median=round(unmet_median, 2),
            upper=round(unmet_upper, 2),
        )
        min_inv_ci = ConfidenceInterval(
            ci_percent=ci_percent,
            lower=round(min_inv_lower, 2),
            median=round(min_inv_median, 2),
            upper=round(min_inv_upper, 2),
        )

        # Percentile Trajectories over Forward Ticks
        median_traj = [round(float(v), 2) for v in np.median(inv_trajectories, axis=0)]
        p10_traj = [round(float(v), 2) for v in np.percentile(inv_trajectories, 10.0, axis=0)]
        p90_traj = [round(float(v), 2) for v in np.percentile(inv_trajectories, 90.0, axis=0)]

        return ScenarioSimulation(
            label=label,
            has_shipment=(shipment_quantity > 0.0),
            shipment_arrival_tick=(
                scheduled_arrivals.get(0) if shipment_arrival_step is None else None
            ),
            shipment_quantity_liters=round(shipment_quantity, 2),
            stockout_probability=round(stockout_prob, 3),
            expected_unmet_liters=round(expected_unmet, 2),
            unmet_ci=unmet_ci,
            min_inventory_ci=min_inv_ci,
            median_trajectory=median_traj,
            p10_trajectory=p10_traj,
            p90_trajectory=p90_traj,
        )

    def compare_scenarios(
        self,
        station: Station,
        fuel: FuelType,
        snapshot: SimulatorSnapshot,
        forecast: StationFuelForecast | None = None,
        candidate_shipment: tuple[int, float] | None = None,  # (arrival_tick, quantity_liters)
        horizon_ticks: int | None = None,
        trials: int | None = None,
        seed: int | None = None,
    ) -> TwinSimulationResult:
        """Run paired Monte Carlo simulations comparing baseline vs candidate shipment scenario."""
        current_tick = snapshot.instance.tick
        h_ticks = horizon_ticks or self.settings.risk_horizon_ticks
        n_trials = trials or self.settings.twin_trials
        sim_seed = seed if seed is not None else self.settings.twin_random_seed

        # 1. Obtain station forecast if not provided
        if forecast is None:
            forecast = self.forecaster.forecast_station_fuel(
                station=station,
                fuel=fuel,
                history=[],
                tick_minutes=snapshot.instance.tick_minutes,
                horizon_ticks=h_ticks,
                current_tick=current_tick,
            )

        residual_sigma = forecast.metrics.residual_std
        base_demand_vec = [pt.forecast_liters_per_tick for pt in forecast.trajectory]

        # 2. Extract existing in-flight / scheduled arrivals for this station & fuel
        scheduled_arrivals: dict[int, float] = {}
        for alloc in snapshot.allocations:
            if alloc.destination_station_id == station.id and alloc.fuel_type == fuel:
                if alloc.status in (AllocationStatus.IN_TRANSIT, AllocationStatus.PENDING):
                    # Check route status
                    route = next((r for r in snapshot.routes if r.id == alloc.route_id), None)
                    if (
                        route
                        and route.status == RouteStatus.DISRUPTED
                        and alloc.status == AllocationStatus.PENDING
                    ):
                        continue

                    # Calculate arrival tick
                    if alloc.expected_arrival_tick is not None:
                        arr_tick = alloc.expected_arrival_tick
                    elif alloc.departure_tick is not None and route:
                        arr_tick = alloc.departure_tick + route.transit_ticks
                    else:
                        transit = route.transit_ticks if route else 2
                        arr_tick = current_tick + 1 + transit

                    step = arr_tick - current_tick
                    if 1 <= step <= h_ticks:
                        scheduled_arrivals[step] = (
                            scheduled_arrivals.get(step, 0.0) + alloc.quantity
                        )

        # 3. Simulate Baseline Scenario (Counterfactual: No new shipment)
        baseline = self.simulate_single_scenario(
            initial_inventory=station.inventory[fuel],
            capacity=station.capacity[fuel],
            base_demand=base_demand_vec,
            scheduled_arrivals=scheduled_arrivals,
            horizon_ticks=h_ticks,
            trials=n_trials,
            residual_std=residual_sigma,
            seed=sim_seed,
            label="baseline",
            ci_percent=self.settings.twin_ci_percent,
            shipment_arrival_step=None,
            shipment_quantity=0.0,
        )

        # 4. Simulate Treatment Scenario (With candidate shipment)
        cand_step = None
        cand_qty = 0.0
        if candidate_shipment is not None:
            cand_arrival_tick, cand_qty = candidate_shipment
            cand_step = cand_arrival_tick - current_tick

        with_shipment = self.simulate_single_scenario(
            initial_inventory=station.inventory[fuel],
            capacity=station.capacity[fuel],
            base_demand=base_demand_vec,
            scheduled_arrivals=scheduled_arrivals,
            horizon_ticks=h_ticks,
            trials=n_trials,
            residual_std=residual_sigma,
            seed=sim_seed,  # Shared seed creates identical stochastic realization pairs
            label="with_shipment",
            ci_percent=self.settings.twin_ci_percent,
            shipment_arrival_step=cand_step,
            shipment_quantity=cand_qty,
        )

        # 5. Comparative Benefit Evaluation
        p_reduction = max(0.0, baseline.stockout_probability - with_shipment.stockout_probability)
        unmet_reduction = max(
            0.0, baseline.expected_unmet_liters - with_shipment.expected_unmet_liters
        )

        if baseline.stockout_probability > 0.0:
            rel_reduction = (p_reduction / baseline.stockout_probability) * 100.0
        else:
            rel_reduction = 0.0

        burn_rate = max(1.0, forecast.forecast_per_hour)
        hours_cover = round(cand_qty / burn_rate, 2)

        # Combined scalar ranking score
        # 1. Unmet demand reduction: 3.0 pts per liter
        # 2. Probability reduction: 1000.0 pts per 1.0 prob
        # 3. Hours of cover added: 40.0 pts per hour
        cand_score = round(
            (unmet_reduction * 3.0) + (p_reduction * 1000.0) + (hours_cover * 40.0), 2
        )

        # Executive narrative
        narrative = (
            f"Monte Carlo Digital Twin ({n_trials} trials, seed={sim_seed}): "
            f"Stockout probability shifts from {baseline.stockout_probability:.1%} to "
            f"{with_shipment.stockout_probability:.1%} (-{rel_reduction:.1f}% relative risk). "
            f"Expected unmet demand drops by {unmet_reduction:,.0f} L "
            f"(from {baseline.expected_unmet_liters:,.0f} L down to "
            f"{with_shipment.expected_unmet_liters:,.0f} L). "
            f"Adds {hours_cover:.1f}h of operational fuel cover."
        )

        return TwinSimulationResult(
            station_id=station.id,
            station_name=station.name,
            fuel_type=fuel,
            current_tick=current_tick,
            horizon_ticks=h_ticks,
            trials_count=n_trials,
            random_seed=sim_seed,
            residual_std=round(residual_sigma, 2),
            baseline=baseline,
            with_shipment=with_shipment,
            stockout_probability_reduction=round(p_reduction, 3),
            expected_unmet_reduction_liters=round(unmet_reduction, 2),
            relative_risk_reduction_percent=round(rel_reduction, 1),
            hours_of_cover_added=hours_cover,
            candidate_score=cand_score,
            explanation=narrative,
        )

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
        """Evaluate candidate allocation benefit conforming to TwinEvaluatorProtocol."""
        # Run comparative Monte Carlo simulation
        result = self.compare_scenarios(
            station=station,
            fuel=fuel,
            snapshot=snapshot,
            candidate_shipment=(arrival_tick, candidate_quantity),
            horizon_ticks=self.settings.risk_horizon_ticks,
            trials=self.settings.twin_trials,
            seed=self.settings.twin_random_seed,
        )

        # Apply timeliness bonus and route transit penalty
        if is_timely:
            timeliness_pts = 400.0
        else:
            ticks_late = max(1, arrival_tick - (risk.first_stockout_tick or arrival_tick))
            timeliness_pts = -50.0 * ticks_late

        transit_pts = -15.0 * route.transit_ticks
        final_score = round(result.candidate_score + timeliness_pts + transit_pts, 2)

        return CandidateImpact(
            expected_unmet_reduction_liters=result.expected_unmet_reduction_liters,
            hours_of_cover_added=result.hours_of_cover_added,
            stockout_probability_before=result.baseline.stockout_probability,
            stockout_probability_after=result.with_shipment.stockout_probability,
            score=final_score,
            scoring_details={
                "unmet_reduction_pts": round(result.expected_unmet_reduction_liters * 3.0, 2),
                "prob_reduction_pts": round(result.stockout_probability_reduction * 1000.0, 2),
                "cover_pts": round(result.hours_of_cover_added * 40.0, 2),
                "timeliness_pts": round(timeliness_pts, 2),
                "transit_pts": round(transit_pts, 2),
            },
        )
