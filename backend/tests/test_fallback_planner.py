"""Unit tests for fallback emergency rule policy and resilience fail-safe integration."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.decisions.audit import AuditLedger
from app.decisions.models import RecommendationStatus
from app.decisions.service import DecisionService
from app.domain.models import (
    Depot,
    DepotStatus,
    FuelType,
    InstanceInfo,
    OperationalMode,
    RiskSeverity,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.intelligence.fallback import FallbackPlanner, generate_fallback_plan
from app.intelligence.planner import ConstrainedPlanner
from app.resilience.mode import ResilienceManager


def _make_fallback_test_snapshot(
    tank_fill_pct: float = 10.0,
    depot_stock: float = 50000.0,
    depot_dispatch: float = 15000.0,
    route_max: float = 9000.0,
    route_status: RouteStatus = RouteStatus.AVAILABLE,
) -> SimulatorSnapshot:
    """Helper creating a test network with controlled fill levels and constraints."""
    cap = 20000.0
    current_inv = cap * (tank_fill_pct / 100.0)

    return SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="fallback-test",
            scenario_version="1.0",
            seed=42,
            sim_time="2026-11-24T12:00:00Z",
            tick=20,
            tick_minutes=15,
            status="RUNNING",
        ),
        depots=[
            Depot(
                id="DEP-NORTH",
                name="North Terminal",
                region_id="north",
                capacity={FuelType.DIESEL: 100000.0, FuelType.OCTANE: 80000.0},
                inventory={FuelType.DIESEL: depot_stock, FuelType.OCTANE: depot_stock},
                dispatch_capacity_per_tick=depot_dispatch,
                status=DepotStatus.OPEN,
            )
        ],
        stations=[
            Station(
                id="STA-ALPHA",
                name="Alpha Highway Station",
                region_id="north",
                demand_profile="highway",
                capacity={FuelType.DIESEL: cap, FuelType.OCTANE: cap},
                inventory={FuelType.DIESEL: current_inv, FuelType.OCTANE: cap * 0.70},
                demand_multiplier=1.0,
                status=StationStatus.OPEN,
            )
        ],
        routes=[
            Route(
                id="RT-ALPHA-PRIMARY",
                source_depot_id="DEP-NORTH",
                destination_station_id="STA-ALPHA",
                transit_ticks=2,
                max_shipment=route_max,
                status=route_status,
            )
        ],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=20,
        sim_time="2026-11-24T12:00:00Z",
        fetched_at=datetime.now(UTC),
        is_stale=False,
    )


def test_fallback_planner_plain_rule_thresholds() -> None:
    """Verify tanks < 20% are CRITICAL and 20-35% are WATCH."""
    planner = FallbackPlanner(critical_fill_ratio=0.20, watch_fill_ratio=0.35)

    # 1. 10% fill -> CRITICAL
    snap_crit = _make_fallback_test_snapshot(tank_fill_pct=10.0)
    plan_crit = planner.generate_plan(snap_crit)
    assert len(plan_crit.actions) == 1
    assert plan_crit.actions[0].severity == RiskSeverity.CRITICAL
    assert plan_crit.actions[0].needs_human_review is True

    # 2. 25% fill -> WATCH
    snap_watch = _make_fallback_test_snapshot(tank_fill_pct=25.0)
    plan_watch = planner.generate_plan(snap_watch)
    assert len(plan_watch.actions) == 1
    assert plan_watch.actions[0].severity == RiskSeverity.WATCH

    # 3. 50% fill -> OK (no actions required)
    snap_ok = _make_fallback_test_snapshot(tank_fill_pct=50.0)
    plan_ok = planner.generate_plan(snap_ok)
    assert len(plan_ok.actions) == 0


def test_fallback_planner_sends_largest_feasible_amount() -> None:
    """Verify fallback policy allocates min(route, depot_avail, dispatch, ullage, deficit)."""
    planner = FallbackPlanner(
        depot_reserve_liters=5000.0,
        min_shipment_liters=500.0,
        target_fill_ratio=0.85,
    )

    # Tank cap = 20,000 L, current = 2,000 L (10%). Deficit to 85% = 15,000 L. Ullage = 18,000 L.
    # Depot stock = 50,000 L (avail = 45,000 L). Dispatch cap = 15,000 L.
    # Route max = 7,000 L.
    # Feasible = min(7000, 45000, 15000, 18000, 15000) = 7,000 L (bottleneck: ROUTE_MAX_SHIPMENT)
    snap = _make_fallback_test_snapshot(
        tank_fill_pct=10.0,
        depot_stock=50000.0,
        depot_dispatch=15000.0,
        route_max=7000.0,
    )
    plan = planner.generate_plan(snap)
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.quantity_liters == 7000.0
    assert action.constraints.bottleneck_constraint.value == "ROUTE_MAX_SHIPMENT"
    assert action.departure_tick == 21
    assert action.arrival_tick == 23


def test_fallback_planner_sets_low_confidence_and_mandates_human_review() -> None:
    """Verify fallback plan actions enforce confidence=0.25 and needs_human_review=True."""
    snap = _make_fallback_test_snapshot(tank_fill_pct=15.0)
    plan = generate_fallback_plan(snap, fallback_reason="Test forecaster offline")

    assert plan.is_fallback is True
    assert "Test forecaster offline" in plan.fallback_reason
    assert len(plan.actions) == 1
    action = plan.actions[0]

    assert action.is_fallback is True
    assert action.confidence_score == 0.25
    assert action.confidence_level == "LOW"
    assert action.needs_human_review is True
    assert "FALLBACK HEURISTIC" in action.explanation
    assert "operator review mandated" in action.explanation.lower()


def test_fallback_planner_unmet_target_when_routes_disrupted() -> None:
    """Verify UnmetTarget is recorded when no available route exists."""
    snap = _make_fallback_test_snapshot(
        tank_fill_pct=10.0,
        route_status=RouteStatus.DISRUPTED,
    )
    planner = FallbackPlanner()
    plan = planner.generate_plan(snap)

    assert len(plan.actions) == 0
    assert len(plan.unmet_targets) == 1
    assert plan.unmet_targets[0].station_id == "STA-ALPHA"
    assert plan.unmet_targets[0].reason_code == "NO_AVAILABLE_ROUTE"
    assert "No available corridor" in plan.unmet_targets[0].explanation


def test_fallback_planner_shared_capacity_bookkeeping() -> None:
    """Verify shared depot dispatch quota and inventory are tracked across multiple stations."""
    # 2 stations requesting DIESEL from DEP-NORTH with total dispatch cap 10,000 L
    cap = 20000.0
    snap = SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="test",
            scenario_version="1.0",
            seed=1,
            sim_time="2026-11-24T12:00:00Z",
            tick=10,
            tick_minutes=15,
            status="RUNNING",
        ),
        depots=[
            Depot(
                id="DEP-NORTH",
                name="North Terminal",
                region_id="north",
                capacity={FuelType.DIESEL: 100000.0, FuelType.OCTANE: 80000.0},
                inventory={FuelType.DIESEL: 20000.0, FuelType.OCTANE: 20000.0},
                dispatch_capacity_per_tick=10000.0,
                status=DepotStatus.OPEN,
            )
        ],
        stations=[
            Station(
                id="STA-01",
                name="Station 1",
                region_id="north",
                demand_profile="urban",
                capacity={FuelType.DIESEL: cap, FuelType.OCTANE: cap},
                inventory={FuelType.DIESEL: 1000.0, FuelType.OCTANE: cap},  # 5% fill (urgency 95)
                demand_multiplier=1.0,
                status=StationStatus.OPEN,
            ),
            Station(
                id="STA-02",
                name="Station 2",
                region_id="north",
                demand_profile="urban",
                capacity={FuelType.DIESEL: cap, FuelType.OCTANE: cap},
                inventory={FuelType.DIESEL: 2000.0, FuelType.OCTANE: cap},  # 10% fill (urgency 90)
                demand_multiplier=1.0,
                status=StationStatus.OPEN,
            ),
        ],
        routes=[
            Route(
                id="RT-01",
                source_depot_id="DEP-NORTH",
                destination_station_id="STA-01",
                transit_ticks=2,
                max_shipment=8000.0,
                status=RouteStatus.AVAILABLE,
            ),
            Route(
                id="RT-02",
                source_depot_id="DEP-NORTH",
                destination_station_id="STA-02",
                transit_ticks=2,
                max_shipment=8000.0,
                status=RouteStatus.AVAILABLE,
            ),
        ],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=10,
        sim_time="2026-11-24T12:00:00Z",
        fetched_at=datetime.now(UTC),
        is_stale=False,
    )

    planner = FallbackPlanner(depot_reserve_liters=5000.0)
    plan = planner.generate_plan(snap)

    # Depot has 10,000 L dispatch cap. Station 1 (most critical) gets 8,000 L.
    # Station 2 gets remaining 2,000 L (bounded by DEPOT_DISPATCH_CAPACITY).
    assert len(plan.actions) == 2
    assert plan.actions[0].station_id == "STA-01"
    assert plan.actions[0].quantity_liters == 8000.0

    assert plan.actions[1].station_id == "STA-02"
    assert plan.actions[1].quantity_liters == 2000.0
    assert plan.actions[1].constraints.bottleneck_constraint.value == "DEPOT_DISPATCH_CAPACITY"

    # Shared summary verifies exact 10,000 L dispatch consumed
    assert plan.total_liters_dispatched == 10000.0
    assert (
        plan.shared_capacity_summary.depot_dispatch_utilization["DEP-NORTH"]["remaining_capacity"]
        == 0.0
    )


def test_constrained_planner_degrades_to_fallback_on_risk_engine_failure() -> None:
    """Verify ConstrainedPlanner catches model exceptions and falls back to FallbackPlanner."""
    snap = _make_fallback_test_snapshot(tank_fill_pct=10.0)
    resilience_mgr = ResilienceManager()

    mock_risk_engine = MagicMock()
    mock_risk_engine.assess_fleet_risk.side_effect = RuntimeError("Forecasting model NaN crash")

    planner = ConstrainedPlanner(
        risk_engine=mock_risk_engine,
        resilience_manager=resilience_mgr,
    )

    plan = planner.generate_plan(snap)

    assert plan.is_fallback is True
    assert "Forecasting model NaN crash" in plan.fallback_reason
    assert len(plan.actions) == 1
    assert plan.actions[0].needs_human_review is True
    assert resilience_mgr.forecaster_failed is True

    # Mode diagnosis reflects DEGRADED_FALLBACK
    diagnosis = resilience_mgr.diagnose()
    assert diagnosis.mode == OperationalMode.DEGRADED_FALLBACK
    assert diagnosis.forecaster_failed is True


def test_decision_service_persists_fallback_recommendations_and_audit_event(tmp_path) -> None:
    """Verify DecisionService creates Recommendation objects with human review and audit event."""
    db_file = tmp_path / "test_decisions.db"
    audit_ledger = AuditLedger(db_path=str(db_file))
    snap = _make_fallback_test_snapshot(tank_fill_pct=10.0)

    planner = FallbackPlanner()
    plan = planner.generate_plan(snap, fallback_reason="Simulated test fallback")

    mock_client = MagicMock()
    service = DecisionService(
        client=mock_client,
        audit_ledger=audit_ledger,
        planner=MagicMock(),
    )

    recs = service.generate_recommendations_from_plan(plan, snap)
    assert len(recs) == 1
    rec = recs[0]

    assert rec.needs_human_review is True
    assert rec.confidence_score == 0.25
    assert rec.confidence_level == "LOW"
    assert rec.signals.get("is_fallback") is True
    assert rec.status == RecommendationStatus.PROPOSED

    # Verify audit ledger recorded the FALLBACK_POLICY_ACTIVATED event
    history = audit_ledger.get_audit_events(limit=10)
    assert len(history) == 1
    event = history[0]
    assert event["event_type"] == "FALLBACK_POLICY_ACTIVATED"
    assert "fallback policy" in event["operator_notes"]
    assert event["details"].get("needs_human_review") is True
