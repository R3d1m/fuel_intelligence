# Fuel Intelligence Platform: Engineering Log & Notes

Personal engineering log documenting architecture insights, simulator quirks, design trade-offs, and daily progress.

---

## Day 1

### Block 1: Repo Hygiene & Engineering Standards
- **Standardization:** Set up clean separation between `adapters` (simulator HTTP/fault boundary), `domain` (Pydantic models), `ingest` (poller and state store), `intelligence` (forecast, risk, planner, digital twin), `decisions` (recommendations, approvals, SQLite audit ledger), `resilience` (circuit breaker, degraded modes), and `api` (FastAPI).
- **12-Factor Configuration:** Built configuration management via `pydantic-settings` with environment variable overrides and rigorous validation constraints (preventing invalid timeouts, negative retries, or zero polling frequencies).
- **Tooling:** Configured `pyproject.toml` with `ruff` for strict code linting and formatting, `pytest` + `pytest-asyncio` for asynchronous unit testing, and pinned simulator container specs.
- **Convention:** Enforced conventional commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`) for an auditable, professional Git history.

### Block 2: Simulator Discovery & Empirical Verification
- **Tick Granularity Trap:** Found that `GET /v1/instance` reports `tick_minutes: 15` despite `TICK_MINUTES: 8` in docker-compose. Confirmed our rule: never hardcode tick durations; calculate rates dynamically from `/v1/instance`.
- **Destination Capacity Quirk:** The simulator accepts allocations exceeding station tank ullage if multiple shipments are in-transit/pending. It does NOT count in-flight fuel against destination capacity! Our planner must locally track effective free space to avoid post-arrival fuel overflow.
- **Lost Fuel on Failure:** When an allocation fails at departure due to a route disruption (`status: FAILED`), the fuel is NOT refunded to the depot inventory! Fuel is permanently lost. Our planner must actively verify route health before dispatching.
- **Cancellation & Idempotency:** Cancelling a `PENDING` allocation immediately refunds the depot, but permanently burns the `idempotency_key`. Replaying a valid payload returns HTTP 201 Created (not 200).
- **Error Discrepancy:** Discovered 3 different error payload formats: `{"error": {"code": ...}}` for chaos faults, `{"detail": {"code": ...}}` for domain errors, and `{"detail": [...]}` for Pydantic validation failures. The adapter must handle all three.
- **Reproducibility:** Confirmed byte-identical determinism across simulator resets when stepped with `/admin/step`.

### Block 3: Typed Domain Models & Adapter Boundary
- **Unified Domain Models:** Implemented typed Pydantic v2 domain models for all simulation entities (`Depot`, `Station`, `Route`, `SupplyArrival`, `Allocation`, `SimEvent`, `InstanceInfo`, `DemandHistoryRecord`, `SimulatorHealth`, `SimulatorMetrics`, `SimulatorSnapshot`) and enums (`FuelType`, `DepotStatus`, `StationStatus`, `RouteStatus`, `SupplyArrivalStatus`, `AllocationStatus`, `OperationalMode`).
- **Effective Ullage Modeling:** Built `Station.effective_free_capacity(fuel, in_transit_liters)` directly into domain models to protect against the destination capacity quirk discovered in Block 2.
- **Tri-Shape Error Parsing:** Built `parse_simulator_error()` in `backend/app/adapters/errors.py` to seamlessly parse chaos/injected faults (`error.code`), domain rule rejections (`detail.code`), and FastAPI 422 lists (`detail -> [loc, msg]`), raising typed custom exceptions (`SimulatorUnavailableError`, `SimulatorDomainError`, `SimulatorValidationError`).
- **Resilient Simulator Client:** Built `SimulatorClient` using `httpx.AsyncClient` with exponential backoff and randomized jitter on transient 503 and network connection errors, while fast-failing domain/validation errors without wasteful retries.
- **Staleness Tracking:** Wrapped all responses in `SimulatorResponse[T]` that automatically checks case-insensitive `X-Simulator-Stale` HTTP headers, enabling downstream modules to flag degraded or stale data.
- **Test Coverage:** Created comprehensive unit test suite (`test_domain_models.py`, `test_error_parsing.py`, `test_simulator_client.py`) using `respx` mock transports to verify retries, error parsing, and domain validations with 100% green tests.

### Block 4: Circuit Breaker, State Store & Atomic Poller
- **Circuit Breaker Pattern:** Implemented custom 3-state `CircuitBreaker` (`CLOSED`, `OPEN`, `HALF_OPEN`) in `backend/app/resilience/breaker.py` with configurable failure thresholds, recovery timeout cooldowns, and coroutine execution wrapper (`call()`). Automatically trips to protect subsystems during upstream outages and executes single-probe recovery in `HALF_OPEN`.
- **In-Memory State Store:** Created `StateStore` in `backend/app/ingest/store.py` holding both `latest_snapshot` and `last_good_snapshot`. Automatically preserves the last valid state when upstream reports stale data (`X-Simulator-Stale: true`) or fails, calculating real-time snapshot age and staleness.
- **Atomic Background Poller:** Built `SimulatorPoller` in `backend/app/ingest/poller.py` utilizing `asyncio.gather` to fetch all simulation entities concurrently. Assembles them into an atomic `SimulatorSnapshot` and feeds `StateStore`. Guards against calling the simulator when the circuit breaker is `OPEN`.
- **Comprehensive Unit Testing:** Added complete test suites (`test_circuit_breaker.py`, `test_state_store.py`, `test_poller.py`) testing circuit state machines, deterministic mock clocks, store staleness heuristics, and poller task lifecycles (48/48 green tests).

### Block 5: API Skeleton & Operational Mode Diagnostics
- **Operational Mode Resolution:** Implemented centralized mode computation and diagnosis in `backend/app/resilience/mode.py` resolving authoritative system state across `NORMAL`, `DEGRADED_CACHED`, and `DEGRADED_FALLBACK` with human-readable diagnostic reasons.
- **FastAPI Core & Lifespan Management:** Constructed `create_app()` factory in `backend/app/api/main.py` with async lifespan hooks managing background poller lifecycle, singleton injection, and clean connection teardown.
- **REST Endpoints & Schemas:** Designed typed Pydantic API schemas in `backend/app/api/schemas.py` and implemented endpoints in `backend/app/api/routes.py`:
  - `GET /health`: Platform liveness, simulator circuit state, poller telemetry, and store health.
  - `GET /state`: Delivers latest validated snapshot in `NORMAL` mode; seamlessly serves cached last-known-good state in `DEGRADED_CACHED` mode during outages.
  - `POST /sync`: Manual deterministic polling trigger for testing and ad-hoc synchronization.
- **Dependency Injection Framework:** Implemented FastAPI dependency providers in `backend/app/api/deps.py` supporting frictionless dependency overrides for unit and mock tests.
- **Empirical Fault Drill Verification:** Validated resilience against the running simulator container: tested baseline normal synchronization, injected `error_rate` (100%) via `/admin/faults`, confirmed circuit breaker tripped to `OPEN`, verified `/state` continued serving cached snapshot with `DEGRADED_CACHED`, cleared faults via `/admin/faults/clear`, and verified automatic return to `NORMAL`.
- **Test Suite & Code Hygiene:** Expanded automated test suite with `test_operational_mode.py` and `test_api_endpoints.py`. All 61 unit/integration tests passing (61/61 green), zero Ruff warnings. Day 1 Foundation & Discovery successfully completed!

---

## Day 2

### Block 1: Demand Forecast & Anomaly Detection
- **Multi-Level Time-Series Engine:** Implemented `DemandForecaster` in `backend/app/intelligence/forecast.py`:
  - **Level 1 (EWMA):** Recursive Exponentially Weighted Moving Average ($s_t = \alpha y_t + (1-\alpha)s_{t-1}$) of per-tick demand with dynamic conversion to liters/hour calculated from real `instance.tick_minutes` ($60 / \text{tick\_minutes}$).
  - **Level 2 (Diurnal Profile Learning):** Empirical hour-of-day seasonal factor learning across hours $0 \dots 23$ with Bayesian shrinkage towards $1.0$ ($M = 4.0$). Smoothly falls back to pure EWMA when historical records span fewer than 3 unique hours or cold-start data is sparse.
  - **Cold-Start Nominal Fallback:** Profile-aware baseline table (`urban_high`, `industrial`, `highway`, `regional`) providing initial rates prior to observation history, scaled by `station.demand_multiplier`.
- **Dual-Signal Anomaly Detection:** Built `detect_anomaly()` evaluating both:
  1. *Simulator station demand_multiplier:* Explicit multiplier status ($> 1.05$) indicating injected or scheduled demand spikes.
  2. *Statistical Demand Surge:* Ratio of short-term moving average (last 3 ticks) against long-term baseline ($\ge 1.35x$).
  3. *Actionable Narrative:* Generates detailed multi-signal diagnostic explanations and severity classifications (`OK`, `WATCH`, `CRITICAL`).
- **Sequential Walk-Forward Backtesting:** Built out-of-sample rolling 1-step-ahead backtest engine measuring real-time forecast error:
  - Mean Absolute Error (MAE in L/tick).
  - Mean Absolute Percentage Error (MAPE in %).
  - Root Mean Squared Error (RMSE in L/tick).
  - Residual standard deviation ($\sigma_{\text{residual}}$) used to calibrate 95% confidence bounds ($\pm 1.96\sigma$) for forward projection trajectories and Monte Carlo digital twin noise.
- **REST Endpoints & Observability:**
  - Added `GET /forecasts` (fleet-wide demand rates, aggregate prediction error metrics, and active anomalies).
  - Added `GET /forecasts/{station_id}` (station-specific forecasts and anomaly telemetry).
- **Test Coverage & Verification:**
  - Added unit test suite `backend/tests/test_forecast.py` covering cold-start, EWMA math, dynamic rates, diurnal shrinkage, backtest error, anomaly dual-signals, and confidence bounds.
  - Added integration tests in `backend/tests/test_api_endpoints.py`.
  - Executed live empirical validation against running Docker simulator: 8 ticks stepped, 96 demand records analyzed, out-of-sample backtest observed $3.41\text{ L/tick}$ MAE and $6.36\%$ MAPE, and live injected `demand_spike` ($2.0x$) was flagged in real time.
  - Test suite now stands at **76 passed tests (76/76 green)**, zero Ruff lint/format errors.

### Block 2: Risk Engine & Stockout Projection
- **Stock-Flow Forward Projection:** Implemented `RiskEngine` in `backend/app/intelligence/risk.py`:
  - Simulates forward inventory trajectories tick-by-tick across a configurable horizon (default: 24 ticks = 6.0h):
    $$\text{Inventory}_{t} = \max(0.0, \min(\text{Capacity}, \text{Inventory}_{t-1} + \text{Incoming Arrivals}_t) - \text{Demand}_t)$$
  - Tracks first stockout tick, hours to stockout ($\Delta t \times \text{tick\_minutes} / 60$), total unmet liters, minimum projected inventory, and tank overflow protection.
- **In-Transit and Pending Shipment Mechanics:**
  - Accurately integrates active shipments (`IN_TRANSIT`) based on route transit duration or explicit `expected_arrival_tick`.
  - Enforces empirical finding: detects when a `PENDING` allocation is assigned to a `DISRUPTED` route (which fails at departure in the simulator without refund) and excludes it from counted arrival stock with clear operator warnings.
- **Arrival-Feasibility & Unavoidable Stockout Reasoning:**
  - Dynamically calculates the minimum transit time ($\min \text{transit\_ticks} + 1$) across all available routes to each station.
  - Flags `is_unavoidable_stockout = True` and computes `unavoidable_shortage_hours` when inventory will deplete before the fastest replenishment could physically arrive.
- **Multi-Factor Risk Severity Resolution:**
  - `CRITICAL`: Stockout within $\le 3.0$ hours, tank inventory $\le 20\%$ of capacity, or unavoidable stockout.
  - `WATCH`: Stockout within $\le 8.0$ hours, tank inventory $\le 35\%$, active demand spike anomaly, or complete route isolation (all connecting routes disrupted).
  - `OK`: Healthy inventory, no projected stockout within horizon, and stable supply.
- **Confidence Scoring & Uncertainty Communication:**
  - Blends data freshness (penalized when data age $> 10$s or flagged stale by simulator `X-Simulator-Stale`) with empirical forecast accuracy (MAPE from backtest):
    $$C = C_{\text{freshness}} \times C_{\text{forecast}} \in [0.05, 1.0]$$
  - Categorizes confidence into `HIGH` ($\ge 0.80$), `MEDIUM` ($0.50 \dots 0.80$), and `LOW` ($< 0.50$), automatically setting `needs_human_review = True` on low confidence or critical severity.
- **Actionable Diagnostic Drivers:**
  - Exposes `RiskDrivers` containing initial fill ratios, burn rates, incoming shipment tallies, route status, and human-readable explanation strings explaining *why* the risk exists for operator transparency.
- **Fleet-Wide Aggregation & Urgency Alerts:**
  - Aggregates fleet severities (`critical_count`, `watch_count`, `ok_count`), identifies earliest fleet-wide stockout, and sorts `active_alerts` by a scalar urgency ranking combining severity tier, stockout proximity, tank emptiness, and demand surges.
  - Implemented bulk depot reserve risk evaluation (`DepotFuelRisk`) modeling effective dispatchable inventory against pending commitments.
- **REST Endpoints & Integration:**
  - Added `GET /risks` (fleet-wide risk assessment, active alerts, overall severity).
  - Added `GET /risks/{station_id}` (station-specific inventory trajectories, drivers, and alerts).
- **Test Coverage & Live Validation:**
  - Created unit test suite `backend/tests/test_risk.py` with 13 comprehensive tests covering nominal scenarios, low-tank thresholds, stockout timing, in-transit mitigation, unavoidable shortage, isolated stations, disrupted pending allocations, tank overflow capping, confidence penalties, and urgency sorting.
  - Added API integration tests in `backend/tests/test_api_endpoints.py` testing `/risks` and `/risks/{station_id}` under normal, 404, and degraded conditions.
  - Full test suite now stands at **92 passed tests (92/92 green)** with 0 Ruff errors.
  - Validated live against the running simulator container: tested nominal operations and verified real-time escalation to `WATCH` and urgency alerts when injecting a $3.5\times$ demand spike.

### Block 3: Dashboard v1 (Streamlit Thin Client)
- **Thin Client Architecture & Resilient API Client:**
  - Implemented `BackendApiClient` in `dashboard/client.py`: a resilient boundary wrapping `httpx.Client` with fast-fail connect timeouts (`connect=1.5s`), structured backend error decoding (`detail.message`), connection health tracking (`is_connected`), and comprehensive error handling preventing UI crashes.
  - Enforced zero business logic in the UI: all forecasts, risk evaluations, circuit breaker states, and urgency scores are derived strictly from the FastAPI backend (`/health`, `/state`, `/forecasts`, `/risks`, `/sync`).
- **High-Visibility Operational Mode Banner:**
  - Renders top-level system status across `NORMAL` (green with circuit/data age telemetry), `DEGRADED_CACHED` (amber with cached snapshot age and breaker cooldown), `DEGRADED_FALLBACK` (red with heuristic indicators), and `DISCONNECTED` (red with actionable operator recovery tips).
- **4-Column KPI Status Panel:**
  - Upstream Simulator Gateway (circuit breaker state, failures count, cooldown remaining).
  - State Repository (ingestion counter, snapshot freshness/staleness, data age, background poller lifecycle).
  - Demand Intelligence (total fleet consumption rate in L/h, walk-forward out-of-sample MAE, MAPE %, active demand anomalies).
  - Fleet Risk Overview (worst fleet severity tier, critical/watch alert counts, earliest stockout ETA).
- **Priority Urgency Alert Deck:**
  - Cards sorted strictly by scalar urgency score descending.
  - Color-coded severity indicators (`CRITICAL`, `WATCH`, `OK`), stockout proximity timers, unavoidable shortage warnings, inventory vs capacity progress bars, burn rates, diagnostic root causes (`drivers.explanation`), confidence tiers, and human review mandates (`⚠️ HUMAN REVIEW MANDATED`).
- **Tabbed Operational Views:**
  - **Risk Matrix:** Color-coded table across all (station, fuel) pairs with soft background highlights (`CRITICAL` soft red, `WATCH` soft amber, `OK` soft green), fill ratios, stockout times, projected unmet demand, in-flight arrivals, and confidence levels.
  - **Station Tanks:** Visual tank levels, fill ratios, and real-time demand multiplier spike badges (`🔥 DEMAND SPIKE ACTIVE`).
  - **Bulk Depots:** Fuel inventories, dispatch capacity vs current dispatch load, and depot reserve warnings.
  - **Logistics Routes:** Route availability, transit times/ticks, max shipment sizes, and active disruption warnings.
  - **Simulation & Chaos Events:** Active injected spikes, route disruptions, shipment delays, and remaining ticks.
- **Modern Auto-Refresh (`st.fragment`):**
  - Utilized Streamlit's `@st.fragment(run_every=2)` for partial-DOM background updates without screen flickering or full-app rerun latency.
- **Test Coverage & Verification:**
  - Built `backend/tests/test_dashboard_client.py` (7 tests) and `backend/tests/test_dashboard_app.py` (11 tests).
  - Verified with headless `streamlit.testing.v1.AppTest`: 0 exceptions, clean degraded/offline error rendering.
  - Full automated test suite expanded to **110 passed tests (110/110 green)** with zero Ruff lint/format errors.

### Block 4: Containers & Orchestration
- **Production Containerization:**
  - `backend/Dockerfile`: Multi-stage ready, lightweight `python:3.11-slim` container with non-root user (`appuser`), layer caching for pip requirements, exposed port `8001`, and internal healthcheck probe (`GET /health`).
  - `dashboard/Dockerfile`: Streamlit production container with configured headless runtime, disabled usage telemetry, non-root user with home directory cache permissions, exposed port `8501`, and internal healthcheck probe (`GET /_stcore/health`).
  - `.dockerignore`: Filtered VCS artifacts, Python bytecode (`__pycache__`), virtual environments (`.venv`), testing caches, and coverage reports to keep Docker build context slim and fast.
- **Docker Compose Networking & Health Orchestration:**
  - `docker-compose.yml`: Fully wired 3-tier service topology:
    - `simulator`: Official BUP simulator on port `8000` with network alias `simulator-api` and healthcheck (`/v1/health`).
    - `backend`: FastAPI backend on port `8001`, connects to `http://simulator:8000`, starts with `depends_on: simulator` condition `service_healthy`.
    - `dashboard`: Streamlit UI on port `8501`, connects to backend via internal container network `http://backend:8001`, starts with `depends_on: backend` condition `service_healthy`.
