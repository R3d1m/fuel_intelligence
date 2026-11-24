"""Unit tests for SQLite AuditLedger.

Verifies:
1. In-memory and persistent schema initialization.
2. Saving and updating Recommendation objects.
3. Strict idempotency key unique constraints and lookups.
4. Filtering by status, destination station, and urgency ordering.
5. Audit event append logging with operator notes and transition details.
6. Aggregate decision statistics and rejection rate calculations.
"""

from datetime import UTC, datetime

from app.decisions.audit import AuditLedger
from app.decisions.models import Recommendation, RecommendationStatus, generate_idempotency_key
from app.domain.models import FuelType, RiskSeverity
from app.intelligence.planner import (
    AppliedConstraints,
    ConstraintType,
    MitigationType,
    PlanActionAlternative,
)


def _make_sample_recommendation(
    station_id: str = "station_1",
    fuel: FuelType = FuelType.DIESEL,
    tick: int = 10,
    quantity: float = 12000.0,
    urgency: float = 78.5,
) -> Recommendation:
    """Helper to generate a realistic sample recommendation."""
    idempotency_key = generate_idempotency_key(station_id, fuel, tick, quantity)
    constraints = AppliedConstraints(
        route_max_shipment=15000.0,
        depot_available_inventory=25000.0,
        depot_remaining_dispatch_capacity=20000.0,
        station_effective_free_space=14000.0,
        amount_needed=12000.0,
        bottleneck_constraint=ConstraintType.AMOUNT_NEEDED,
        allocated_quantity=quantity,
    )
    alternatives = [
        PlanActionAlternative(
            route_id="route_alt_1",
            source_depot_id="depot_north",
            source_depot_name="Depot North",
            quantity_liters=10000.0,
            departure_tick=tick,
            arrival_tick=tick + 3,
            transit_ticks=3,
            is_timely=True,
            mitigation_type=MitigationType.FULL_PREVENTION,
            bottleneck_constraint=ConstraintType.ROUTE_MAX_SHIPMENT,
            score=65.0,
            reason_not_chosen="Longer transit time and lower risk reduction",
        )
    ]
    return Recommendation(
        recommendation_id=f"rec_{idempotency_key}",
        plan_id="plan_sample_1",
        action_id="act_sample_1",
        created_tick=tick,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        station_id=station_id,
        station_name="Dhaka Central Station",
        fuel_type=fuel,
        quantity_liters=quantity,
        source_depot_id="depot_central",
        source_depot_name="Depot Central Hub",
        route_id="route_1",
        departure_tick=tick,
        expected_arrival_tick=tick + 2,
        transit_ticks=2,
        urgency_score=urgency,
        severity=RiskSeverity.CRITICAL,
        mitigation_type=MitigationType.FULL_PREVENTION,
        unavoidable_shortage_hours=None,
        explanation="Imminent stockout projected in 1.5h under active demand spike",
        signals={"station_fill_percent": 15.2, "burn_rate_l_h": 2400.0},
        constraints=constraints,
        expected_unmet_reduction_liters=12000.0,
        hours_of_cover_added=5.0,
        stockout_probability_before=0.82,
        stockout_probability_after=0.12,
        impact_summary="Reduces stockout probability from 82.0% to 12.0% (-70.0%)",
        confidence_score=0.91,
        confidence_level="HIGH",
        needs_human_review=False,
        alternatives=alternatives,
        idempotency_key=idempotency_key,
        status=RecommendationStatus.PROPOSED,
    )


def test_audit_ledger_save_and_retrieve():
    """Verify storing a recommendation and retrieving it by ID and idempotency key."""
    ledger = AuditLedger(db_path=":memory:")
    rec = _make_sample_recommendation()

    ledger.save_recommendation(rec, raw_inputs={"test_key": "test_val"})

    # Fetch by primary ID
    retrieved = ledger.get_recommendation(rec.recommendation_id)
    assert retrieved is not None
    assert retrieved.recommendation_id == rec.recommendation_id
    assert retrieved.station_id == "station_1"
    assert retrieved.fuel_type == FuelType.DIESEL
    assert retrieved.quantity_liters == 12000.0
    assert retrieved.status == RecommendationStatus.PROPOSED
    assert retrieved.urgency_score == 78.5
    assert len(retrieved.alternatives) == 1
    assert retrieved.alternatives[0].route_id == "route_alt_1"
    assert retrieved.constraints.bottleneck_constraint == ConstraintType.AMOUNT_NEEDED

    # Fetch by idempotency key
    by_key = ledger.get_recommendation_by_idempotency_key(rec.idempotency_key)
    assert by_key is not None
    assert by_key.recommendation_id == rec.recommendation_id


