"""Demand Forecasting and Anomaly Detection Engine.

Implements multi-level time-series demand forecasting with diurnal profile learning,
walk-forward residual backtesting (MAE, MAPE, RMSE), and dual-signal anomaly/spike detection.

Levels:
- Level 1: Exponentially Weighted Moving Average (EWMA) of per-tick demand with dynamic
  conversion to liters/hour based on instance tick_minutes.
- Level 2: Diurnal hour-of-day seasonal adjustment with empirical shrinkage, falling back
  smoothly to pure EWMA when historical data across hours is sparse.
- Anomaly Detection: Dual-signal evaluation checking station demand_multiplier (> 1.0)
  and statistical ratio of recent demand vs. longer baseline, reporting rich explanations.
- Backtesting: 1-step-ahead rolling out-of-sample backtest measuring real-time forecast error
  and calibrated residual variance for Monte Carlo uncertainty sampling.
"""

import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.domain.models import (
    DemandHistoryRecord,
    FuelType,
    RiskSeverity,
    SimulatorSnapshot,
    Station,
)

logger = logging.getLogger(__name__)

# Daily baseline profile nominals in liters/day (from simulator specifications)
DAILY_BASELINE_NOMINALS: dict[str, dict[FuelType, float]] = {
    "urban_high": {
        FuelType.DIESEL: 8500.0,
        FuelType.PETROL: 6000.0,
        FuelType.OCTANE: 4000.0,
    },
    "industrial": {
        FuelType.DIESEL: 10500.0,
        FuelType.PETROL: 3000.0,
        FuelType.OCTANE: 1500.0,
    },
    "highway": {
        FuelType.DIESEL: 8600.0,
        FuelType.PETROL: 6700.0,
        FuelType.OCTANE: 3300.0,
    },
    "regional": {
        FuelType.DIESEL: 4700.0,
        FuelType.PETROL: 4300.0,
        FuelType.OCTANE: 2400.0,
    },
}

DEFAULT_DAILY_NOMINALS: dict[FuelType, float] = {
    FuelType.DIESEL: 7200.0,
    FuelType.PETROL: 5000.0,
    FuelType.OCTANE: 2800.0,
}


class ForecastPoint(BaseModel):
    """Single forward-projected demand prediction at a future tick."""

    model_config = ConfigDict(extra="ignore")

    tick: int
    sim_time: datetime | str | None = None
    forecast_liters_per_tick: float = Field(ge=0.0)
    forecast_liters_per_hour: float = Field(ge=0.0)
    lower_bound_liters: float = Field(ge=0.0)
    upper_bound_liters: float = Field(ge=0.0)


class ForecastMetrics(BaseModel):
    """Residual error and accuracy metrics from out-of-sample backtesting."""

    model_config = ConfigDict(extra="ignore")

    mae: float = Field(ge=0.0, description="Mean Absolute Error in liters/tick")
    mape: float = Field(ge=0.0, description="Mean Absolute Percentage Error (0-100%)")
    rmse: float = Field(ge=0.0, description="Root Mean Squared Error in liters/tick")
    residual_std: float = Field(ge=0.0, description="Standard deviation of prediction residuals")
    sample_count: int = Field(ge=0, description="Number of historical evaluation points")


class AnomalyReport(BaseModel):
    """Diagnostic anomaly report evaluating demand spikes."""

    model_config = ConfigDict(extra="ignore")

    is_spike: bool = False
    severity: RiskSeverity = RiskSeverity.OK
    multiplier_spike: bool = False
    statistical_spike: bool = False
    demand_multiplier: float = 1.0
    recent_demand_mean: float = 0.0
    baseline_demand_mean: float = 0.0
    spike_ratio: float = 1.0
    explanation: str = "Nominal demand"


