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
    alerts: list[str] = Field(
        default_factory=list, description="Active resilience and data integrity alerts."
    )
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
    alerts: list[str] = Field(
        default_factory=list, description="Active resilience and data integrity alerts."
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


class RiskResponse(BaseModel):
    """Fleet-wide inventory risk projections, active alerts, and stockout assessments."""

    mode: OperationalMode = Field(
        ...,
        description="Authoritative operational mode: NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK.",
    )
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    overall_severity: str = Field(..., description="Fleet maximum severity: OK, WATCH, CRITICAL.")
    critical_count: int = Field(
        ..., description="Count of (station, fuel) pairs in CRITICAL severity."
    )
    watch_count: int = Field(..., description="Count of (station, fuel) pairs in WATCH severity.")
    ok_count: int = Field(..., description="Count of (station, fuel) pairs in OK severity.")
    total_projected_unmet_liters: float = Field(
        ..., description="Total projected unmet demand over horizon in liters."
    )
    earliest_stockout_station_id: str | None = Field(
        None, description="Identifier of station facing earliest stockout."
    )
    earliest_stockout_fuel: str | None = Field(
        None, description="Fuel type facing earliest stockout."
    )
    earliest_stockout_hours: float | None = Field(
        None, description="Hours until earliest stockout across fleet."
    )
    fleet_average_confidence: float = Field(
        ..., description="Average confidence score across all risk evaluations (0-1)."
    )
    active_alerts_count: int = Field(..., description="Number of priority operational alerts.")
    active_alerts: list[dict[str, Any]] = Field(
        default_factory=list, description="Urgency-sorted alert cards for dashboard."
    )
    station_risks: dict[str, dict[str, Any]] = Field(
        ..., description="Nested risk projections keyed by [station_id][fuel_type]."
    )
    depot_risks: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Depot bulk inventory reserve risks keyed by [depot_id][fuel_type].",
    )
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class StationRiskResponse(BaseModel):
    """Station-specific risk projections and stockout analysis across fuels."""

    mode: OperationalMode = Field(..., description="Authoritative operational mode.")
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    station_id: str = Field(..., description="Target fuel station identifier.")
    station_name: str = Field(..., description="Name of the fuel station.")
    highest_severity: str = Field(
        ..., description="Worst-case severity across fuels at this station."
    )
    risks: dict[str, Any] = Field(..., description="Detailed risk projections keyed by fuel type.")
    alerts: list[dict[str, Any]] = Field(
        default_factory=list, description="Active alerts for this station."
    )
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class PlanResponse(BaseModel):
    """Constrained greedy dispatch plan with alternatives and bottleneck analysis."""

    mode: OperationalMode = Field(
        ...,
        description="Authoritative operational mode: NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK.",
    )
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    plan_id: str = Field(..., description="Unique generated plan identifier.")
    created_tick: int = Field(..., description="Simulation tick at which plan was calculated.")
    total_actions: int = Field(..., description="Count of planned allocation actions.")
    total_liters_dispatched: float = Field(
        ..., description="Total fuel volume in liters planned for dispatch."
    )
    actions: list[dict[str, Any]] = Field(
        default_factory=list, description="Primary allocation actions with alternatives."
    )
    unmet_targets: list[dict[str, Any]] = Field(
        default_factory=list, description="At-risk stations that could not be served."
    )
    shared_capacity_summary: dict[str, Any] = Field(
        default_factory=dict, description="Audit of depot dispatch, inventory, and tank ullage."
    )
    summary: str = Field(..., description="Executive narrative summary of the plan.")
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class TwinSimulateRequest(BaseModel):
    """Request payload to execute a Monte Carlo digital twin forward simulation."""

    station_id: str = Field(..., description="Target fuel station identifier.")
    fuel_type: str = Field(
        default="DIESEL", description="Fuel commodity type: DIESEL, PETROL, or OCTANE."
    )
    candidate_shipment_quantity: float = Field(
        default=0.0, ge=0.0, description="Optional candidate shipment volume in liters."
    )
    candidate_arrival_tick: int | None = Field(
        None, description="Optional simulation tick when candidate shipment arrives."
    )
    trials: int | None = Field(
        None, ge=50, le=2000, description="Monte Carlo trial count override."
    )
    seed: int | None = Field(None, ge=0, description="Deterministic random seed override.")


class TwinSimulationResponse(BaseModel):
    """Comparative Monte Carlo digital twin forward simulation report."""

    mode: OperationalMode = Field(
        ...,
        description="Authoritative operational mode: NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK.",
    )
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    simulation: dict[str, Any] = Field(..., description="Detailed comparative simulation metrics.")
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class RecommendationApproveRequest(BaseModel):
    """Optional payload when an operator approves a recommendation."""

    operator_notes: str | None = Field(
        default=None, description="Optional justification or operator note for audit trail."
    )


class RecommendationRejectRequest(BaseModel):
    """Payload when an operator rejects a recommendation."""

    operator_notes: str | None = Field(
        default=None,
        description="Mandatory or optional rationale explaining why recommendation was rejected.",
    )


class RecommendationResponse(BaseModel):
    """Single recommendation response with full context, impact, and state."""

    recommendation: dict[str, Any] = Field(
        ..., description="Recommendation domain object dictionary."
    )
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class RecommendationListResponse(BaseModel):
    """List of active or historical recommendations."""

    mode: OperationalMode = Field(
        ...,
        description="Authoritative operational mode: NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK.",
    )
    reason: str = Field(..., description="Diagnostic justification for current operational mode.")
    total_count: int = Field(..., description="Total count of recommendations returned.")
    recommendations: list[dict[str, Any]] = Field(
        default_factory=list, description="Array of recommendation items."
    )
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class AuditHistoryResponse(BaseModel):
    """Audit log history entries recording decisions and state machine transitions."""

    total_events: int = Field(..., description="Total count of audit log entries returned.")
    events: list[dict[str, Any]] = Field(default_factory=list, description="Audit log records.")
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")


class AuditStatsResponse(BaseModel):
    """Consolidated decision and compliance metrics from the SQLite audit ledger."""

    stats: dict[str, Any] = Field(
        ..., description="Summary statistics: counts, liters, rejection rate."
    )
    evaluated_at: datetime = Field(..., description="UTC evaluation timestamp.")