def test_audit_ledger_status_update_on_conflict():
    """Verify upserting an existing recommendation updates status and timestamps."""
    ledger = AuditLedger(db_path=":memory:")
    rec = _make_sample_recommendation()
    ledger.save_recommendation(rec)

    # Modify status and re-save
    rec.status = RecommendationStatus.APPROVED
    rec.approved_at = datetime.now(UTC)
    rec.operator_notes = "Approved by senior dispatcher"
    ledger.save_recommendation(rec)

    updated = ledger.get_recommendation(rec.recommendation_id)
    assert updated is not None
    assert updated.status == RecommendationStatus.APPROVED
    assert updated.operator_notes == "Approved by senior dispatcher"
    assert updated.approved_at is not None


def test_audit_ledger_filtering_and_urgency_ordering():
    """Verify list_recommendations filters by status, station, and orders by urgency."""
    ledger = AuditLedger(db_path=":memory:")

    rec1 = _make_sample_recommendation(station_id="station_1", urgency=50.0)
    rec2 = _make_sample_recommendation(
        station_id="station_2", fuel=FuelType.PETROL, urgency=90.0, tick=12
    )
    rec3 = _make_sample_recommendation(
        station_id="station_1", fuel=FuelType.OCTANE, urgency=70.0, tick=15
    )

    ledger.save_recommendation(rec1)
    ledger.save_recommendation(rec2)
    ledger.save_recommendation(rec3)

    # All sorted by urgency descending
    all_recs = ledger.list_recommendations()
    assert len(all_recs) == 3
    assert [r.urgency_score for r in all_recs] == [90.0, 70.0, 50.0]

    # Filter by station
    st1_recs = ledger.list_recommendations(station_id="station_1")
    assert len(st1_recs) == 2
    assert [r.urgency_score for r in st1_recs] == [70.0, 50.0]

    # Filter by status
    rec2.status = RecommendationStatus.SUBMITTED
    ledger.save_recommendation(rec2)

    proposed = ledger.list_recommendations(status=RecommendationStatus.PROPOSED)
    assert len(proposed) == 2
    assert all(r.status == RecommendationStatus.PROPOSED for r in proposed)

    submitted = ledger.list_recommendations(status=RecommendationStatus.SUBMITTED)
    assert len(submitted) == 1
    assert submitted[0].station_id == "station_2"


def test_audit_ledger_event_logging():
    """Verify recording and querying state machine audit transitions."""
    ledger = AuditLedger(db_path=":memory:")
    rec = _make_sample_recommendation()
    ledger.save_recommendation(rec)

    ledger.record_audit_event(
        recommendation_id=rec.recommendation_id,
        event_type="PROPOSAL_CREATED",
        from_status=None,
        to_status="PROPOSED",
        operator_notes="Greedy plan generated",
    )
    ledger.record_audit_event(
        recommendation_id=rec.recommendation_id,
        event_type="DECISION_APPROVED",
        from_status="PROPOSED",
        to_status="APPROVED",
        operator_notes="Fast-track approval",
        details={"speed": "fast"},
    )

    events = ledger.get_audit_events(recommendation_id=rec.recommendation_id)
    assert len(events) == 2
    # Ordered descending by timestamp
    assert events[0]["event_type"] == "DECISION_APPROVED"
    assert events[0]["to_status"] == "APPROVED"
    assert events[0]["details"] == {"speed": "fast"}
    assert events[1]["event_type"] == "PROPOSAL_CREATED"


def test_audit_ledger_statistics_aggregation():
    """Verify statistical summary metrics and rejection rate computation."""
    ledger = AuditLedger(db_path=":memory:")

    r_sub = _make_sample_recommendation(station_id="s1", quantity=10000.0, tick=1)
    r_sub.status = RecommendationStatus.SUBMITTED
    ledger.save_recommendation(r_sub)

    r_rej = _make_sample_recommendation(
        station_id="s2", fuel=FuelType.PETROL, quantity=5000.0, tick=2
    )
    r_rej.status = RecommendationStatus.REJECTED
    ledger.save_recommendation(r_rej)

    r_prop = _make_sample_recommendation(
        station_id="s3", fuel=FuelType.OCTANE, quantity=8000.0, tick=3
    )
    r_prop.status = RecommendationStatus.PROPOSED
    ledger.save_recommendation(r_prop)

    stats = ledger.get_audit_statistics()
    assert stats["total_recommendations"] == 3
    assert stats["total_liters_recommended"] == 23000.0
    assert stats["submitted_count"] == 1
    assert stats["rejected_count"] == 1
    # Decided = submitted (1) + rejected (1) = 2. Rejection rate = 1/2 = 50.0%
    assert stats["rejection_rate"] == 0.5

    # Test clear
    ledger.clear()
    cleared_stats = ledger.get_audit_statistics()
    assert cleared_stats["total_recommendations"] == 0
    assert cleared_stats["rejection_rate"] == 0.0
