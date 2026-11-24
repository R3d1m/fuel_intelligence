"""Unit tests for DecisionService and recommendation lifecycle state machine.

Verifies:
1. Formulation of durable recommendations from DispatchPlan actions.
2. Idempotency key stability and duplicate prevention.
3. Operator approval workflow: PROPOSED -> APPROVED -> SUBMITTED.
4. Operator rejection workflow: PROPOSED -> REJECTED.
5. Error handling and simulator failure mapping: APPROVED -> FAILED with actionable guidance.
6. Retry execution of previously failed recommendations.
7. Strict state machine transition validation preventing invalid state changes.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from app.adapters.errors import SimulatorDomainError, SimulatorUnavailableError
from app.adapters.simulator_client import SimulatorClient, SimulatorResponse
from app.decisions.audit import AuditLedger
from app.decisions.models import RecommendationStatus, generate_idempotency_key
from app.decisions.service import DecisionService
from app.domain.models import (
    Allocation,
    AllocationStatus,
    Depot,
    FuelMap,
    FuelType,
    InstanceInfo,
    RiskSeverity,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.intelligence.planner import (
    AppliedConstraints,
    CandidateImpact,
    ConstrainedPlanner,
    ConstraintType,
    DispatchPlan,
    MitigationType,
    PlanAction,
    SharedCapacitySummary,
)


def _build_test_snapshot(current_tick: int = 15) -> SimulatorSnapshot:
    """Helper creating a minimal valid SimulatorSnapshot."""
    instance = InstanceInfo(
        id=1,
        scenario_id="bup-standard",
        scenario_version="1.0",
        seed=12345,
        sim_time="2026-11-22T10:00:00Z",
        tick=current_tick,
        tick_minutes=15,
        status="RUNNING",
    )
    depot = Depot(
        id="depot_1",
        name="Dhaka Depot Hub",
        region_id="reg_1",
        status="OPEN",
        capacity=FuelMap(DIESEL=50000.0, PETROL=30000.0, OCTANE=20000.0),
        inventory=FuelMap(DIESEL=30000.0, PETROL=20000.0, OCTANE=15000.0),
        dispatch_capacity_per_tick=25000.0,
    )
    station = Station(
        id="station_1",
        name="Station Central",
        region_id="reg_1",
        status=StationStatus.OPEN,
        demand_profile="urban_high",
        capacity=FuelMap(DIESEL=20000.0, PETROL=15000.0, OCTANE=10000.0),
        inventory=FuelMap(DIESEL=3000.0, PETROL=8000.0, OCTANE=6000.0),
        demand_rate=FuelMap(DIESEL=800.0, PETROL=400.0, OCTANE=300.0),
        demand_multiplier=1.0,
    )
    route = Route(
        id="route_1",
        source_depot_id="depot_1",
        destination_station_id="station_1",
        transit_ticks=2,
        max_shipment=15000.0,
        status=RouteStatus.AVAILABLE,
    )
    return SimulatorSnapshot(
        tick=current_tick,
        sim_time="2026-11-22T10:00:00Z",
        fetched_at=datetime.now(UTC),
        is_stale=False,
        instance=instance,
        depots=[depot],
        stations=[station],
        routes=[route],
        supply_arrivals=[],
        events=[],
        allocations=[],
        demand_history=[],
    )


def _build_test_plan(snapshot: SimulatorSnapshot) -> DispatchPlan:
    """Helper creating a test DispatchPlan."""
    action = PlanAction(
        action_id="act_1",
        station_id="station_1",
        station_name="Station Central",
        fuel_type=FuelType.DIESEL,
        urgency_score=85.0,
        severity=RiskSeverity.CRITICAL,
        route_id="route_1",
        source_depot_id="depot_1",
        source_depot_name="Dhaka Depot Hub",
        quantity_liters=10000.0,
        departure_tick=snapshot.current_tick + 1,
        arrival_tick=snapshot.current_tick + 3,
        transit_ticks=2,
        is_timely=True,
        mitigation_type=MitigationType.FULL_PREVENTION,
        constraints=AppliedConstraints(
            route_max_shipment=15000.0,
            depot_available_inventory=25000.0,
            depot_remaining_dispatch_capacity=25000.0,
            station_effective_free_space=17000.0,
            amount_needed=10000.0,
            bottleneck_constraint=ConstraintType.AMOUNT_NEEDED,
            allocated_quantity=10000.0,
        ),
        impact=CandidateImpact(
            expected_unmet_reduction_liters=10000.0,
            hours_of_cover_added=5.0,
            stockout_probability_before=0.80,
            stockout_probability_after=0.15,
            score=92.0,
        ),
        explanation="Urgent replenishment mitigating stockout in 1.8h",
    )
    return DispatchPlan(
        plan_id="plan_test_1",
        created_tick=snapshot.current_tick,
        total_actions=1,
        total_liters_dispatched=10000.0,
        actions=[action],
        unmet_targets=[],
        shared_capacity_summary=SharedCapacitySummary(),
        summary="Test dispatch plan",
    )


@pytest.fixture
def mock_client():
    """Mock SimulatorClient for unit testing."""
    client = AsyncMock(spec=SimulatorClient)
    return client


@pytest.fixture
def memory_audit_ledger():
    """In-memory SQLite AuditLedger."""
    return AuditLedger(db_path=":memory:")


@pytest.fixture
def decision_service(mock_client, memory_audit_ledger):
    """DecisionService wired with in-memory ledger and mock client."""
    planner = AsyncMock(spec=ConstrainedPlanner)
    return DecisionService(
        client=mock_client,
        audit_ledger=memory_audit_ledger,
        planner=planner,
    )


def test_idempotency_key_deterministic_and_unique():
    """Verify idempotency key formula generates identical tokens for identical parameters."""
    k1 = generate_idempotency_key("st_1", FuelType.DIESEL, 10, 5000.0)
    k2 = generate_idempotency_key("st_1", FuelType.DIESEL, 10, 5000.0)
    assert k1 == k2

    # Different quantity produces different key
    k3 = generate_idempotency_key("st_1", FuelType.DIESEL, 10, 5001.0)
    assert k1 != k3

    # Different tick produces different key
    k4 = generate_idempotency_key("st_1", FuelType.DIESEL, 11, 5000.0)
    assert k1 != k4


def test_generate_recommendations_from_plan(decision_service):
    """Verify converting DispatchPlan into durable recommendations with signals."""
    snapshot = _build_test_snapshot(current_tick=10)
    plan = _build_test_plan(snapshot)

    recs = decision_service.generate_recommendations_from_plan(plan, snapshot)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.status == RecommendationStatus.PROPOSED
    assert rec.station_id == "station_1"
    assert rec.fuel_type == FuelType.DIESEL
    assert rec.quantity_liters == 10000.0
    assert rec.signals["station_fill_percent"] == 15.0
    assert "from 80.0% to 15.0%" in rec.impact_summary

    # Calling again with identical plan skips duplicates
    recs_second = decision_service.generate_recommendations_from_plan(plan, snapshot)
    assert len(recs_second) == 1
    assert recs_second[0].recommendation_id == rec.recommendation_id

    # Verify recorded in audit ledger
    stored = decision_service.get_recommendation(rec.recommendation_id)
    assert stored is not None
    assert stored.idempotency_key == rec.idempotency_key


@pytest.mark.asyncio
async def test_approve_recommendation_success(decision_service, mock_client):
    """Verify approval workflow transitions to SUBMITTED and captures allocation ID."""
    snapshot = _build_test_snapshot(current_tick=10)
    plan = _build_test_plan(snapshot)
    recs = decision_service.generate_recommendations_from_plan(plan, snapshot)
    rec = recs[0]

    # Mock simulator response
    sim_alloc = Allocation(
        id=777,
        idempotency_key=rec.idempotency_key,
        source_depot_id=rec.source_depot_id,
        destination_station_id=rec.station_id,
        route_id=rec.route_id,
        fuel_type=rec.fuel_type,
        quantity=rec.quantity_liters,
        created_tick=10,
        departure_tick=11,
        expected_arrival_tick=13,
        status=AllocationStatus.PENDING,
    )
    mock_client.create_allocation.return_value = SimulatorResponse(
        data=sim_alloc,
        is_stale=False,
        status_code=201,
    )

    approved = await decision_service.approve_recommendation(
        rec.recommendation_id, operator_notes="Urgent approval"
    )

    assert approved.status == RecommendationStatus.SUBMITTED
    assert approved.simulator_allocation_id == 777
    assert approved.operator_notes == "Urgent approval"
    assert approved.approved_at is not None
    assert approved.submitted_at is not None

    # Check audit event trail
    events = decision_service.get_audit_history(rec.recommendation_id)
    assert len(events) >= 2
    types = [e["event_type"] for e in events]
    assert "ALLOCATION_SUBMITTED" in types
    assert "DECISION_APPROVED" in types


@pytest.mark.asyncio
async def test_approve_recommendation_handles_simulator_domain_error(decision_service, mock_client):
    """Verify approval handles simulator rejection (e.g. ROUTE_DISRUPTED) and marks FAILED."""
    snapshot = _build_test_snapshot(current_tick=10)
    plan = _build_test_plan(snapshot)
    recs = decision_service.generate_recommendations_from_plan(plan, snapshot)
    rec = recs[0]

    mock_client.create_allocation.side_effect = SimulatorDomainError(
        message="Route is blocked due to accident",
        status_code=400,
        code="ROUTE_DISRUPTED",
    )

    failed_rec = await decision_service.approve_recommendation(
        rec.recommendation_id, operator_notes="Dispatching anyway"
    )

    assert failed_rec.status == RecommendationStatus.FAILED
    assert failed_rec.failure_code == "ROUTE_DISRUPTED"
    assert "corridor is currently blocked" in failed_rec.failure_reason

    # Verify audit event logged
    events = decision_service.get_audit_history(rec.recommendation_id)
    assert events[0]["event_type"] == "ALLOCATION_FAILED"
    assert events[0]["to_status"] == "FAILED"


@pytest.mark.asyncio
async def test_reject_recommendation(decision_service):
    """Verify rejecting a recommendation sets REJECTED status and audits reason."""
    snapshot = _build_test_snapshot(current_tick=10)
    plan = _build_test_plan(snapshot)
    recs = decision_service.generate_recommendations_from_plan(plan, snapshot)
    rec = recs[0]

    rejected = decision_service.reject_recommendation(
        rec.recommendation_id, operator_notes="Road works expected in 1 hour"
    )

    assert rejected.status == RecommendationStatus.REJECTED
    assert rejected.operator_notes == "Road works expected in 1 hour"
    assert rejected.rejected_at is not None

    events = decision_service.get_audit_history(rec.recommendation_id)
    assert events[0]["event_type"] == "DECISION_REJECTED"


@pytest.mark.asyncio
async def test_state_machine_invalid_transitions(decision_service, mock_client):
    """Verify illegal state transitions raise ValueError."""
    snapshot = _build_test_snapshot(current_tick=10)
    plan = _build_test_plan(snapshot)
    recs = decision_service.generate_recommendations_from_plan(plan, snapshot)
    rec = recs[0]

    # Reject first
    decision_service.reject_recommendation(rec.recommendation_id)

    # Cannot approve rejected
    with pytest.raises(ValueError, match="was rejected"):
        await decision_service.approve_recommendation(rec.recommendation_id)

    # Reset recommendation to SUBMITTED
    rec.status = RecommendationStatus.SUBMITTED
    rec.simulator_allocation_id = 888
    decision_service.audit_ledger.save_recommendation(rec)

    # Cannot re-approve submitted
    with pytest.raises(ValueError, match="already been submitted"):
        await decision_service.approve_recommendation(rec.recommendation_id)

    # Cannot reject submitted
    with pytest.raises(ValueError, match="already been submitted"):
        decision_service.reject_recommendation(rec.recommendation_id)


@pytest.mark.asyncio
async def test_retry_failed_recommendation_succeeds_when_simulator_recovers(
    decision_service, mock_client
):
    """Verify an operator can retry a FAILED recommendation when the upstream recovers."""
    snapshot = _build_test_snapshot(current_tick=10)
    plan = _build_test_plan(snapshot)
    recs = decision_service.generate_recommendations_from_plan(plan, snapshot)
    rec = recs[0]

    # First attempt: 503 Service Unavailable
    mock_client.create_allocation.side_effect = SimulatorUnavailableError(
        message="Simulator server 503", code="SERVICE_UNAVAILABLE"
    )
    failed = await decision_service.approve_recommendation(rec.recommendation_id)
    assert failed.status == RecommendationStatus.FAILED

    # Second attempt: Simulator recovers
    sim_alloc = Allocation(
        id=999,
        idempotency_key=rec.idempotency_key,
        source_depot_id=rec.source_depot_id,
        destination_station_id=rec.station_id,
        route_id=rec.route_id,
        fuel_type=rec.fuel_type,
        quantity=rec.quantity_liters,
        created_tick=10,
        departure_tick=11,
        expected_arrival_tick=13,
        status=AllocationStatus.PENDING,
    )
    mock_client.create_allocation.side_effect = None
    mock_client.create_allocation.return_value = SimulatorResponse(
        data=sim_alloc,
        is_stale=False,
        status_code=201,
    )

    recovered = await decision_service.approve_recommendation(rec.recommendation_id)
    assert recovered.status == RecommendationStatus.SUBMITTED
    assert recovered.simulator_allocation_id == 999
    assert recovered.failure_code is None