class StationFuelForecast(BaseModel):
    """Consolidated forecast and anomaly diagnosis for a specific station and fuel."""

    model_config = ConfigDict(extra="ignore")

    station_id: str
    fuel_type: FuelType
    forecast_per_tick: float = Field(
        ge=0.0, description="Estimated current consumption rate in liters/tick"
    )
    forecast_per_hour: float = Field(
        ge=0.0, description="Estimated current consumption rate in liters/hour"
    )
    horizon_ticks: int = Field(gt=0)
    model_used: str
    trajectory: list[ForecastPoint] = Field(default_factory=list)
    metrics: ForecastMetrics
    anomaly: AnomalyReport
    computed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FleetDemandForecast(BaseModel):
    """Aggregate fleet-wide demand forecast across all stations and fuel types."""

    model_config = ConfigDict(extra="ignore")

    forecasts: dict[str, dict[str, StationFuelForecast]] = Field(default_factory=dict)
    total_fleet_demand_per_hour: float = Field(ge=0.0)
    average_mae: float = Field(ge=0.0)
    average_mape: float = Field(ge=0.0)
    active_anomalies: list[AnomalyReport] = Field(default_factory=list)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Mathematical & Modeling Primitives
# ---------------------------------------------------------------------------


def get_fallback_nominal(
    profile: str,
    fuel: FuelType,
    tick_minutes: int,
    multiplier: float = 1.0,
) -> float:
    """Compute nominal per-tick demand from profile baseline when history is absent."""
    ticks_per_day = max(1.0, 1440.0 / max(1, tick_minutes))
    profile_nominals = DAILY_BASELINE_NOMINALS.get(profile, DEFAULT_DAILY_NOMINALS)
    daily_liters = profile_nominals.get(fuel, DEFAULT_DAILY_NOMINALS[fuel])
    nominal_per_tick = (daily_liters / ticks_per_day) * max(0.1, multiplier)
    return round(nominal_per_tick, 3)