- **Validation:**
  - Successfully built both images using `docker compose build`.
  - Started the full 3-container stack and verified all three services report `healthy` with clean compose networking.

---

## Day 3

### Block 1: The Constrained Greedy Planner
- **Multi-Constraint Optimization Engine:** Implemented `ConstrainedPlanner` in `backend/app/intelligence/planner.py`:
  - **Urgency-Ordered Greedy Prioritization:** Evaluates at-risk `(station, fuel)` targets sorted strictly by continuous scalar urgency score descending (broken by stockout proximity and unmet liters).
  - **5 Simultaneous Physical & Operational Constraints:** Computes exact allocation volume as the minimum of:
    1. Route maximum tanker capacity (`route.max_shipment`).
    2. Available depot inventory above configured operational reserve (`planner_depot_reserve_liters`).
    3. Remaining depot dispatch capacity in departure tick (`depot.dispatch_capacity_per_tick` minus commitments).
    4. Effective station tank free space (`station.effective_free_capacity`), accounting for in-transit and pending fuel to prevent post-arrival overflow.
    5. Net replenishment deficit (`amount_needed`) based on projected unmet demand and nominal target fill level (`planner_target_fill_ratio`).
  - **Active Bottleneck Attribution:** Accurately identifies which of the 5 bounds formed the binding constraint (`bottleneck_constraint`) and exports complete constraint audits for operator transparency.
