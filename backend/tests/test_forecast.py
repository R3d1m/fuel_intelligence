"""Unit tests for demand forecasting and anomaly detection engine."""

from datetime import UTC, datetime

import pytest
from app.domain.models import (
    DemandHistoryRecord,
    FuelMap,
    FuelType,
    InstanceInfo,
    RiskSeverity,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.intelligence.forecast import (
    DemandForecaster,
    compute_ewma,
    compute_hourly_factors,
    detect_anomaly,
    evaluate_walk_forward_backtest,
)


def create_station(
    station_id: str = "station-mirpur",
    profile: str = "urban_high",
    multiplier: float = 1.0,
) -> Station:
    """Helper to instantiate a test Station."""
    return Station(
        id=station_id,
        name="Mirpur Fuel Station",
        region_id="region-dhaka",
        status=StationStatus.OPEN,
        demand_profile=profile,
        demand_multiplier=multiplier,
        capacity=FuelMap(DIESEL=15000, PETROL=14000, OCTANE=9000),
        inventory=FuelMap(DIESEL=9000, PETROL=9000, OCTANE=5000),
    )


def create_history_record(
    station_id: str,
    fuel: FuelType,
    tick: int,
    demand: float,
    hour: int = 0,
) -> DemandHistoryRecord:
    """Helper to instantiate a test DemandHistoryRecord with specific sim_time hour."""
    sim_time = datetime(2026, 1, 1, hour, 0, tzinfo=UTC)
    return DemandHistoryRecord(
        id=tick + 1,
        station_id=station_id,
        fuel_type=fuel,
        tick=tick,
        sim_time=sim_time,
        demand_liters=demand,
        served_liters=demand,
        unmet_liters=0.0,
    )


def test_cold_start_fallback():
    """Verify that empty history produces a valid baseline fallback without errors."""
    forecaster = DemandForecaster()
    station = create_station(profile="urban_high", multiplier=1.0)

    forecast = forecaster.forecast_station_fuel(
        station=station,
        fuel=FuelType.DIESEL,
        history=[],
        tick_minutes=15,
        horizon_ticks=12,
    )

    assert forecast.station_id == "station-mirpur"
    assert forecast.fuel_type == FuelType.DIESEL
    assert forecast.model_used == "BASELINE_FALLBACK"
    assert forecast.forecast_per_tick > 0.0
    # For urban_high, diesel nominal is ~8500 / 96 = ~88.54 L/tick
    assert pytest.approx(forecast.forecast_per_tick, rel=1e-2) == 88.542
    assert pytest.approx(forecast.forecast_per_hour, rel=1e-2) == 88.542 * 4  # 15 min = 4 ticks/hr
    assert len(forecast.trajectory) == 12
    assert forecast.metrics.sample_count == 0
    assert forecast.metrics.mae == 0.0
    assert not forecast.anomaly.is_spike


def test_ewma_calculation():
    """Verify recursive EWMA smoothing math."""
    values = [100.0, 110.0, 90.0, 120.0]
    alpha = 0.5
    # s0 = 100
    # s1 = 0.5*110 + 0.5*100 = 105
    # s2 = 0.5*90 + 0.5*105 = 97.5
    # s3 = 0.5*120 + 0.5*97.5 = 108.75
    ewma = compute_ewma(values, alpha)
    assert len(ewma) == 4
    assert ewma[0] == 100.0
    assert ewma[1] == 105.0
    assert ewma[2] == 97.5
    assert ewma[3] == 108.75


def test_liters_per_hour_conversion():
    """Verify liters/hour converts dynamically based on tick_minutes."""
    forecaster = DemandForecaster()
    station = create_station()
    history = [
        create_history_record(station.id, FuelType.DIESEL, 0, 50.0),
        create_history_record(station.id, FuelType.DIESEL, 1, 50.0),
    ]

    # 15 minutes per tick -> 4 ticks per hour -> 50.0 * 4 = 200 L/h
    fc_15 = forecaster.forecast_station_fuel(
        station=station,
        fuel=FuelType.DIESEL,
        history=history,
        tick_minutes=15,
    )
    assert fc_15.forecast_per_tick == 50.0
    assert fc_15.forecast_per_hour == 200.0

    # 30 minutes per tick -> 2 ticks per hour -> 50.0 * 2 = 100 L/h
    fc_30 = forecaster.forecast_station_fuel(
        station=station,
        fuel=FuelType.DIESEL,
        history=history,
        tick_minutes=30,
    )
    assert fc_30.forecast_per_tick == 50.0
    assert fc_30.forecast_per_hour == 100.0


def test_insufficient_history_falls_back_to_pure_ewma():
    """Verify that fewer than 3 unique hours defaults diurnal factors to 1.0 (pure EWMA)."""
    station = create_station()
    # 4 records, all at hour 0
    history = [
        create_history_record(station.id, FuelType.PETROL, t, 40.0 + t, hour=0) for t in range(4)
    ]
    factors, has_seasonality = compute_hourly_factors(history, tick_minutes=15)
    assert not has_seasonality
    assert all(factors[h] == 1.0 for h in range(24))

    forecaster = DemandForecaster()
    fc = forecaster.forecast_station_fuel(
        station=station,
        fuel=FuelType.PETROL,
        history=history,
        tick_minutes=15,
    )
    assert fc.model_used == "EWMA"


def test_hourly_factors_shrinkage():
    """Verify diurnal factor learning across multiple hours with Bayesian shrinkage."""
    records = []
    # Create 12 records across 4 different hours (0, 8, 12, 18)
    for hour, demand in [(0, 50.0), (8, 150.0), (12, 100.0), (18, 100.0)]:
        for _ in range(3):
            records.append(
                create_history_record(
                    "station-mirpur",
                    FuelType.DIESEL,
                    tick=len(records),
                    demand=demand,
                    hour=hour,
                )
            )

    factors, has_seasonality = compute_hourly_factors(records, tick_minutes=15)
    assert has_seasonality
    # Hour 8 had demand 150 vs mean 100 -> raw factor = 1.5, regularized factor > 1.0
    assert factors[8] > 1.0
    # Hour 0 had demand 50 vs mean 100 -> raw factor = 0.5, regularized factor < 1.0
    assert factors[0] < 1.0
    # Untouched hour (e.g. hour 3) defaults to 1.0
    assert factors[3] == 1.0


def test_walk_forward_backtest_metrics():
    """Verify 1-step-ahead walk-forward evaluation metrics (MAE, MAPE, RMSE, residual_std)."""
    station_id = "station-tongi"
    fuel = FuelType.DIESEL
    # Constant demand of 100.0 produces 0 error once initialized
    history = [create_history_record(station_id, fuel, t, 100.0) for t in range(6)]
    metrics, residuals = evaluate_walk_forward_backtest(
        history,
        alpha=0.3,
        tick_minutes=15,
    )
    assert metrics.sample_count == 5
    assert metrics.mae == 0.0
    assert metrics.mape == 0.0
    assert metrics.rmse == 0.0

    # With varying demand:
    varying_history = [
        create_history_record(station_id, fuel, 0, 100.0),
        create_history_record(station_id, fuel, 1, 120.0),  # predicted 100, err = +20
        create_history_record(station_id, fuel, 2, 80.0),  # ewma ~106, err ~ -26
    ]
    v_metrics, v_res = evaluate_walk_forward_backtest(
        varying_history,
        alpha=0.3,
        tick_minutes=15,
    )
    assert v_metrics.sample_count == 2
    assert v_metrics.mae > 0.0
    assert v_metrics.mape > 0.0
    assert v_metrics.rmse > 0.0
    assert v_metrics.residual_std > 0.0


def test_anomaly_detection_nominal():
    """Verify nominal conditions produce no anomaly spike."""
    station = create_station(multiplier=1.0)
    history = [create_history_record(station.id, FuelType.DIESEL, t, 60.0) for t in range(5)]
    anomaly = detect_anomaly(
        station=station,
        fuel=FuelType.DIESEL,
        recent_records=history,
        baseline_rate=60.0,
    )
    assert not anomaly.is_spike
    assert anomaly.severity == RiskSeverity.OK
    assert not anomaly.multiplier_spike
    assert not anomaly.statistical_spike
    assert "Nominal demand" in anomaly.explanation


def test_anomaly_detection_multiplier_spike():
    """Verify simulator station demand_multiplier > 1.0 triggers spike flag."""
    station = create_station(multiplier=1.75)
    history = [create_history_record(station.id, FuelType.DIESEL, t, 60.0) for t in range(5)]
    anomaly = detect_anomaly(
        station=station,
        fuel=FuelType.DIESEL,
        recent_records=history,
        baseline_rate=60.0,
    )
    assert anomaly.is_spike
    assert anomaly.multiplier_spike
    assert not anomaly.statistical_spike
    assert anomaly.severity == RiskSeverity.CRITICAL
    assert "demand_multiplier is elevated" in anomaly.explanation or "1.75x" in anomaly.explanation


def test_anomaly_detection_statistical_surge():
    """Verify surge in recent observed values triggers statistical spike flag."""
    station = create_station(multiplier=1.0)
    # Baseline was 50, recent 3 ticks surged to 100
    history = [
        create_history_record(station.id, FuelType.DIESEL, 0, 50.0),
        create_history_record(station.id, FuelType.DIESEL, 1, 50.0),
        create_history_record(station.id, FuelType.DIESEL, 2, 100.0),
        create_history_record(station.id, FuelType.DIESEL, 3, 105.0),
        create_history_record(station.id, FuelType.DIESEL, 4, 95.0),
    ]
    anomaly = detect_anomaly(
        station=station,
        fuel=FuelType.DIESEL,
        recent_records=history,
        baseline_rate=50.0,
        threshold_ratio=1.35,
    )
    assert anomaly.is_spike
    assert anomaly.statistical_spike
    assert not anomaly.multiplier_spike
    assert anomaly.spike_ratio >= 1.9
    assert "Statistical demand surge" in anomaly.explanation


def test_anomaly_detection_dual_signal():
    """Verify dual signal (elevated multiplier AND surge) generates comprehensive report."""
    station = create_station(multiplier=2.0)
    history = [create_history_record(station.id, FuelType.DIESEL, t, 120.0) for t in range(5)]
    anomaly = detect_anomaly(
        station=station,
        fuel=FuelType.DIESEL,
        recent_records=history,
        baseline_rate=60.0,
        threshold_ratio=1.35,
    )
    assert anomaly.is_spike
    assert anomaly.multiplier_spike
    assert anomaly.statistical_spike
    assert anomaly.severity == RiskSeverity.CRITICAL
    assert "demand_multiplier is elevated at 2.00x" in anomaly.explanation
    assert "above baseline" in anomaly.explanation


def test_trajectory_confidence_bounds():
    """Verify forward trajectory points have valid 95% confidence intervals."""
    forecaster = DemandForecaster()
    station = create_station()
    history = [
        create_history_record(station.id, FuelType.OCTANE, t, 30.0 + (t % 3) * 5) for t in range(10)
    ]
    fc = forecaster.forecast_station_fuel(
        station=station,
        fuel=FuelType.OCTANE,
        history=history,
        tick_minutes=15,
        horizon_ticks=8,
    )
    assert len(fc.trajectory) == 8
    for pt in fc.trajectory:
        assert pt.lower_bound_liters >= 0.0
        assert pt.lower_bound_liters <= pt.forecast_liters_per_tick
        assert pt.upper_bound_liters >= pt.forecast_liters_per_tick


def test_fleet_demand_forecast_batch():
    """Verify batch forecasting across entire snapshot stations and fuels."""
    forecaster = DemandForecaster()
    station1 = create_station("station-1", "urban_high", multiplier=1.0)
    station2 = create_station("station-2", "industrial", multiplier=1.8)

    snapshot = SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="test",
            scenario_version="1.0",
            seed=42,
            sim_time="2026-01-01T00:00:00",
            tick=5,
            tick_minutes=15,
            status="RUNNING",
        ),
        depots=[],
        stations=[station1, station2],
        routes=[],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=5,
        sim_time="2026-01-01T00:00:00",
        fetched_at=datetime.now(UTC),
    )

    history = [create_history_record("station-1", FuelType.DIESEL, t, 70.0) for t in range(5)]

    fleet_fc = forecaster.forecast_all(snapshot=snapshot, demand_history=history)

    assert "station-1" in fleet_fc.forecasts
    assert "station-2" in fleet_fc.forecasts
    assert FuelType.DIESEL.value in fleet_fc.forecasts["station-1"]
    assert FuelType.PETROL.value in fleet_fc.forecasts["station-1"]
    assert FuelType.OCTANE.value in fleet_fc.forecasts["station-1"]

    assert fleet_fc.total_fleet_demand_per_hour > 0.0
    # Station 2 has multiplier 1.8 -> active anomalies should include station 2
    assert len(fleet_fc.active_anomalies) >= 3  # Diesel, Petrol, Octane for station 2
