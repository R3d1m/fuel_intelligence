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

    # Resilience / Circuit Breaker
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
