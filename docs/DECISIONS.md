# Architectural Decision Records (ADRs)

This document records the major design decisions, trade-offs, alternatives considered, and rationale for the Fuel Intelligence Platform.

---

### ADR-001: Dedicated Adapter Layer for Simulator Isolation

- **Context:** The Fuel simulator can inject faults (503s, latency, stale data, malformed schemas). Exposing simulator client logic across the application leads to brittle code.
- **Decision:** Encapsulate all simulator interactions behind a dedicated `SimulatorClient` adapter that enforces timeouts, retries with backoff/jitter, circuit breaking, and converts raw payloads into validated Pydantic models.
- **Alternatives Rejected:** Direct `httpx` calls in route handlers or background jobs.
- **Consequence:** The rest of the backend code deals only with clean domain models and predictable custom exceptions.

---

### ADR-002: REST Polling as Primary Source of Truth with SSE as Advisory Hint

- **Context:** The simulator provides REST endpoints and an optional Server-Sent Events (SSE) feed (`/v1/stream`), but the SSE connection drops under `stream_disconnect` faults with no `Last-Event-ID` replay.
- **Decision:** Rely on periodic REST polling into a central `StateStore` as the authoritative source of truth. Use SSE solely as a low-latency trigger to schedule an immediate REST refresh.
- **Alternatives Rejected:** Pure SSE event-sourcing with in-memory projection.
- **Consequence:** Reconnection drops never corrupt internal state; system is resilient to stream disconnect faults.

---

### ADR-003: EWMA Time-Series Forecasting with Empirical Residual Error Backtest

- **Context:** Regional demand fluctuates based on daily profiles and injected demand spikes. We need fast, interpretable forecasts and stockout predictions.
- **Decision:** Implement an Exponentially Weighted Moving Average (EWMA) model with hour-of-day scaling and track prediction residuals (MAE/MAPE) in real-time.
- **Alternatives Rejected:** Heavy deep-learning time-series models (e.g. LSTM/Transformers) or opaque black-box regressions.
- **Consequence:** Zero cold-start latency, lightweight execution in containers, transparent explainability for operators, and calibrated uncertainty intervals for the digital twin.

---

### ADR-004: Constrained Greedy Allocation Planner with Monte Carlo Digital Twin

- **Context:** Allocating fuel involves multi-depot, multi-route, and multi-station constraints (transit time, tank capacity, dispatch limits).
- **Decision:** Use a priority-ranked greedy planner that checks all physical constraints and uses a Monte Carlo forward-projection digital twin to quantify stockout risk reduction (e.g. 72% -> 19%).
- **Alternatives Rejected:** Model-free Reinforcement Learning (RL).
- **Consequence:** Instant decision generation, deterministic repeatability, strict adherence to physical limits, and transparent trade-off inspection for judges and operators.

---

### ADR-005: Human-in-the-Loop Operator Approval for Dispatches

- **Context:** Autonomous actions during simulated crises could trigger wasteful or conflicting allocations without operator context.
- **Decision:** System generates inspectable recommendations; allocations are only executed to `POST /v1/allocations` upon operator approval via dashboard or API.
- **Alternatives Rejected:** Unchecked automated dispatch loop.
- **Consequence:** Directly satisfies the core hackathon requirement and safety guardrails.

---

### ADR-006: SQLite for Decision Audit Ledger

- **Context:** Need persistent, queryable storage of every recommendation, approval, rejection, and execution outcome with zero deployment friction.
- **Decision:** Use SQLite stored locally, accessed via standard library / SQLModel.
- **Alternatives Rejected:** External PostgreSQL or MongoDB database container.
- **Consequence:** Zero operational overhead, single-file portability, instant reproducibility across developer laptops and judge environments.

---

### ADR-007: Streamlit as a Decoupled Thin-Client Operator Dashboard

- **Context:** Operators need a rich UI to monitor tanks, inspect risk alerts, and review recommendations.
- **Decision:** Build a Streamlit application that interacts exclusively via the backend FastAPI REST endpoints.
- **Alternatives Rejected:** Complex React/Vue frontend or embedding business logic inside Streamlit.
- **Consequence:** Rapid iteration, clean separation of concerns, and full backend observability independent of UI.

---

### ADR-008: Explicit Operational Modes (NORMAL, DEGRADED_CACHED, DEGRADED_FALLBACK)

- **Context:** The simulator and models can fail independently. Silent failures or generic error pages degrade operator trust.
- **Decision:** Compute an explicit operational mode surfaced prominently in API health checks, dashboard banners, and Prometheus metrics.
- **Alternatives Rejected:** Generic HTTP 500 error propagation.
- **Consequence:** Clear visibility during fault drills; operators immediately know whether they are viewing real-time data or cached last-known-good states.
