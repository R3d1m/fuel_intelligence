"""Application configuration using pydantic-settings.

Follows 12-factor application design: configuration is loaded from environment
variables with sensible defaults, supporting .env file overrides for local development.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the Fuel Intelligence backend platform."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Simulator API Connection
    simulator_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the BUP Fuel Supply Simulator REST API.",
    )
    simulator_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        description="Per-request HTTP timeout in seconds when calling the simulator.",
    )
    simulator_max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum retry attempts on transient simulator failures (503, connect).",
    )
    simulator_backoff_factor: float = Field(
        default=0.5,
        ge=0.0,
        description="Exponential backoff base factor in seconds for retry delays.",
    )

    # Ingestion / Polling
    poll_interval_seconds: float = Field(
        default=1.0,
        gt=0.0,
        description="Interval in seconds between simulator polling cycles.",
    )
    stale_threshold_seconds: float = Field(
        default=10.0,
        gt=0.0,
        description="Age threshold in seconds beyond which cached snapshot is deemed stale.",
    )

    # Intelligence / Demand Forecasting
    forecast_horizon_ticks: int = Field(
        default=24,
        ge=1,
        description="Number of future ticks to project forward in demand forecasts.",
    )
    forecast_ewma_alpha: float = Field(
        default=0.3,
        gt=0.0,
        le=1.0,
        description="Smoothing parameter alpha for EWMA demand forecasting.",
    )
    forecast_history_limit: int = Field(
        default=300,
        ge=10,
        description="Maximum demand history records to query per station.",
    )
    forecast_spike_threshold_ratio: float = Field(
        default=1.35,
        gt=1.0,
        description="Ratio threshold between recent demand and baseline to flag anomaly spike.",
    )

    # Intelligence / Risk Engine
    risk_horizon_ticks: int = Field(
        default=24,
        ge=1,
        description="Number of future ticks to project inventory forward in risk assessments.",
    )
    risk_critical_hours: float = Field(
        default=3.0,
        gt=0.0,
        description="Hours to stockout threshold below which severity is CRITICAL.",
    )
    risk_critical_fill_ratio: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description="Tank fill ratio threshold below which severity is CRITICAL.",
    )
    risk_watch_hours: float = Field(
        default=8.0,
        gt=0.0,
        description="Hours to stockout threshold below which severity is WATCH.",
    )
    risk_watch_fill_ratio: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Tank fill ratio threshold below which severity is WATCH.",
    )
    risk_min_confidence: float = Field(
        default=0.60,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score below which human review is mandated.",
    )

    # Intelligence / Constrained Planner
    planner_depot_reserve_liters: float = Field(
        default=5000.0,
        ge=0.0,
        description="Buffer in liters held back at depots and unavailable for dispatch.",
    )
    planner_min_shipment_liters: float = Field(
        default=500.0,
        ge=0.0,
        description="Minimum shipment threshold in liters below which allocations are omitted.",
    )
    planner_target_fill_ratio: float = Field(
        default=0.85,
        ge=0.1,
        le=1.0,
        description="Nominal target tank fill ratio when replenishing at-risk station inventory.",
    )

    # Intelligence / Digital Twin
    twin_trials: int = Field(
        default=300,
        ge=50,
        le=2000,
        description="Number of Monte Carlo forward simulation trials per scenario evaluation.",
    )
    twin_random_seed: int = Field(
        default=42,
        ge=0,
        description="Fixed pseudorandom generator seed ensuring byte-identical determinism.",
    )
    twin_ci_percent: float = Field(
        default=95.0,
        gt=50.0,
        lt=100.0,
        description="Confidence interval width percentage (e.g. 95.0 for 95% CI).",
    )

    # Resilience / Circuit Breaker & Fallback Modes
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        ge=1,
        description="Number of consecutive failures required to trip the breaker OPEN.",
    )
    circuit_breaker_recovery_timeout: float = Field(
        default=10.0,
        gt=0.0,
        description="Cooldown duration in seconds before HALF_OPEN probe is allowed.",
    )
    fallback_critical_fill_ratio: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description=(
            "Tank fill ratio threshold below which fallback policy marks a tank as CRITICAL."
        ),
    )
    fallback_watch_fill_ratio: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Tank fill ratio threshold below which fallback policy marks a tank as WATCH.",
    )
    fallback_model_timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        description=(
            "Timeout in seconds for risky intelligence calls before triggering fallback policy."
        ),
    )

    # Decisions / Audit Ledger
    audit_db_path: str = Field(
        default="data/decisions.db",
        description="Path to SQLite database file for decisions and audit logging.",
    )
    auto_approve_enabled: bool = Field(
        default=False,
        description="Whether auto-approval mode is enabled (for automated evaluation harnesses).",
    )

    # Service Runtime
    app_env: Literal["development", "testing", "production"] = Field(
        default="development",
        description="Current application environment.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level for standard and structured logs.",
    )
    port: int = Field(
        default=8001,
        ge=1,
        le=65535,
        description="Port for the Fuel Intelligence FastAPI backend.",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton instance of Settings."""
    return Settings()