def extract_hour_of_day(
    record: DemandHistoryRecord,
    tick_minutes: int,
) -> int:
    """Extract hour of day (0-23) from a demand record's sim_time or tick."""
    if isinstance(record.sim_time, datetime):
        return record.sim_time.hour
    if isinstance(record.sim_time, str):
        try:
            dt = datetime.fromisoformat(record.sim_time)
            return dt.hour
        except (ValueError, TypeError):
            pass
    # Fallback: estimate from tick index
    total_minutes = record.tick * tick_minutes
    return int((total_minutes // 60) % 24)


def compute_ewma(values: Sequence[float], alpha: float) -> list[float]:
    """Calculate Exponentially Weighted Moving Average series.

    s_0 = y_0
    s_t = alpha * y_t + (1 - alpha) * s_{t-1}
    """
    if not values:
        return []
    ewma: list[float] = [values[0]]
    for val in values[1:]:
        next_s = (alpha * val) + ((1.0 - alpha) * ewma[-1])
        ewma.append(next_s)
    return ewma


def compute_hourly_factors(
    records: Sequence[DemandHistoryRecord],
    tick_minutes: int,
    shrinkage_m: float = 4.0,
    min_unique_hours: int = 3,
    min_total_records: int = 8,
) -> tuple[dict[int, float], bool]:
    """Compute regularized diurnal hour-of-day multipliers with Bayesian shrinkage.

    Returns:
        (hourly_factors_dict, has_sufficient_seasonal_data)
    """
    if len(records) < min_total_records:
        return {h: 1.0 for h in range(24)}, False

    hour_buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    all_values: list[float] = []

    for r in records:
        h = extract_hour_of_day(r, tick_minutes)
        hour_buckets[h].append(r.demand_liters)
        all_values.append(r.demand_liters)

    unique_hours_present = sum(1 for h in range(24) if len(hour_buckets[h]) > 0)
    if unique_hours_present < min_unique_hours or not all_values:
        return {h: 1.0 for h in range(24)}, False

    grand_mean = sum(all_values) / len(all_values)
    if grand_mean <= 1e-4:
        return {h: 1.0 for h in range(24)}, False

    factors: dict[int, float] = {}
    for h in range(24):
        obs = hour_buckets[h]
        n_h = len(obs)
        if n_h == 0:
            factors[h] = 1.0
        else:
            h_mean = sum(obs) / n_h
            raw_factor = h_mean / grand_mean
            # Regularize towards 1.0 using prior weight shrinkage_m
            shrunk_factor = (n_h * raw_factor + shrinkage_m * 1.0) / (n_h + shrinkage_m)
            # Clip factors to reasonable bounds [0.3, 2.5]
            factors[h] = max(0.3, min(2.5, shrunk_factor))

    return factors, True


def evaluate_walk_forward_backtest(
    records: Sequence[DemandHistoryRecord],
    alpha: float,
    tick_minutes: int,
    hourly_factors: dict[int, float] | None = None,
) -> tuple[ForecastMetrics, list[float]]:
    """Run sequential 1-step-ahead walk-forward out-of-sample backtest.

    For each tick i >= 1, trains on history 0..i-1 and predicts y_i.
    Measures out-of-sample residuals (y_i - y_hat_i).
    """
    n = len(records)
    if n < 2:
        default_std = 5.0
        if n == 1:
            default_std = max(1.0, records[0].demand_liters * 0.10)
        return (
            ForecastMetrics(
                mae=0.0,
                mape=0.0,
                rmse=0.0,
                residual_std=default_std,
                sample_count=n,
            ),
            [],
        )

    residuals: list[float] = []
    abs_errors: list[float] = []
    ape_values: list[float] = []
    sq_errors: list[float] = []

    # Online simulation of EWMA
    current_ewma = records[0].demand_liters

    for i in range(1, n):
        target_record = records[i]
        actual_val = target_record.demand_liters

        # 1-step-ahead prediction
        predicted = current_ewma
        if hourly_factors is not None:
            h = extract_hour_of_day(target_record, tick_minutes)
            predicted *= hourly_factors.get(h, 1.0)

        err = actual_val - predicted
        residuals.append(err)
        abs_err = abs(err)
        abs_errors.append(abs_err)
        sq_errors.append(err * err)

        # Guard against division by zero in MAPE
        denominator = max(actual_val, 1.0)
        ape_values.append((abs_err / denominator) * 100.0)

        # Update EWMA state with observed point
        current_ewma = (alpha * actual_val) + ((1.0 - alpha) * current_ewma)

    sample_count = len(residuals)
    mae = sum(abs_errors) / sample_count
    mape = sum(ape_values) / sample_count
    rmse = math.sqrt(sum(sq_errors) / sample_count)

    # Sample standard deviation of residuals
    if sample_count > 1:
        mean_err = sum(residuals) / sample_count
        variance = sum((r - mean_err) ** 2 for r in residuals) / (sample_count - 1)
        res_std = math.sqrt(variance)
    else:
        res_std = abs(residuals[0]) if residuals else 5.0

    metrics = ForecastMetrics(
        mae=round(mae, 3),
        mape=round(mape, 2),
        rmse=round(rmse, 3),
        residual_std=round(max(0.5, res_std), 3),
        sample_count=sample_count,
    )
    return metrics, residuals


def detect_anomaly(
    station: Station,
    fuel: FuelType,
    recent_records: Sequence[DemandHistoryRecord],
    baseline_rate: float,
    threshold_ratio: float = 1.35,
) -> AnomalyReport:
    """Evaluate dual-signal demand spike anomaly detection.

    Signal 1: Explicit simulator station.demand_multiplier (> 1.05).
    Signal 2: Statistical ratio of recent demand (last 3 ticks) vs. long-term baseline.
    """
    multiplier = float(station.demand_multiplier)
    multiplier_spike = multiplier > 1.05

    recent_values = [r.demand_liters for r in recent_records[-3:]] if recent_records else []
    recent_mean = sum(recent_values) / len(recent_values) if recent_values else baseline_rate
    base_mean = max(1.0, baseline_rate)
    spike_ratio = recent_mean / base_mean

    # Statistical spike triggered if ratio exceeds threshold and absolute diff is meaningful (> 8L)
    statistical_spike = (spike_ratio >= threshold_ratio) and ((recent_mean - base_mean) >= 8.0)
    is_spike = multiplier_spike or statistical_spike

    # Severity determination
    if not is_spike:
        severity = RiskSeverity.OK
    elif multiplier >= 1.6 or spike_ratio >= 1.7:
        severity = RiskSeverity.CRITICAL
    else:
        severity = RiskSeverity.WATCH

    # Construct actionable diagnostic explanation covering both signals
    if multiplier_spike and statistical_spike:
        explanation = (
            f"Demand spike detected on {fuel.value}: station demand_multiplier is elevated "
            f"at {multiplier:.2f}x; observed recent demand ({recent_mean:.1f} L/tick) is "
            f"{spike_ratio:.2f}x above baseline ({base_mean:.1f} L/tick)."
        )
    elif multiplier_spike:
        explanation = (
            f"Elevated demand multiplier at {multiplier:.2f}x for {fuel.value}; "
            f"recent demand ({recent_mean:.1f} L/tick) is {spike_ratio:.2f}x baseline."
        )
    elif statistical_spike:
        explanation = (
            f"Statistical demand surge detected on {fuel.value}: recent demand "
            f"({recent_mean:.1f} L/tick) is {spike_ratio:.2f}x above baseline "
            f"({base_mean:.1f} L/tick), while station multiplier is {multiplier:.2f}x."
        )
    else:
        explanation = (
            f"Nominal demand on {fuel.value}. Multiplier is {multiplier:.2f}x and "
            f"recent demand ratio is {spike_ratio:.2f}x within normal baseline."
        )

    return AnomalyReport(
        is_spike=is_spike,
        severity=severity,
        multiplier_spike=multiplier_spike,
        statistical_spike=statistical_spike,
        demand_multiplier=round(multiplier, 2),
        recent_demand_mean=round(recent_mean, 2),
        baseline_demand_mean=round(base_mean, 2),
        spike_ratio=round(spike_ratio, 2),
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Forecaster Service
# ---------------------------------------------------------------------------


class DemandForecaster:
    """Multi-level time-series forecasting service for fuel demand."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def forecast_station_fuel(
        self,
        station: Station,
        fuel: FuelType,
        history: Sequence[DemandHistoryRecord],
        tick_minutes: int,
        horizon_ticks: int | None = None,
        alpha: float | None = None,
        current_tick: int | None = None,
    ) -> StationFuelForecast:
        """Generate comprehensive demand forecast and anomaly report for a station and fuel."""
        h_ticks = horizon_ticks or self.settings.forecast_horizon_ticks
        ewma_alpha = alpha or self.settings.forecast_ewma_alpha

        # Filter and sort chronologically (ascending tick)
        filtered_records = sorted(
            [r for r in history if r.station_id == station.id and r.fuel_type == fuel],
            key=lambda r: r.tick,
        )

        ticks_per_hour = 60.0 / max(1, tick_minutes)

        # Case 1: Cold start (no history available yet)
        if not filtered_records:
            base_rate = get_fallback_nominal(
                profile=station.demand_profile,
                fuel=fuel,
                tick_minutes=tick_minutes,
                multiplier=station.demand_multiplier,
            )
            rate_per_hour = base_rate * ticks_per_hour
            base_tick = current_tick or 0

            trajectory = [
                ForecastPoint(
                    tick=base_tick + step,
                    forecast_liters_per_tick=round(base_rate, 3),
                    forecast_liters_per_hour=round(rate_per_hour, 3),
                    lower_bound_liters=round(max(0.0, base_rate - 10.0), 3),
                    upper_bound_liters=round(base_rate + 10.0, 3),
                )
                for step in range(1, h_ticks + 1)
            ]

            anomaly = detect_anomaly(
                station=station,
                fuel=fuel,
                recent_records=[],
                baseline_rate=base_rate,
                threshold_ratio=self.settings.forecast_spike_threshold_ratio,
            )

            metrics = ForecastMetrics(
                mae=0.0,
                mape=0.0,
                rmse=0.0,
                residual_std=round(base_rate * 0.15, 3),
                sample_count=0,
            )

            return StationFuelForecast(
                station_id=station.id,
                fuel_type=fuel,
                forecast_per_tick=round(base_rate, 3),
                forecast_per_hour=round(rate_per_hour, 3),
                horizon_ticks=h_ticks,
                model_used="BASELINE_FALLBACK",
                trajectory=trajectory,
                metrics=metrics,
                anomaly=anomaly,
            )

        # Case 2: Historical records available -> Level 1 EWMA + Level 2 Diurnal Learning
        demand_values = [r.demand_liters for r in filtered_records]
        ewma_series = compute_ewma(demand_values, ewma_alpha)
        current_base_ewma = ewma_series[-1]

        # Learn hour-of-day seasonal profile
        hourly_factors, has_seasonality = compute_hourly_factors(
            filtered_records,
            tick_minutes=tick_minutes,
        )

        model_name = "EWMA_HOURLY_PROFILE" if has_seasonality else "EWMA"

        # Sequential walk-forward backtesting
        metrics, _ = evaluate_walk_forward_backtest(
            filtered_records,
            alpha=ewma_alpha,
            tick_minutes=tick_minutes,
            hourly_factors=hourly_factors if has_seasonality else None,
        )

        # Anomaly detection against recent records and long-term baseline
        anomaly = detect_anomaly(
            station=station,
            fuel=fuel,
            recent_records=filtered_records,
            baseline_rate=current_base_ewma,
            threshold_ratio=self.settings.forecast_spike_threshold_ratio,
        )

        # Forward projection trajectory
        last_tick = current_tick if current_tick is not None else filtered_records[-1].tick
        trajectory: list[ForecastPoint] = []
        z_score = 1.96  # 95% normal confidence interval
        std_margin = z_score * metrics.residual_std

        for step in range(1, h_ticks + 1):
            f_tick = last_tick + step
            total_mins = f_tick * tick_minutes
            f_hour = int((total_mins // 60) % 24)

            seasonal_mult = hourly_factors.get(f_hour, 1.0) if has_seasonality else 1.0
            point_pred = max(0.0, current_base_ewma * seasonal_mult)
            point_per_hour = point_pred * ticks_per_hour

            lower = max(0.0, point_pred - std_margin)
            upper = point_pred + std_margin

            trajectory.append(
                ForecastPoint(
                    tick=f_tick,
                    forecast_liters_per_tick=round(point_pred, 3),
                    forecast_liters_per_hour=round(point_per_hour, 3),
                    lower_bound_liters=round(lower, 3),
                    upper_bound_liters=round(upper, 3),
                )
            )

        current_rate_per_hour = current_base_ewma * ticks_per_hour

        return StationFuelForecast(
            station_id=station.id,
            fuel_type=fuel,
            forecast_per_tick=round(current_base_ewma, 3),
            forecast_per_hour=round(current_rate_per_hour, 3),
            horizon_ticks=h_ticks,
            model_used=model_name,
            trajectory=trajectory,
            metrics=metrics,
            anomaly=anomaly,
        )

    def forecast_all(
        self,
        snapshot: SimulatorSnapshot,
        demand_history: Sequence[DemandHistoryRecord],
        horizon_ticks: int | None = None,
    ) -> FleetDemandForecast:
        """Run batch forecasting across all stations and fuel types in a snapshot."""
        forecasts: dict[str, dict[str, StationFuelForecast]] = {}
        total_hourly_rate = 0.0
        mae_list: list[float] = []
        mape_list: list[float] = []
        active_anomalies: list[AnomalyReport] = []

        tick_minutes = snapshot.instance.tick_minutes
        current_tick = snapshot.instance.tick

        for station in snapshot.stations:
            forecasts[station.id] = {}
            for fuel in [FuelType.DIESEL, FuelType.PETROL, FuelType.OCTANE]:
                fc = self.forecast_station_fuel(
                    station=station,
                    fuel=fuel,
                    history=demand_history,
                    tick_minutes=tick_minutes,
                    horizon_ticks=horizon_ticks,
                    current_tick=current_tick,
                )
                forecasts[station.id][fuel.value] = fc
                total_hourly_rate += fc.forecast_per_hour

                if fc.metrics.sample_count > 0:
                    mae_list.append(fc.metrics.mae)
                    mape_list.append(fc.metrics.mape)

                if fc.anomaly.is_spike:
                    active_anomalies.append(fc.anomaly)

        avg_mae = round(sum(mae_list) / len(mae_list), 3) if mae_list else 0.0
        avg_mape = round(sum(mape_list) / len(mape_list), 2) if mape_list else 0.0

        return FleetDemandForecast(
            forecasts=forecasts,
            total_fleet_demand_per_hour=round(total_hourly_rate, 3),
            average_mae=avg_mae,
            average_mape=avg_mape,
            active_anomalies=active_anomalies,
        )