- **Feasibility & Arrival Mitigation Timing:**
  - Compares projected shipment arrival tick ($\text{departure} + \text{transit\_ticks}$) against station first stockout tick:
    - `FULL_PREVENTION`: Arrival $\le \text{first\_stockout\_tick}$, eliminating shortage before tank runs dry.
    - `PARTIAL_MITIGATION`: Arrival $> \text{first\_stockout\_tick}$, terminates shortage and computes exact unavoidable shortage hours.
    - `PREVENTATIVE_RESTOCK`: Proactive replenishment for low-tank WATCH status with no projected stockout.
- **Shared State Bookkeeping:**
  - Solves the multi-destination shared capacity challenge: dynamically decrements shared depot dispatch capacity, depot inventory per fuel, and destination station free space as greedy choices are made, preventing concurrent over-commitments.
  - Summarizes network-wide capacity utilization in `SharedCapacitySummary`.
- **Candidate Ranking & Pluggable Digital Twin Protocol:**
  - Designed `TwinEvaluatorProtocol` with baseline `HeuristicTwinEvaluator` scoring candidate options on unmet demand reduction, hours of cover added, delivery timeliness, and route transit duration.
  - Prepared architectural drop-in interface for Day 3 Block 2 Monte Carlo digital twin (`twin.py`).
  - For every recommended action, captures the primary top choice along with the next 2 highest-ranked alternative routes/depots, including rationale for why they were not chosen.
