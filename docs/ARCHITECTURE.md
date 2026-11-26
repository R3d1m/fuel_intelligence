# System Architecture: Fuel Intelligence Platform

```
Fuel Simulator (HTTP, SSE)
        │
   [adapters]      SimulatorClient: timeouts, retries, circuit breaker,
        │          error parsing, stale detection, schema validation
        ▼
   [domain]        Typed models (Pydantic): Depot, Station, Route, Arrival, Allocation
        │
   [ingest]        Poller -> StateStore (latest snapshot + last-good cache)
        │
   [intelligence]  Forecast -> Risk -> Planner -> Digital Twin (Monte Carlo What-If)
        │
   [decisions]     Recommendation -> Human Approval -> POST -> Audit Log (SQLite)
        │
   [api]           FastAPI: /health, /state, /sync, /forecasts, /risks, /plan,
                   /twin/simulate, /recommendations, /decisions/*, /metrics
        │
   [ui]            React + TypeScript Console (frontend/) & Streamlit (dashboard/)
                   — both strictly thin clients of the FastAPI backend
   [tools & ops]   Docker Compose, Prometheus, GitHub Actions CI,
                   Fault Drills, Policy Evaluator, Locust Load Test
```

## Core Design Principles

1. **Simulator Isolation (Adapter Boundary):**
   The Fuel Supply Simulator is an external, volatile dependency subject to faults (latency, 503 errors, stale headers). Only the adapter layer (`backend/app/adapters/`) communicates directly with the simulator over HTTP. The rest of the platform operates strictly against validated, typed domain models.

2. **Explicit Degraded Modes:**
   The platform is always in an explicit operational mode:
   - `NORMAL`: Simulator is healthy, data is fresh, forecaster is active.
   - `DEGRADED_CACHED`: Simulator is unreachable or stale; serving cached last-known-good state; blocking risky automated dispatches.
   - `DEGRADED_FALLBACK`: Forecaster/model failure; running rule-based heuristic allocation with clear `needs_human_review` flags.
     The operational mode is continuously exposed to operators on the dashboard and exported to Prometheus.

3. **Human-in-the-Loop for Consequential Actions:**
   As specified in the hackathon brief, human operators must retain review capability. The decision engine proposes structured `Recommendation` objects with risk reduction scores, constraints, and alternatives. Physical allocations are dispatched only upon operator approval.

4. **Deterministic Auditability:**
   Every recommendation, approval, rejection, and simulator response is permanently recorded with input snapshots in an SQLite audit ledger for post-incident review and simulation replay.
