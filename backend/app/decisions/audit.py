"""SQLite Audit Ledger for Recommendations and Operator Decisions.

Persists every recommendation, state machine transition, and operator intervention in SQLite.
Enforces durable traceability:
1. Records all input parameters, forecast signals, constraints, and impact models at proposal time.
2. Captures state transitions (PROPOSED -> APPROVED/REJECTED -> SUBMITTED/FAILED) with timestamps.
3. Provides query interfaces for decision history, audit compliance, and evaluation harnesses.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.decisions.models import Recommendation, RecommendationStatus
from app.domain.models import FuelType, RiskSeverity
from app.intelligence.planner import (
    AppliedConstraints,
    ConstraintType,
    MitigationType,
    PlanActionAlternative,
)

logger = logging.getLogger(__name__)


class AuditLedger:
    """Durable SQLite storage and event audit trail for fuel allocation decisions."""

    def __init__(self, db_path: str = "data/decisions.db") -> None:
        self.db_path = db_path
        self._is_memory = db_path == ":memory:"
        self._mem_conn: sqlite3.Connection | None = None

        if self._is_memory:
            self._mem_conn = sqlite3.connect(":memory:")
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA foreign_keys=ON;")
        else:
            # Ensure parent directories exist
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """Create or reuse a configured SQLite connection with row factory and pragmas."""
        if self._is_memory and self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        """Initialize database tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    plan_id TEXT,
                    action_id TEXT,
                    created_tick INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    station_name TEXT NOT NULL,
                    fuel_type TEXT NOT NULL,
                    quantity_liters REAL NOT NULL,
                    source_depot_id TEXT NOT NULL,
                    source_depot_name TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    departure_tick INTEGER NOT NULL,
                    expected_arrival_tick INTEGER NOT NULL,
                    transit_ticks INTEGER NOT NULL,
                    urgency_score REAL NOT NULL,
                    severity TEXT NOT NULL,
                    mitigation_type TEXT NOT NULL,
                    unavoidable_shortage_hours REAL,
                    explanation TEXT NOT NULL,
                    signals_json TEXT NOT NULL,
                    constraints_json TEXT NOT NULL,
                    expected_unmet_reduction_liters REAL NOT NULL,
                    hours_of_cover_added REAL NOT NULL,
                    stockout_probability_before REAL NOT NULL,
                    stockout_probability_after REAL NOT NULL,
                    impact_summary TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    confidence_level TEXT NOT NULL,
                    needs_human_review INTEGER NOT NULL,
                    alternatives_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    operator_notes TEXT,
                    approved_at TEXT,
                    rejected_at TEXT,
                    submitted_at TEXT,
                    simulator_allocation_id INTEGER,
                    failure_code TEXT,
                    failure_reason TEXT,
                    raw_inputs_json TEXT
                );

                CREATE TABLE IF NOT EXISTS decision_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    operator_notes TEXT,
                    details_json TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (recommendation_id)
                        REFERENCES recommendations(recommendation_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_rec_station ON recommendations(station_id);
                CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendations(status);
                CREATE INDEX IF NOT EXISTS idx_rec_created_tick ON recommendations(created_tick);
                CREATE INDEX IF NOT EXISTS idx_rec_idempotency ON recommendations(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_audit_rec_id
                    ON decision_audit_log(recommendation_id);
                CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON decision_audit_log(timestamp);
                """
            )

    def save_recommendation(
        self,
        recommendation: Recommendation,
        raw_inputs: dict[str, Any] | None = None,
    ) -> None:
        """Upsert a recommendation and record its state in the audit ledger."""
        rec = recommendation
        signals_json = json.dumps(rec.signals)
        constraints_json = rec.constraints.model_dump_json()
        alternatives_json = json.dumps([a.model_dump() for a in rec.alternatives])
        raw_inputs_json = json.dumps(raw_inputs) if raw_inputs else None

        now_str = datetime.now(UTC).isoformat()
        approved_at_str = rec.approved_at.isoformat() if rec.approved_at else None
        rejected_at_str = rec.rejected_at.isoformat() if rec.rejected_at else None
        submitted_at_str = rec.submitted_at.isoformat() if rec.submitted_at else None

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO recommendations (
                    recommendation_id, plan_id, action_id, created_tick, created_at, updated_at,
                    station_id, station_name, fuel_type, quantity_liters, source_depot_id,
                    source_depot_name, route_id, departure_tick, expected_arrival_tick,
                    transit_ticks, urgency_score, severity, mitigation_type,
                    unavoidable_shortage_hours, explanation, signals_json, constraints_json,
                    expected_unmet_reduction_liters, hours_of_cover_added,
                    stockout_probability_before, stockout_probability_after,
                    impact_summary, confidence_score, confidence_level,
                    needs_human_review, alternatives_json, idempotency_key, status,
                    operator_notes, approved_at, rejected_at, submitted_at,
                    simulator_allocation_id, failure_code, failure_reason, raw_inputs_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(recommendation_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    operator_notes = excluded.operator_notes,
                    approved_at = excluded.approved_at,
                    rejected_at = excluded.rejected_at,
                    submitted_at = excluded.submitted_at,
                    simulator_allocation_id = excluded.simulator_allocation_id,
                    failure_code = excluded.failure_code,
                    failure_reason = excluded.failure_reason;
                """,
                (
                    rec.recommendation_id,
                    rec.plan_id,
                    rec.action_id,
                    rec.created_tick,
                    rec.created_at.isoformat(),
                    now_str,
                    rec.station_id,
                    rec.station_name,
                    rec.fuel_type.value,
                    rec.quantity_liters,
                    rec.source_depot_id,
                    rec.source_depot_name,
                    rec.route_id,
                    rec.departure_tick,
                    rec.expected_arrival_tick,
                    rec.transit_ticks,
                    rec.urgency_score,
                    rec.severity.value,
                    rec.mitigation_type.value,
                    rec.unavoidable_shortage_hours,
                    rec.explanation,
                    signals_json,
                    constraints_json,
                    rec.expected_unmet_reduction_liters,
                    rec.hours_of_cover_added,
                    rec.stockout_probability_before,
                    rec.stockout_probability_after,
                    rec.impact_summary,
                    rec.confidence_score,
                    rec.confidence_level,
                    1 if rec.needs_human_review else 0,
                    alternatives_json,
                    rec.idempotency_key,
                    rec.status.value,
                    rec.operator_notes,
                    approved_at_str,
                    rejected_at_str,
                    submitted_at_str,
                    rec.simulator_allocation_id,
                    rec.failure_code,
                    rec.failure_reason,
                    raw_inputs_json,
                ),
            )

    def record_audit_event(
        self,
        recommendation_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str,
        operator_notes: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append an immutable audit entry to decision_audit_log."""
        now_str = datetime.now(UTC).isoformat()
        details_json = json.dumps(details) if details else None

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO decision_audit_log (
                    recommendation_id, event_type, from_status, to_status,
                    operator_notes, details_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    recommendation_id,
                    event_type,
                    from_status,
                    to_status,
                    operator_notes,
                    details_json,
                    now_str,
                ),
            )

    def _row_to_recommendation(self, row: sqlite3.Row) -> Recommendation:
        """Convert a SQLite row into a validated Recommendation domain model."""
        constraints_dict = json.loads(row["constraints_json"])
        constraints = AppliedConstraints(
            route_max_shipment=constraints_dict["route_max_shipment"],
            depot_available_inventory=constraints_dict["depot_available_inventory"],
            depot_remaining_dispatch_capacity=constraints_dict["depot_remaining_dispatch_capacity"],
            station_effective_free_space=constraints_dict["station_effective_free_space"],
            amount_needed=constraints_dict["amount_needed"],
            bottleneck_constraint=ConstraintType(constraints_dict["bottleneck_constraint"]),
            allocated_quantity=constraints_dict["allocated_quantity"],
        )

        raw_alts = json.loads(row["alternatives_json"])
        alternatives = [
            PlanActionAlternative(
                route_id=alt["route_id"],
                source_depot_id=alt["source_depot_id"],
                source_depot_name=alt["source_depot_name"],
                quantity_liters=alt["quantity_liters"],
                departure_tick=alt["departure_tick"],
                arrival_tick=alt["arrival_tick"],
                transit_ticks=alt["transit_ticks"],
                is_timely=alt["is_timely"],
                mitigation_type=MitigationType(alt["mitigation_type"]),
                bottleneck_constraint=ConstraintType(alt["bottleneck_constraint"]),
                score=alt["score"],
                reason_not_chosen=alt["reason_not_chosen"],
            )
            for alt in raw_alts
        ]

        signals = json.loads(row["signals_json"]) if row["signals_json"] else {}

        return Recommendation(
            recommendation_id=row["recommendation_id"],
            plan_id=row["plan_id"],
            action_id=row["action_id"],
            created_tick=row["created_tick"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            station_id=row["station_id"],
            station_name=row["station_name"],
            fuel_type=FuelType(row["fuel_type"]),
            quantity_liters=row["quantity_liters"],
            source_depot_id=row["source_depot_id"],
            source_depot_name=row["source_depot_name"],
            route_id=row["route_id"],
            departure_tick=row["departure_tick"],
            expected_arrival_tick=row["expected_arrival_tick"],
            transit_ticks=row["transit_ticks"],
            urgency_score=row["urgency_score"],
            severity=RiskSeverity(row["severity"]),
            mitigation_type=MitigationType(row["mitigation_type"]),
            unavoidable_shortage_hours=row["unavoidable_shortage_hours"],
            explanation=row["explanation"],
            signals=signals,
            constraints=constraints,
            expected_unmet_reduction_liters=row["expected_unmet_reduction_liters"],
            hours_of_cover_added=row["hours_of_cover_added"],
            stockout_probability_before=row["stockout_probability_before"],
            stockout_probability_after=row["stockout_probability_after"],
            impact_summary=row["impact_summary"],
            confidence_score=row["confidence_score"],
            confidence_level=row["confidence_level"],
            needs_human_review=bool(row["needs_human_review"]),
            alternatives=alternatives,
            idempotency_key=row["idempotency_key"],
            status=RecommendationStatus(row["status"]),
            operator_notes=row["operator_notes"],
            approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
            rejected_at=datetime.fromisoformat(row["rejected_at"]) if row["rejected_at"] else None,
            submitted_at=(
                datetime.fromisoformat(row["submitted_at"]) if row["submitted_at"] else None
            ),
            simulator_allocation_id=row["simulator_allocation_id"],
            failure_code=row["failure_code"],
            failure_reason=row["failure_reason"],
        )

    def get_recommendation(self, recommendation_id: str) -> Recommendation | None:
        """Retrieve a specific recommendation by its primary ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?;",
                (recommendation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_recommendation(row)

    def get_recommendation_by_idempotency_key(self, idempotency_key: str) -> Recommendation | None:
        """Look up recommendation by its unique deterministic idempotency key."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM recommendations WHERE idempotency_key = ?;",
                (idempotency_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_recommendation(row)

    def list_recommendations(
        self,
        status: RecommendationStatus | str | None = None,
        station_id: str | None = None,
        limit: int = 100,
    ) -> list[Recommendation]:
        """Query recommendations filtered by lifecycle status or station, ordered by urgency."""
        query = "SELECT * FROM recommendations WHERE 1=1"
        params: list[Any] = []

        if status:
            s_val = status.value if hasattr(status, "value") else str(status)
            query += " AND status = ?"
            params.append(s_val)

        if station_id:
            query += " AND station_id = ?"
            params.append(station_id)

        query += " ORDER BY urgency_score DESC, created_tick DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            return [self._row_to_recommendation(row) for row in cursor.fetchall()]

    def get_audit_events(
        self,
        recommendation_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch audit log entries, optionally scoped to a single recommendation."""
        query = "SELECT * FROM decision_audit_log"
        params: list[Any] = []

        if recommendation_id:
            query += " WHERE recommendation_id = ?"
            params.append(recommendation_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            results = []
            for r in rows:
                results.append(
                    {
                        "id": r["id"],
                        "recommendation_id": r["recommendation_id"],
                        "event_type": r["event_type"],
                        "from_status": r["from_status"],
                        "to_status": r["to_status"],
                        "operator_notes": r["operator_notes"],
                        "details": json.loads(r["details_json"]) if r["details_json"] else {},
                        "timestamp": r["timestamp"],
                    }
                )
            return results

    def get_audit_statistics(self) -> dict[str, Any]:
        """Compute consolidated decision and audit metrics."""
        with self._get_connection() as conn:
            # Status counts
            cursor = conn.execute(
                """
                SELECT status, COUNT(*) as count, SUM(quantity_liters) as total_liters
                FROM recommendations
                GROUP BY status;
                """
            )
            status_stats: dict[str, dict[str, Any]] = {}
            total_count = 0
            total_liters = 0.0

            for row in cursor.fetchall():
                st = row["status"]
                c = row["count"]
                liters = row["total_liters"] or 0.0
                status_stats[st] = {"count": c, "total_liters": liters}
                total_count += c
                total_liters += liters

            # Total audit events
            cursor = conn.execute("SELECT COUNT(*) FROM decision_audit_log;")
            total_events = cursor.fetchone()[0]

            # Rejection rate
            rejected_count = status_stats.get("REJECTED", {}).get("count", 0)
            submitted_count = status_stats.get("SUBMITTED", {}).get("count", 0)
            approved_count = status_stats.get("APPROVED", {}).get("count", 0)
            failed_count = status_stats.get("FAILED", {}).get("count", 0)

            total_decided = rejected_count + submitted_count + approved_count + failed_count
            rejection_rate = (rejected_count / total_decided) if total_decided > 0 else 0.0

            return {
                "total_recommendations": total_count,
                "total_liters_recommended": total_liters,
                "total_audit_events": total_events,
                "status_breakdown": status_stats,
                "submitted_count": submitted_count,
                "rejected_count": rejected_count,
                "failed_count": failed_count,
                "rejection_rate": rejection_rate,
            }

    def clear(self) -> None:
        """Wipe all audit tables (used during test teardown or admin reset)."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM decision_audit_log;")
            conn.execute("DELETE FROM recommendations;")
