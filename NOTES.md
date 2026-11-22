# Fuel Intelligence Platform: Engineering Log & Notes

Personal engineering log documenting architecture insights, simulator quirks, design trade-offs, and daily progress.

---

## Day 1: Foundation and Discovery

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
