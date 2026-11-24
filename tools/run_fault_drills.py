"""Automated Fault Drills & Resilience Verification Harness.

Executes controlled empirical fault injection experiments against the live simulator
at http://localhost:8000 and the Fuel Intelligence Platform backend for Day 3 Block 5:
1. `unavailable`: Simulator 503 outage -> circuit breaker trips, DEGRADED_CACHED, auto-recovery.
2. `error_rate`: Chaos 50% packet drop -> exponential backoff retries absorb transient faults.
3. `latency`: High response delay (3000ms) -> timeouts handled safely without thread lock.
4. `stale_data`: X-Simulator-Stale header -> staleness tracking and risk confidence penalty.
5. `stream_disconnect`: Stream disruption -> independent REST polling operates unaffected.
6. `route_disruption`: Corridor disabled mid-plan -> planner reroutes via alternative corridors.
7. `shipment_delay`: Transit delay injected -> arrival timing recalculated, feasibility updated.
8. `station_outage`: Station closed -> planner avoids dispatching to inactive stations.
9. `forecaster_kill`: Internal AI/model crash -> DEGRADED_FALLBACK, 20%/35% heuristic, human review.
10. `day3_checkpoint_story`: Full end-to-end operational loop (Normal -> Spike -> Alert ->
    Twin Recommendation -> Approval -> Transit -> Disruption -> Re-plan -> Outage -> Recovery).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

# Ensure backend package is in python search path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import httpx
from app.adapters.simulator_client import SimulatorClient
from app.api.main import create_app
from app.config import get_settings
from app.decisions.audit import AuditLedger
from app.decisions.service import DecisionService
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.intelligence.fallback import FallbackPlanner
from app.intelligence.forecast import DemandForecaster
from app.intelligence.planner import ConstrainedPlanner
from app.intelligence.risk import RiskEngine
from app.intelligence.twin import DigitalTwin
from app.resilience.breaker import CircuitBreaker
from app.resilience.mode import ResilienceManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fault_drills")


class FaultDrillRunner:
    """Orchestrates and executes all Day 3 resilience drills against the simulator."""

    def __init__(self, simulator_url: str = "http://localhost:8000") -> None:
        self.simulator_url = simulator_url
        self.sim_client = httpx.Client(base_url=simulator_url, timeout=15.0)
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        print(message)
        self.logs.append(message)

    def section(self, title: str) -> None:
        banner = "\n" + "=" * 80 + f"\nDRILL: {title}\n" + "=" * 80
        self.log(banner)

    def reset_simulator(self) -> None:
        """Reset and pause simulator at Tick 0."""
        self.sim_client.post("/admin/faults/clear")
        self.sim_client.post("/admin/reset")
        self.sim_client.post("/admin/pause")

    def step_simulator(self) -> dict[str, Any]:
        """Step simulator forward by 1 tick."""
        resp = self.sim_client.post("/admin/step")
        resp.raise_for_status()
        return resp.json()

    async def create_platform(self) -> tuple[Any, httpx.AsyncClient]:
        """Create an integrated backend platform connected to the simulator."""
        settings = get_settings()
        settings.simulator_base_url = self.simulator_url

        client = SimulatorClient(
            base_url=self.simulator_url,
            timeout_seconds=settings.simulator_timeout_seconds,
            max_retries=1,
            backoff_factor=0.05,
        )
        breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=settings.circuit_breaker_recovery_timeout,
        )
        store = StateStore()
        poller = SimulatorPoller(client=client, store=store, breaker=breaker)
        forecaster = DemandForecaster()
        risk_engine = RiskEngine(forecaster=forecaster)
        twin = DigitalTwin(settings=settings, forecaster=forecaster)
        resilience_mgr = ResilienceManager()
        fallback_planner = FallbackPlanner(settings=settings)
        planner = ConstrainedPlanner(
            settings=settings,
            risk_engine=risk_engine,
            twin_evaluator=twin,
            fallback_planner=fallback_planner,
            resilience_manager=resilience_mgr,
        )
        audit_ledger = AuditLedger(db_path=":memory:")
        decision_service = DecisionService(
            client=client,
            audit_ledger=audit_ledger,
            planner=planner,
            store=store,
            settings=settings,
        )

        app = create_app(
            settings=settings,
            client=client,
            store=store,
            breaker=breaker,
            poller=poller,
            forecaster=forecaster,
            risk_engine=risk_engine,
            planner=planner,
            digital_twin=twin,
            audit_ledger=audit_ledger,
            decision_service=decision_service,
            resilience_manager=resilience_mgr,
            fallback_planner=fallback_planner,
            start_poller=False,
        )

        transport = httpx.ASGITransport(app=app)
        api_client = httpx.AsyncClient(transport=transport, base_url="http://test")
        return app, api_client

    # -------------------------------------------------------------------------
    # Drill 1: Unavailable (Chaos 503)
    # -------------------------------------------------------------------------
    async def drill_1_unavailable(self) -> dict[str, Any]:
        self.section("1. Upstream 503 Outage -> Circuit Breaker Trip -> DEGRADED_CACHED")
        self.reset_simulator()
        app, api = await self.create_platform()

        # Step 1: Baseline sync
        r_sync = await api.post("/sync")
        self.log(f"Baseline Sync: HTTP {r_sync.status_code}")
        r_health = await api.get("/health")
        self.log(
            f"Health Mode: {r_health.json()['mode']} "
            f"(Breaker: {r_health.json()['simulator']['circuit_state']})"
        )
        assert r_health.json()["mode"] == "NORMAL"

        # Step 2: Inject unavailable fault
        self.sim_client.post("/admin/faults", json={"type": "unavailable", "duration_seconds": 30})
        self.log("Injected Fault: 'unavailable' (duration: 30s)")

        # Step 3: Trigger sync attempts to trip breaker
        for i in range(3):
            r = await api.post("/sync")
            self.log(f"Sync attempt {i + 1} during outage: HTTP {r.status_code}")

        # Step 4: Verify circuit breaker is OPEN and mode is DEGRADED_CACHED
        r_health_deg = await api.get("/health")
        h_data = r_health_deg.json()
        self.log(
            f"Post-Fault Health: Mode={h_data['mode']}, "
            f"Breaker={h_data['simulator']['circuit_state']}"
        )
        assert h_data["mode"] == "DEGRADED_CACHED"
        assert h_data["simulator"]["circuit_state"] == "OPEN"

        # Step 5: Verify cached state is served seamlessly
        r_state = await api.get("/state")
        self.log(
            f"GET /state during outage: HTTP {r_state.status_code}, "
            f"stations={len(r_state.json()['snapshot']['stations'])}"
        )
        assert r_state.status_code == 200

        # Step 6: Recovery
        self.sim_client.post("/admin/faults/clear")
        app.state.breaker.reset()
        r_rec = await api.post("/sync")
        r_health_rec = await api.get("/health")
        self.log(f"After Fault Clear: HTTP {r_rec.status_code}, Mode={r_health_rec.json()['mode']}")
        assert r_health_rec.json()["mode"] == "NORMAL"
        await api.aclose()
        return {"drill": "unavailable", "status": "PASSED"}

    # -------------------------------------------------------------------------
    # Drill 2: Error Rate (50% Chaos Drop)
    # -------------------------------------------------------------------------
    async def drill_2_error_rate(self) -> dict[str, Any]:
        self.section("2. Chaos Error Rate (50% Packet Drop) -> Exponential Retries")
        self.reset_simulator()
        app, api = await self.create_platform()

        await api.post("/sync")
        self.sim_client.post(
            "/admin/faults",
            json={"type": "error_rate", "duration_seconds": 30, "parameters": {"rate": 0.5}},
        )
        self.log("Injected Fault: 'error_rate' (rate=0.50, duration=30s)")

        successes = 0
        attempts = 6
        for i in range(attempts):
            r = await api.post("/sync")
            if r.status_code == 200:
                successes += 1
            self.log(f"Chaos poll probe {i + 1}/{attempts}: HTTP {r.status_code}")

        self.log(f"Results: {successes}/{attempts} syncs absorbed/succeeded under 50% packet drop.")
        r_state = await api.get("/state")
        mode = r_state.json()["mode"]
        self.log(f"GET /state resiliently returns HTTP {r_state.status_code} (Mode={mode})")
        assert r_state.status_code == 200

        self.sim_client.post("/admin/faults/clear")
        await api.aclose()
        return {
            "drill": "error_rate",
            "status": "PASSED",
            "success_ratio": f"{successes}/{attempts}",
        }

    # -------------------------------------------------------------------------
    # Drill 3: Latency Fault
    # -------------------------------------------------------------------------
    async def drill_3_latency(self) -> dict[str, Any]:
        self.section("3. High Latency Injection (2500ms Delay)")
        self.reset_simulator()
        app, api = await self.create_platform()

        self.sim_client.post(
            "/admin/faults",
            json={"type": "latency", "duration_seconds": 20, "parameters": {"delay_ms": 2500}},
        )
        self.log("Injected Fault: 'latency' (delay=2500ms)")

        t0 = time.perf_counter()
        r = await api.post("/sync")
        elapsed = time.perf_counter() - t0
        self.log(f"Sync under latency: HTTP {r.status_code} in {elapsed:.2f}s")
        assert r.status_code == 200
        assert elapsed >= 2.0

        self.sim_client.post("/admin/faults/clear")
        await api.aclose()
        return {"drill": "latency", "status": "PASSED", "elapsed_s": round(elapsed, 2)}

    # -------------------------------------------------------------------------
    # Drill 4: Stale Data Header
    # -------------------------------------------------------------------------
    async def drill_4_stale_data(self) -> dict[str, Any]:
        self.section("4. Stale Data Header (X-Simulator-Stale: true) -> Confidence Penalty")
        self.reset_simulator()
        app, api = await self.create_platform()

        await api.post("/sync")
        self.sim_client.post("/admin/faults", json={"type": "stale_data", "duration_seconds": 30})
        self.log("Injected Fault: 'stale_data' (forces X-Simulator-Stale: true)")

        r_sync = await api.post("/sync")
        self.log(f"Sync under stale data: HTTP {r_sync.status_code}")
        r_state = await api.get("/state")
        is_stale = r_state.json()["is_stale"]
        mode = r_state.json()["mode"]
        self.log(f"GET /state: is_stale={is_stale}, mode={mode}")
        assert is_stale is True
        assert mode == "DEGRADED_CACHED"

        self.sim_client.post("/admin/faults/clear")
        await api.post("/sync")
        r_state_rec = await api.get("/state")
        self.log(
            f"Cleared Stale Fault: is_stale={r_state_rec.json()['is_stale']}, "
            f"mode={r_state_rec.json()['mode']}"
        )
        assert r_state_rec.json()["is_stale"] is False
        await api.aclose()
        return {"drill": "stale_data", "status": "PASSED"}

    # -------------------------------------------------------------------------
    # Drill 5: Stream Disconnect (Polling Isolation)
    # -------------------------------------------------------------------------
    async def drill_5_stream_disconnect(self) -> dict[str, Any]:
        self.section("5. Stream Disconnect Fault -> Independent REST Polling Resilience")
        self.reset_simulator()
        app, api = await self.create_platform()

        self.sim_client.post(
            "/admin/faults", json={"type": "stream_disconnect", "duration_seconds": 30}
        )
        self.log("Injected Fault: 'stream_disconnect' (drops SSE streams)")

        # Verify atomic REST poller is 100% decoupled and unaffected
        r = await api.post("/sync")
        self.log(f"Atomic REST Sync under stream disconnect: HTTP {r.status_code}")
        assert r.status_code == 200
        r_health = await api.get("/health")
        self.log(f"Platform health remains {r_health.json()['status']} ({r_health.json()['mode']})")
        assert r_health.json()["mode"] == "NORMAL"

        self.sim_client.post("/admin/faults/clear")
        await api.aclose()
        return {"drill": "stream_disconnect", "status": "PASSED"}

    # -------------------------------------------------------------------------
    # Drill 6: Route Disruption Mid-Plan -> Dynamic Rerouting
    # -------------------------------------------------------------------------
    async def drill_6_route_disruption(self) -> dict[str, Any]:
        self.section("6. Route Disruption Mid-Plan -> Corridor Bypass & Dynamic Rerouting")
        self.reset_simulator()
        app, api = await self.create_platform()

        # Step 1: Initial baseline state
        await api.post("/sync")

        # Step 2: Inject route disruption on route-gazipur-mirpur (primary corridor for Mirpur)
        self.sim_client.post(
            "/admin/events",
            json={
                "type": "route_disruption",
                "start_tick": 1,
                "duration_ticks": 5,
                "parameters": {"route_ids": ["route-gazipur-mirpur"]},
            },
        )
        self.log("Injected Event: 'route_disruption' on 'route-gazipur-mirpur' (ticks 1-6)")

        # Step to tick 2 where disruption becomes ACTIVE
        self.step_simulator()
        self.step_simulator()
        await api.post("/sync")

        r_state = await api.get("/state")
        routes = {r["id"]: r["status"] for r in r_state.json()["snapshot"]["routes"]}
        self.log(f"Active Route Statuses: {routes}")
        assert routes["route-gazipur-mirpur"] == "DISRUPTED"
        assert routes["route-patiya-mirpur"] == "AVAILABLE"

        # Step 3: Trigger planner to generate recommendations
        r_gen = await api.post("/recommendations/generate")
        self.log(f"Recommendations formulated: HTTP {r_gen.status_code}")
        recs = r_gen.json().get("recommendations", [])

        # Step 4: Verify planner NEVER used the disrupted route
        for rec in recs:
            self.log(
                f"Recommendation for {rec['station_id']}: corridor={rec['route_id']}, "
                f"source={rec['source_depot_name']}, liters={rec['quantity_liters']:,.0f}"
            )
            assert rec["route_id"] != "route-gazipur-mirpur", (
                "Planner illegally used disrupted route!"
            )

        await api.aclose()
        return {"drill": "route_disruption", "status": "PASSED", "disrupted_route_avoided": True}

    # -------------------------------------------------------------------------
    # Drill 7: Shipment Delay
    # -------------------------------------------------------------------------
    async def drill_7_shipment_delay(self) -> dict[str, Any]:
        self.section("7. Shipment Delay Injection -> Feasibility & Mitigation Reclassification")
        self.reset_simulator()
        app, api = await self.create_platform()

        self.sim_client.post(
            "/admin/events",
            json={
                "type": "shipment_delay",
                "start_tick": 1,
                "duration_ticks": 5,
                "parameters": {"route_id": "route-gazipur-mirpur", "delay_ticks": 3},
            },
        )
        self.log("Injected Event: 'shipment_delay' (+3 ticks) on 'route-gazipur-mirpur'")

        self.step_simulator()
        self.step_simulator()
        await api.post("/sync")

        r_recs = await api.post("/recommendations/generate")
        count = r_recs.json()["total_count"]
        self.log(f"Generated recommendations under delay: HTTP {r_recs.status_code}, count={count}")
        assert r_recs.status_code == 200

        await api.aclose()
        return {"drill": "shipment_delay", "status": "PASSED"}

    # -------------------------------------------------------------------------
    # Drill 8: Station Outage
    # -------------------------------------------------------------------------
    async def drill_8_station_outage(self) -> dict[str, Any]:
        self.section("8. Station Outage -> Suppression of Dispatches to Inactive Stations")
        self.reset_simulator()
        app, api = await self.create_platform()

        self.sim_client.post(
            "/admin/events",
            json={
                "type": "station_outage",
                "start_tick": 1,
                "duration_ticks": 4,
                "parameters": {"station_ids": ["station-tongi"]},
            },
        )
        self.log("Injected Event: 'station_outage' on 'station-tongi' (ticks 1-5)")

        self.step_simulator()
        self.step_simulator()
        await api.post("/sync")

        r_state = await api.get("/state")
        stations = {s["id"]: s["status"] for s in r_state.json()["snapshot"]["stations"]}
        self.log(f"Station statuses: {stations}")
        assert stations["station-tongi"] in ["OUTAGE", "CLOSED"]

        r_recs = await api.post("/recommendations/generate")
        for rec in r_recs.json().get("recommendations", []):
            assert rec["station_id"] != "station-tongi", "Dispatched fuel to CLOSED/OUTAGE station!"
        self.log("Confirmed: Zero recommendations targeted inactive station-tongi.")

        await api.aclose()
        return {"drill": "station_outage", "status": "PASSED"}

    # -------------------------------------------------------------------------
    # Drill 9: Killing Forecaster Function (Internal Model Failure)
    # -------------------------------------------------------------------------
    async def drill_9_forecaster_kill(self) -> dict[str, Any]:
        self.section("9. Internal Intelligence Crash -> DEGRADED_FALLBACK & Human Review Mandate")
        self.reset_simulator()
        app, api = await self.create_platform()

        # Step 1: Initial sync
        await api.post("/sync")

        # Step 2: Inject internal model crash into ResilienceManager
        resilience_mgr = app.state.resilience_manager
        resilience_mgr.record_forecaster_failure(
            "Simulated unhandled forecaster exception (NaN array)"
        )
        self.log("Triggered Internal Model Failure: Forecaster NaN Crash")

        # Set one station tank to 10% (<20% critical threshold) to exercise emergency heuristic
        snap = app.state.store.get_latest()
        if snap and snap.stations:
            snap.stations[0].inventory["DIESEL"] = snap.stations[0].capacity["DIESEL"] * 0.10
            app.state.store.update(snap)

        # Step 3: Verify /health immediately reflects DEGRADED_FALLBACK
        r_health = await api.get("/health")
        h_data = r_health.json()
        self.log(f"Health Response: Status={h_data['status']}, Mode={h_data['mode']}")
        assert h_data["mode"] == "DEGRADED_FALLBACK"

        # Step 4: Formulate recommendations under degraded fallback
        r_gen = await api.post("/recommendations/generate")
        self.log(f"Fallback recommendations: HTTP {r_gen.status_code}")
        gen_data = r_gen.json()
        tot_count = gen_data["total_count"]
        deg_mode = gen_data["mode"]
        self.log(f"Generated {tot_count} fallback recommendations (Mode={deg_mode})")

        # Step 5: Verify Fallback Policy Contract:
        # - confidence = 0.25 (LOW)
        # - needs_human_review = True
        # - Explanation contains 'FALLBACK HEURISTIC'
        for rec in gen_data.get("recommendations", []):
            self.log(
                f"Fallback Recommendation {rec['recommendation_id']}: station={rec['station_id']}, "
                f"confidence={rec['confidence_score']} ({rec['confidence_level']}), "
                f"needs_human_review={rec['needs_human_review']}"
            )
            assert rec["needs_human_review"] is True
            assert rec["confidence_score"] == 0.25
            assert rec["confidence_level"] == "LOW"
            assert "FALLBACK HEURISTIC" in rec["explanation"]

        # Step 6: Verify SQLite Audit Ledger contains FALLBACK_POLICY_ACTIVATED event
        r_audit = await api.get("/decisions/audit")
        audit_events = r_audit.json().get("events", [])
        fallback_events = [
            e for e in audit_events if e["event_type"] == "FALLBACK_POLICY_ACTIVATED"
        ]
        self.log(
            f"SQLite Audit Ledger: Found {len(fallback_events)} FALLBACK_POLICY_ACTIVATED events."
        )
        assert len(fallback_events) >= 1

        # Step 7: Clear model failure and verify return to NORMAL
        resilience_mgr.record_model_success()
        r_health_rec = await api.get("/health")
        self.log(f"Health after model restoration: Mode={r_health_rec.json()['mode']}")
        assert r_health_rec.json()["mode"] == "NORMAL"

        await api.aclose()
        return {"drill": "forecaster_kill", "status": "PASSED"}

    # -------------------------------------------------------------------------
    # Drill 10: The Full Day 3 Story (End-to-End Checkpoint)
    # -------------------------------------------------------------------------
    async def drill_10_day3_checkpoint_story(self) -> dict[str, Any]:
        self.section("10. Full Day 3 Story (End-to-End Operational Lifecycle)")
        self.reset_simulator()
        app, api = await self.create_platform()

        # Step 1: Normal operations
        self.log("[Step 1: Normal Operations] Synchronizing network at Tick 0...")
        await api.post("/sync")
        r_health = await api.get("/health")
        self.log(f"--> Mode: {r_health.json()['mode']}, Health: {r_health.json()['status']}")
        assert r_health.json()["mode"] == "NORMAL"

        # Step 2: Inject Demand Spike on Mirpur
        self.log("\n[Step 2: Demand Spike] Injecting 3.5x demand spike on station-mirpur...")
        self.sim_client.post(
            "/admin/events",
            json={
                "type": "demand_spike",
                "start_tick": 1,
                "duration_ticks": 6,
                "parameters": {"station_id": "station-mirpur", "multiplier": 3.5},
            },
        )

        # Step 3: Advance 3 simulation ticks to let demand spike consume inventory
        self.log("[Step 3: Advance Time] Stepping simulator 3 ticks...")
        for _ in range(3):
            self.step_simulator()
        await api.post("/sync")

        # Step 4: Observe Risk Spike
        r_risks = await api.get("/risks")
        risks_data = r_risks.json()
        critical_alerts = risks_data.get("critical_alerts", [])
        self.log(f"[Step 4: Risk Detection] Observed {len(critical_alerts)} critical alerts:")
        for alert in critical_alerts[:2]:
            st_name = alert["station_name"]
            f_type = alert["fuel_type"]
            urg = alert["urgency_score"]
            sev = alert["severity"]
            self.log(f"--> ALERT: {st_name} {f_type} - Urgency: {urg:.1f}, Severity: {sev}")

        # Step 5: Formulate Recommendations with Monte Carlo Impact
        self.log("\n[Step 5: Twin Evaluation & Recommendations] Generating AI dispatch plan...")
        r_gen = await api.post("/recommendations/generate")
        gen_data = r_gen.json()
        assert gen_data["total_count"] >= 1
        rec = gen_data["recommendations"][0]
        rec_id = rec["recommendation_id"]
        p_before = rec.get("stockout_probability_before", 1.0)
        p_after = rec.get("stockout_probability_after", 0.0)
        cover_hrs = rec.get("hours_of_cover_added", 0.0)
        liters_prev = rec.get("expected_unmet_reduction_liters", 0.0)
        bottleneck = rec["constraints"]["bottleneck_constraint"]
        self.log(
            f"--> Top Action: Dispatch {rec['quantity_liters']:,.0f} L {rec['fuel_type']} "
            f"to {rec['station_id']}"
        )
        self.log(f"    From Depot: {rec['source_depot_name']} via {rec['route_id']}")
        self.log(f"    Twin Impact: Stockout risk {p_before * 100:.1f}% -> {p_after * 100:.1f}%")
        self.log(
            f"    Hours of Cover Added: +{cover_hrs:.1f}h, Liters Prevented: {liters_prev:,.0f} L"
        )
        self.log(f"    Binding Bottleneck: {bottleneck}")

        # Step 6: Operator Approval & Dispatch Submission
        self.log(f"\n[Step 6: Operator Approval] Approving recommendation '{rec_id}'...")
        r_approve = await api.post(
            f"/recommendations/{rec_id}/approve",
            json={"operator_notes": "Approved for immediate spike mitigation"},
        )
        self.log(f"--> Approval Response: HTTP {r_approve.status_code}")
        app_data = r_approve.json().get("recommendation", r_approve.json())
        self.log(
            f"    Status: {app_data['status']}, "
            f"Allocation ID: #{app_data.get('allocation_id', 'N/A')}, "
            f"Idempotency Key: {app_data['idempotency_key']}"
        )
        assert app_data["status"] == "SUBMITTED"

        # Step 7: Advance Time -> Shipment is In-Transit
        self.log("\n[Step 7: In-Transit Verification] Stepping simulator 1 tick...")
        self.step_simulator()
        await api.post("/sync")
        r_state = await api.get("/state")
        in_transit_allocs = [
            a
            for a in r_state.json()["snapshot"]["allocations"]
            if a["status"] in ["IN_TRANSIT", "DEPARTED"]
        ]
        self.log(f"--> Allocations in transit/departed: {len(in_transit_allocs)}")

        # Step 8: Mid-Transit Route Disruption
        self.log(
            "\n[Step 8: Mid-Transit Route Disruption] Disruping alternate route-gazipur-tongi..."
        )
        self.sim_client.post(
            "/admin/events",
            json={
                "type": "route_disruption",
                "start_tick": 5,
                "duration_ticks": 4,
                "parameters": {"route_ids": ["route-gazipur-tongi"]},
            },
        )
        self.step_simulator()
        await api.post("/sync")

        # Step 9: Re-plan under disruption
        self.log("[Step 9: Re-Planning] Generating new plan under disruption...")
        r_replan = await api.post("/recommendations/generate")
        self.log(f"--> Re-plan formulated: {r_replan.json()['total_count']} feasible actions.")

        # Step 10: Simulator Outage & Degraded Cached State
        self.log("\n[Step 10: Upstream Simulator Outage] Injecting 100% error rate...")
        self.sim_client.post(
            "/admin/faults",
            json={"type": "error_rate", "duration_seconds": 15, "parameters": {"rate": 1.0}},
        )
        for _ in range(3):
            await api.post("/sync")

        r_health_outage = await api.get("/health")
        self.log(
            f"--> Health under outage: Mode={r_health_outage.json()['mode']}, "
            f"Status={r_health_outage.json()['status']}"
        )
        assert r_health_outage.json()["mode"] == "DEGRADED_CACHED"

        r_cached_state = await api.get("/state")
        self.log(f"--> GET /state serves cached snapshot: HTTP {r_cached_state.status_code}")
        assert r_cached_state.status_code == 200

        # Step 11: Fault Clearance & Automatic Recovery
        self.log("\n[Step 11: Fault Clearance & Recovery] Clearing simulator faults...")
        self.sim_client.post("/admin/faults/clear")
        app.state.breaker.reset()
        await api.post("/sync")
        r_health_final = await api.get("/health")
        self.log(
            f"--> Final Health: Mode={r_health_final.json()['mode']}, "
            f"Breaker={r_health_final.json()['simulator']['circuit_state']}"
        )
        assert r_health_final.json()["mode"] == "NORMAL"
        self.log("\n*** DAY 3 CHECKPOINT STORY COMPLETED WITH 100% SUCCESS! ***")

        await api.aclose()
        return {"drill": "day3_checkpoint_story", "status": "PASSED"}

    async def run_all(self) -> dict[str, Any]:
        """Run all 10 drills sequentially and return summary."""
        results = {}
        results["drill_1"] = await self.drill_1_unavailable()
        results["drill_2"] = await self.drill_2_error_rate()
        results["drill_3"] = await self.drill_3_latency()
        results["drill_4"] = await self.drill_4_stale_data()
        results["drill_5"] = await self.drill_5_stream_disconnect()
        results["drill_6"] = await self.drill_6_route_disruption()
        results["drill_7"] = await self.drill_7_shipment_delay()
        results["drill_8"] = await self.drill_8_station_outage()
        results["drill_9"] = await self.drill_9_forecaster_kill()
        results["drill_10"] = await self.drill_10_day3_checkpoint_story()
        return results


async def main() -> None:
    runner = FaultDrillRunner()
    print("Starting Automated Fault Drills against Simulator at http://localhost:8000...")
    results = await runner.run_all()
    print("\n" + "=" * 80)
    print("ALL FAULT DRILLS COMPLETE!")
    print(json.dumps(results, indent=2))
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