- **Diagnostic Root-Cause Tracking for Unmet Demands:**
  - Records unserved at-risk stations in `unmet_targets` with structured failure codes (`ALL_ROUTES_DISRUPTED`, `DEPOT_OR_ROUTE_CONSTRAINED`, `STATION_FULL`, `BELOW_MIN_SHIPMENT`) and actionable operator explanations.
- **REST Endpoints & Domain Schemas:**
  - Added `PlanResponse` schema in `backend/app/api/schemas.py`.
  - Added `GET /plan` endpoint in `backend/app/api/routes.py` with operational mode diagnostics and fallback resilience.
  - Added dependency injection provider `get_planner` in `backend/app/api/deps.py` and factory wiring in `main.py`.
- **Test Coverage & Verification:**
  - Built comprehensive test suite `backend/tests/test_planner.py` (16 unit and integration tests) verifying nominal allocation, urgency sorting, shared dispatch capacity quotas, depot inventory reserves, effective ullage overflow capping, route tanker limits, disrupted route avoidance, closed depot avoidance, timely vs partial mitigation, alternatives extraction, and REST API integration.
  - Full platform test suite now stands at **126 passed tests (126/126 green)** with zero Ruff lint/format errors.

### Block 2: The Digital Twin (Monte Carlo Simulation)
- **Vectorized Monte Carlo Forward Simulation:** Implemented `DigitalTwin` in `backend/app/intelligence/twin.py`:
  - **Empirically Calibrated Stochastic Noise:** Generates Gaussian perturbation matrices ($\sim \mathcal{N}(0, \sigma_{\text{residual}}^2)$) scaled directly from walk-forward backtest error variance (`forecast.metrics.residual_std`).
  - **Paired Counterfactual Scenarios:** Simulates hundreds of simultaneous forward paths (default: 300 trials) comparing:
    1. *Baseline Scenario:* No intervention; projects natural inventory runout and stockout timing under demand uncertainty.
    2. *Treatment Scenario:* Integrates candidate shipment arrival at specific delivery tick, evaluating residual risk.
  - **Physical Tank Capacity Enforcement:** Clamps inventory at every forward step ($\min(\text{capacity}, \text{inventory} + \text{arrivals}_t)$), faithfully modeling tank ullage and avoiding phantom stock overflow.
  - **Byte-Identical Determinism:** Uses NumPy's `default_rng(seed)` with configurable seed (`twin_random_seed: 42`) ensuring byte-identical reproducibility across independent runs.
