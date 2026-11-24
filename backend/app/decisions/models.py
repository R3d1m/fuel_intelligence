"""Decision Models, Recommendation State Machine, and Idempotency Utilities.

Defines the core data structures for operator decision support:
1. Recommendation object encapsulating: what, why, signals, constraints, expected impact,
   confidence, alternatives, and deterministic idempotency token.
2. Formal recommendation lifecycle state machine:
   PROPOSED -> APPROVED -> SUBMITTED (or FAILED)
   PROPOSED -> REJECTED
3. Deterministic SHA-256 idempotency key generation preventing duplicate simulator allocations.
4. Comprehensive error code mapping translating simulator rejection codes to actionable guidance.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import FuelType, RiskSeverity
from app.intelligence.planner import (
    AppliedConstraints,
    MitigationType,
    PlanAction,
    PlanActionAlternative,
)


class RecommendationStatus(StrEnum):
    """Lifecycle states of a decision recommendation."""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"


def generate_idempotency_key(
    station_id: str,
    fuel_type: str | FuelType,
    tick: int,
    quantity: float,
) -> str:
    """Generate a deterministic, collision-resistant idempotency key.

    Guarantees that re-submitting or retrying an allocation for the same
    station, fuel type, simulation tick, and volume produces an identical token,
    preventing double-dispatching in upstream simulator.
    """
    fuel_str = fuel_type.value if hasattr(fuel_type, "value") else str(fuel_type)
    # Use rounded 2-decimal volume to avoid floating-point representation quirks
    canonical_repr = f"{station_id}:{fuel_str}:{tick}:{round(quantity, 2)}"
    digest = hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()[:12]
    # Keep length well within simulator's 150-char constraint
    return f"alloc_{station_id}_{fuel_str}_t{tick}_q{int(round(quantity))}_{digest}"


SIMULATOR_ERROR_GUIDANCE: dict[str, str] = {
    "ROUTE_DISRUPTED": (
        "Transit corridor is currently blocked or disrupted by an active incident. "
        "Re-route allocation through an alternative corridor or switch source depot."
    ),
    "DEPOT_CLOSED": (
        "Selected fuel depot is closed or under maintenance. "
        "Dispatch from an alternative active regional depot."
    ),
    "DEPOT_INSUFFICIENT_STOCK": (
        "Depot fuel inventory has depleted below operational reserve. "
        "Awaiting upstream pipeline/rail supply arrival or select another depot."
    ),
    "DEPOT_DISPATCH_EXCEEDED": (
        "Depot has reached its maximum tanker loading and dispatch capacity for this tick. "
        "Stagger departure to next tick or dispatch from a neighboring depot."
    ),
    "STATION_CLOSED": ("Destination retail station is currently offline or unreachable."),
    "CAPACITY_EXCEEDED": (
        "Station storage tank does not have sufficient ullage for this volume. "
        "Reduced shipment size recommended to prevent overflow."
    ),
    "ULLAGE_EXCEEDED": (
        "Combined on-hand and in-transit volume exceeds station tank capacity. "
        "Hold dispatch until existing shipments arrive and fuel is consumed."
    ),
    "IDEMPOTENCY_CONFLICT": (
        "An allocation with this exact idempotency key was already processed or cancelled. "
        "Double-dispatch blocked by simulator protection."
    ),
    "INVALID_FUEL_TYPE": (
        "Specified fuel product is not supported by station or depot storage infrastructure."
    ),
    "ROUTE_NOT_FOUND": (
        "No viable physical road or pipeline route connects this depot to the station."
    ),
    "SERVICE_UNAVAILABLE": (
        "Simulator API or gateway is temporarily unavailable (503/Circuit Open). "
        "Circuit breaker active; decision will retry once upstream recovers."
    ),
    "CIRCUIT_OPEN": (
        "Simulator circuit breaker is tripped in OPEN state. Dispatch held to protect platform."
    ),
    "TIMEOUT": ("Simulator HTTP request timed out during dispatch processing."),
    "CONNECTION_ERROR": ("Failed to establish network connection to simulator server."),
    "VALIDATION_ERROR": ("Simulator rejected request payload schema validation."),
}


def map_simulator_error_code(code: str, fallback_message: str | None = None) -> str:
    """Map a simulator error code to a clear, actionable operator message."""
    normalized_code = code.upper().strip()
    if normalized_code in SIMULATOR_ERROR_GUIDANCE:
        return SIMULATOR_ERROR_GUIDANCE[normalized_code]
    return fallback_message or f"Simulator rejected dispatch with code '{code}'."


class Recommendation(BaseModel):
    """Actionable fuel allocation recommendation with full contextual and impact telemetry."""

    model_config = ConfigDict(extra="ignore")

    # Identifiers & Metadata
    recommendation_id: str
    plan_id: str | None = None
    action_id: str | None = None
    created_tick: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # What: Dispatch Parameters
    station_id: str
    station_name: str
    fuel_type: FuelType
    quantity_liters: float = Field(gt=0.0)
    source_depot_id: str
    source_depot_name: str
    route_id: str
    departure_tick: int
    expected_arrival_tick: int
    transit_ticks: int

    # Why & Signals
    urgency_score: float = Field(ge=0.0)
    severity: RiskSeverity
    mitigation_type: MitigationType
    unavoidable_shortage_hours: float | None = None
    explanation: str
    signals: dict[str, Any] = Field(
        default_factory=dict,
        description="Dynamic telemetry snapshot at decision time (fill ratio, burn rate, spikes)",
    )

    # Constraints Evaluated
    constraints: AppliedConstraints

    # Expected Operational Benefit (Digital Twin)
    expected_unmet_reduction_liters: float = Field(ge=0.0)
    hours_of_cover_added: float = Field(ge=0.0)
    stockout_probability_before: float = Field(ge=0.0, le=1.0)
    stockout_probability_after: float = Field(ge=0.0, le=1.0)
    impact_summary: str

    # Confidence & Human Review Trigger
    confidence_score: float = Field(ge=0.0, le=1.0)
    confidence_level: str
    needs_human_review: bool

    # Alternative Candidates Considered
    alternatives: list[PlanActionAlternative] = Field(default_factory=list)

    # State Machine & Submission Telemetry
    idempotency_key: str
    status: RecommendationStatus = RecommendationStatus.PROPOSED
    operator_notes: str | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    submitted_at: datetime | None = None
    simulator_allocation_id: int | None = None
    failure_code: str | None = None
    failure_reason: str | None = None

    @classmethod
    def from_plan_action(
        cls,
        action: PlanAction,
        plan_id: str | None = None,
        created_tick: int | None = None,
        confidence_score: float | None = None,
        confidence_level: str | None = None,
        needs_human_review: bool | None = None,
        signals: dict[str, Any] | None = None,
    ) -> Recommendation:
        """Construct a Recommendation from a ConstrainedPlanner PlanAction."""
        tick = created_tick if created_tick is not None else action.departure_tick
        idempotency_key = generate_idempotency_key(
            station_id=action.station_id,
            fuel_type=action.fuel_type,
            tick=tick,
            quantity=action.quantity_liters,
        )

        p_before = action.impact.stockout_probability_before
        p_after = action.impact.stockout_probability_after
        prob_drop = max(0.0, p_before - p_after)

        impact_summary = (
            f"Reduces stockout probability from {p_before * 100:.1f}% to {p_after * 100:.1f}% "
            f"(-{prob_drop * 100:.1f}%), adding {action.impact.hours_of_cover_added:.1f}h cover "
            f"and preventing {action.impact.expected_unmet_reduction_liters:,.0f} L unmet demand."
        )

        rec_id = f"rec_{idempotency_key}"

        eff_conf_score = (
            confidence_score
            if confidence_score is not None
            else getattr(action, "confidence_score", 0.85)
        )
        eff_conf_level = (
            confidence_level
            if confidence_level is not None
            else getattr(action, "confidence_level", "HIGH")
        )
        eff_needs_review = (
            needs_human_review
            if needs_human_review is not None
            else getattr(action, "needs_human_review", False)
        )

        return cls(
            recommendation_id=rec_id,
            plan_id=plan_id,
            action_id=action.action_id,
            created_tick=tick,
            station_id=action.station_id,
            station_name=action.station_name,
            fuel_type=action.fuel_type,
            quantity_liters=action.quantity_liters,
            source_depot_id=action.source_depot_id,
            source_depot_name=action.source_depot_name,
            route_id=action.route_id,
            departure_tick=action.departure_tick,
            expected_arrival_tick=action.arrival_tick,
            transit_ticks=action.transit_ticks,
            urgency_score=action.urgency_score,
            severity=action.severity,
            mitigation_type=action.mitigation_type,
            unavoidable_shortage_hours=action.unavoidable_shortage_hours,
            explanation=action.explanation,
            signals=signals or {},
            constraints=action.constraints,
            expected_unmet_reduction_liters=action.impact.expected_unmet_reduction_liters,
            hours_of_cover_added=action.impact.hours_of_cover_added,
            stockout_probability_before=p_before,
            stockout_probability_after=p_after,
            impact_summary=impact_summary,
            confidence_score=eff_conf_score,
            confidence_level=eff_conf_level,
            needs_human_review=eff_needs_review,
            alternatives=action.alternatives,
            idempotency_key=idempotency_key,
            status=RecommendationStatus.PROPOSED,
        )
