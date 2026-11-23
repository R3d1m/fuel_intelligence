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