- **Uncertainty Quantification & Trajectory Confidence Bands:**
  - **Stockout Probability ($P_{\text{stockout}}$):** Fraction of stochastic trials experiencing complete fuel depletion or unmet demand.
  - **Expected Unmet Customer Demand:** Mean unserved liters across trials with empirical 95% confidence intervals ($[P_{2.5}, P_{97.5}]$).
  - **Percentile Trajectories:** Extracts conservative $P_{10}$, median $P_{50}$, and optimistic $P_{90}$ inventory trajectories over future ticks for dashboard visualization.
  - **Comparative Benefit Metrics:** Computes absolute stockout probability drop ($\Delta P = P_{\text{before}} - P_{\text{after}}$), relative risk reduction percentage, expected unmet demand reduction, and added hours of operational cover.
- **Planner Protocol Integration:**
  - `DigitalTwin` implements `TwinEvaluatorProtocol`, serving as the drop-in ranking evaluator for `ConstrainedPlanner`.
  - Replaces provisional heuristic ranker with empirical Monte Carlo risk reduction numbers.
  - Wired into `create_app()` factory in `backend/app/api/main.py` and `get_digital_twin` in `backend/app/api/deps.py`.
- **REST Endpoints & Schemas:**
  - Added `TwinSimulateRequest` and `TwinSimulationResponse` schemas in `backend/app/api/schemas.py`.
  - Implemented `POST /twin/simulate` (interactive ad-hoc scenario evaluation) and `GET /twin/{station_id}/{fuel_type}` in `backend/app/api/routes.py`.
- **Test Coverage & Verification:**
  - Created test suite `backend/tests/test_twin.py` (13 tests) verifying fixed seed reproducibility, stochastic variation across seeds, significant stockout risk reduction, confidence interval ordering ($L \le M \le U$), trajectory percentiles ($P_{10} \le P_{50} \le P_{90}$), tank ullage capping, residual variance sensitivity, protocol conformance, planner integration, and API endpoints.
  - Full automated platform test suite expanded to **139 passed tests (139/139 green)** with zero Ruff lint or format warnings.






