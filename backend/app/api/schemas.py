"""Pydantic schemas for API request and response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.models import OperationalMode, SimulatorSnapshot


class SimulatorDependencyHealth(BaseModel):
    """Health and circuit status of the upstream BUP Fuel Supply Simulator."""

    connected: bool = Field(..., description="Whether the simulator was successfully reached.")
    circuit_state: str = Field(..., description="Circuit breaker state: CLOSED, OPEN, HALF_OPEN.")
    consecutive_failures: int = Field(..., description="Consecutive failure count on the breaker.")
    cooldown_remaining: float = Field(
        ..., description="Remaining cooldown in seconds if breaker is OPEN."
    )


class StoreDependencyHealth(BaseModel):
    """Status of the in-memory state repository."""

    has_snapshot: bool = Field(..., description="Whether at least one snapshot has been ingested.")
    is_stale: bool = Field(..., description="Whether stored data is deemed stale.")
    data_age_seconds: float | None = Field(
        None, description="Elapsed seconds since latest snapshot ingestion."
    )
    total_updates: int = Field(..., description="Total successful snapshot updates ingested.")
    failed_polls: int = Field(..., description="Total polling cycle failures encountered.")


class PollerDependencyHealth(BaseModel):
    """Status of the background synchronization loop."""

    is_running: bool = Field(
        ..., description="Whether background polling loop is currently active."
    )
    poll_interval_seconds: float = Field(
        ..., description="Configured interval between polling ticks in seconds."
    )


class HealthResponse(BaseModel):
    """Comprehensive health check response describing liveness and operational mode."""

    status: str = Field(..., description="Overall platform status: healthy, degraded, unhealthy.")
    mode: OperationalMode = Field(
        ...,
        description="Authoritative operational mode: NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK.",
    )
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    simulator: SimulatorDependencyHealth = Field(..., description="Simulator dependency status.")
    store: StoreDependencyHealth = Field(..., description="In-memory state store status.")
    poller: PollerDependencyHealth = Field(..., description="Background poller status.")
    timestamp: datetime = Field(..., description="UTC evaluation timestamp.")


class StateResponse(BaseModel):
    """Authoritative platform state response delivering the latest validated snapshot."""

    mode: OperationalMode = Field(
        ...,
        description="Authoritative operational mode: NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK.",
    )
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    data_age_seconds: float | None = Field(
        None, description="Age in seconds of the presented snapshot."
    )
    is_stale: bool = Field(..., description="Whether presented data is flagged stale.")
    circuit_state: str = Field(..., description="Current circuit breaker state.")
    snapshot: SimulatorSnapshot | None = Field(
        None, description="Latest or effective simulator snapshot."
    )
    evaluated_at: datetime = Field(..., description="UTC timestamp of state resolution.")


class SyncResponse(BaseModel):
    """Response returned after a manual synchronization poll request."""

    success: bool = Field(..., description="Whether the poll cycle succeeded.")
    tick: int | None = Field(None, description="Simulation tick observed during sync.")
    is_stale: bool | None = Field(None, description="Whether the resulting snapshot is stale.")
    message: str = Field(..., description="Status summary of the manual poll execution.")
    details: dict[str, Any] | None = Field(None, description="Optional extra diagnostic details.")


class ForecastResponse(BaseModel):
    """Fleet-wide demand forecasts, prediction error metrics, and detected anomalies."""

    mode: OperationalMode = Field(
        ...,
        description="Authoritative operational mode: NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK.",
    )
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    total_fleet_demand_per_hour: float = Field(..., description="Total fuel consumption rate L/h.")
    average_mae: float = Field(..., description="Fleet average Mean Absolute Error (liters/tick).")
    average_mape: float = Field(..., description="Fleet average MAPE percentage.")
    active_anomalies_count: int = Field(..., description="Number of active demand spike anomalies.")
    active_anomalies: list[dict[str, Any]] = Field(
        default_factory=list, description="List of active anomaly reports."
    )
    forecasts: dict[str, dict[str, Any]] = Field(
        ..., description="Nested forecasts keyed by [station_id][fuel_type]."
    )
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class StationForecastResponse(BaseModel):
    """Station-specific demand forecast and anomaly status across fuels."""

    mode: OperationalMode = Field(..., description="Authoritative operational mode.")
    station_id: str = Field(..., description="Target fuel station identifier.")
    forecasts: dict[str, Any] = Field(..., description="Forecasts keyed by fuel type.")
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")
