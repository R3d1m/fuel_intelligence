# Fuel Intelligence Platform: 4-Day Solo Build Guide

*A day-by-day plan for building a solid, well-engineered platform on the BUP Fuel Supply Simulator, written so that an employer reading your repo can see real engineering.*

## 1. The strategy: what employers actually look at

They are choosing 4 people. They will not run a leaderboard; they will open your repo, read the README, skim the code, look at the commit history, and ask you why you built it that way. So you win by making these things **visible and verifiable**:

| Employers want to see | How you prove it |
| --- | --- |
| You can design a system | `docs/ARCHITECTURE.md` with a diagram, clear layers, and a one-paragraph reason for each |
| You make and justify trade-offs | `docs/DECISIONS.md`: 6 to 8 short decision records (what, why, what you rejected) |
| It works and is better than a naive approach | `docs/EVIDENCE.md`: a measured before/after table from your own evaluation harness |
| You handle failure | A fault-drill table: injected fault, what the system did, screenshot or log |
| You write maintainable code | Small modules, typed models, tests, linting, CI badge |
| You are creative, not flashy | One or two original ideas, done properly (see section 3) |
| It is honest | README that claims only what works, plus a Known Limits section |
| It is yours | You can explain every module; a `NOTES.md` learning log in your own words |

**Golden rules**

1. A smaller system that is correct, tested and explained beats a bigger one that is half-working.
2. Every claim in the README must be backed by code or evidence in the repo.
3. Commit small and often with clear messages (for example `feat(client): add retry with backoff`). The history is part of the portfolio.
4. Everything the simulator does not tell you, you verify by experiment and write down.
5. Use AI assistance for boilerplate if you like, but you must understand and be able to explain everything you commit. Interviewers will ask.

## 2. Architecture (keep it clean, not complicated)

```
Fuel Simulator (HTTP, SSE)
        |
   [adapters]  SimulatorClient: timeouts, retries, circuit breaker,
        |      error parsing, stale detection, schema validation
        v
   [domain]    typed models (pydantic): Depot, Station, Route, Arrival, Allocation
        |
   [ingest]    Poller -> StateStore (latest snapshot + last-good cache)
        |
   [intelligence]  forecast -> risk -> planner -> twin (what-if)
        |
   [decisions] Recommendation -> human approval -> POST -> audit log (SQLite)
        |
   [api]       FastAPI: /health /state /risks /recommendations /decisions /metrics
        |
   [dashboard] Streamlit (thin client of the API)
   [ops]       Docker, compose, Prometheus metrics, GitHub Actions
```

**Design principles to state in your docs**

- **The simulator is an external dependency.** Only the adapter layer talks to it; the rest of the code sees clean validated models. This is the core architectural idea and easy to explain.
- **Explicit degraded modes.** The system is always in one of: `NORMAL`, `DEGRADED_CACHED` (simulator unreachable or stale, serving last-good state), `DEGRADED_FALLBACK` (forecaster failed, using a simple rule). The mode is shown on the dashboard and exported as a metric.
- **Humans approve consequential actions.** The brief requires it. Recommendations are proposals; approval triggers the POST.
- **Everything is auditable.** Every recommendation, approval, rejection and API result is stored with inputs and reasons.

**Repository layout**

```
fuel_intelligence/
  backend/app/
    config.py                  # env-based settings
    adapters/simulator_client.py, errors.py
    domain/models.py
    ingest/poller.py, store.py
    intelligence/forecast.py, risk.py, planner.py, twin.py, fallback.py
    decisions/service.py, audit.py
    resilience/breaker.py, mode.py
    observability/metrics.py, logging.py
    api/routes.py, main.py
  backend/tests/
  dashboard/app.py
  tools/evaluate.py            # A/B evaluation harness
  tools/loadtest/locustfile.py
  docs/ARCHITECTURE.md DECISIONS.md EVIDENCE.md RUNBOOK.md SIMULATOR_NOTES.md
  .github/workflows/ci.yml
  docker-compose.yml  README.md  NOTES.md
```

## 3. The creative parts (your differentiators)

Keep the number small and make them excellent.

1. **A local digital twin with Monte Carlo what-if.** The simulator has no `/simulate` endpoint, but the brief wants expected impact (stockout risk 72% to 19%). Your twin projects inventory forward using your forecast and demand noise, and runs a few hundred random trials with and without a proposed shipment. The output is a real probability, not a guess.
2. **Arrival-feasibility reasoning.** Shipments depart next tick and take 2 to 4 ticks. If fuel cannot arrive before the projected stockout, say so plainly (`unavoidable shortage of about N hours; this shipment shortens it`). Most teams will not notice this.
3. **Evidence-based evaluation harness.** The simulator is deterministic, so you can replay the same scenario with different policies and compare results. This is real engineering rigor.
4. **Decision audit and replay.** Every decision stored with its inputs, so an operator can see why something was recommended.

## 4. Day-by-day plan

About 8 hours per day is assumed. Times are estimates; if something takes double, simplify it and use the cut lines in section 8. Each day ends with a working checkpoint. Do not start the next day with the checkpoint broken.

### Day 1: Foundation and discovery

**Goal:** a trustworthy connection to the simulator, a repo that looks professional, and verified knowledge of how the simulator really behaves.

**Block 1 (1h): Repo hygiene**

- Restructure the repo as in section 2. Add `pyproject.toml` (or `requirements.txt`), `ruff` for linting, `pytest`, a `.env.example`, and `config.py` using `pydantic-settings` (base URL, timeouts, poll interval).
- *Learn:* Python project layout, virtual environments, environment-based configuration (12-factor config), conventional commits.

**Block 2 (2h): Discover the simulator and write it down** Run the simulator paused, use `/docs` and `/admin`, and answer these by experiment, recording each in `docs/SIMULATOR_NOTES.md`:

- Actual `tick_minutes` from `/v1/instance` (the guide and compose file disagree, so never hard-code it).
- Does the demand you see per tick match the profile math (for example, Mirpur diesel is about 8,500 L/day times the hour factor)?
- After you POST an allocation, when does it depart? What happens to depot inventory immediately?
- Does in-transit fuel count against a station's destination-capacity check?
- What happens to depot inventory when an allocation ends `FAILED` (route disrupted at departure)?
- Does cancel refund? Can you re-use a key? Is idempotent replay 200 or 201?
- Is the simulation really deterministic? Reset twice, step the same number of ticks, compare state.
- *Learn:* experimental method, reading an OpenAPI spec, curl or httpx basics.

**Block 3 (2h): Typed domain models and the adapter**

- `domain/models.py`: pydantic models for every resource, with enums (`FuelType`, `RouteStatus`, and so on).
- `adapters/simulator_client.py` using `httpx`: one `get` and one `post` path with timeouts; retry with exponential backoff and jitter on 503 and connect errors; parse all three error shapes (`detail.code`, `error.code`, FastAPI 422 list); read `X-Simulator-Stale`; return a result object that says whether the data was stale.
- *Learn:* HTTP semantics, idempotency, retries with backoff and jitter, pydantic validation, custom exceptions.

**Block 4 (1.5h): Circuit breaker + poller + state store**

- `resilience/breaker.py`: a small circuit breaker (CLOSED, OPEN, HALF\_OPEN) with failure threshold and cool-down. About 40 lines; write it yourself.
- `ingest/poller.py`: an asyncio background loop that fetches all resources, validates them, and writes one consistent `Snapshot` (with tick and fetched-at time) into `store.py`. Keep the last good snapshot.
- *Learn:* asyncio tasks, the circuit breaker pattern, snapshot consistency.

**Block 5 (1.5h): API skeleton + tests**

- `GET /health` (liveness plus dependency status), `GET /state` (latest snapshot, mode, age of data).
- Tests: error-shape parser, stale header, breaker state transitions, retry count. Use `respx` or `httpx.MockTransport` to fake the simulator.
- *Learn:* FastAPI basics, dependency injection, mocking HTTP.

**Day 1 checkpoint:** `/state` returns live data. Inject an `error_rate` fault from `/admin`: the endpoint keeps answering from cache with mode `DEGRADED_CACHED`, then recovers when the fault clears. Tests are green. About 8 to 10 commits.

### Day 2: Intelligence core, dashboard, containers

**Goal:** the system can see risk, explain it, and show it to an operator, and it runs with one command.

**Block 1 (2.5h): Demand forecast + anomaly detection**

- `intelligence/forecast.py`. Level 1 (required): an exponentially weighted moving average of recent per-tick demand for each (station, fuel), pulled from `/v1/demand-history` with an explicit `limit`. Convert to liters per hour using the real `tick_minutes`.
- Level 2 (if time): learn an hour-of-day factor from history so the forecast anticipates the daily peaks. Fall back to the EWMA if there is not enough data.
- Detection: flag a spike when recent demand is well above the longer baseline, or when the station's `demand_multiplier` rises above 1.0. Report both signals in the explanation.
- Backtest: predict the next tick, compare with the observed `demand_liters`, and keep MAE and MAPE as a metric. The brief lists prediction error as expected observability.
- *Learn:* time-series smoothing, forecast error metrics, anomaly detection by ratio or z-score, why you measure a model instead of trusting it.

**Block 2 (2h): Risk engine**

- `intelligence/risk.py`: for every (station, fuel), project inventory over a horizon: `inventory + scheduled arrivals + in-transit shipments - forecast demand`. Output hours to stockout, the tick of first stockout, a severity (OK, WATCH, CRITICAL) and a confidence value (based on forecast error and data freshness).
- Include which inputs drove the risk, so the dashboard can show why.
- *Learn:* projecting stock-flow, thresholds, communicating uncertainty.

**Block 3 (2h): Dashboard v1**

- `dashboard/app.py` (Streamlit, thin client of your API): inventory by station and fuel, depot status, route status, active events, risk table with colors, alerts sorted by urgency, a mode banner, and a status panel (simulator, backend, forecaster). Use a recent Streamlit auto-refresh (`st.fragment` with `run_every`).
- *Learn:* Streamlit layout, calling an API from a UI, keeping a UI free of business logic.

**Block 4 (1.5h): Containers**

- Dockerfiles for the backend and dashboard, update `docker-compose.yml` to run simulator, backend, dashboard with healthchecks and `depends_on`. Test `docker compose up` from a fresh clone.
- *Learn:* Dockerfile basics, compose networking (service names, not localhost), healthchecks.

**Day 2 checkpoint:** one command starts everything. Inject a `demand_spike`, press Run or Step in `/admin`, and watch risk turn red with an explanation and a rising forecast error metric. Tests for the forecast and risk math are green.

### Day 3: Decisions, the twin, approval, resilience

**Goal:** the complete loop works: detect, recommend, simulate impact, approve, act, survive faults.

**Block 1 (2.5h): The planner**

- `intelligence/planner.py`, a constrained greedy planner. For each at-risk (station, fuel), ordered by urgency:
  - consider each route that is `AVAILABLE`, ends at that station, and whose depot is `OPEN` or `CONSTRAINED`;
  - quantity is the smallest of: route `max_shipment`, depot inventory minus a reserve, remaining depot dispatch capacity (shared across this plan), station free tank space (including in-transit), and the amount actually needed;
  - check feasibility: departure next tick plus `transit_ticks` must come before the projected stockout, otherwise mark it as a partial mitigation;
  - rank candidates by the twin's risk reduction.
- Return the top choice plus the next two alternatives and every constraint that applied.
- *Learn:* constrained decision-making, greedy algorithms and their limits, why shared capacity needs bookkeeping. (Optional later: model it as a linear program with `scipy.optimize.linprog` and compare.)

**Block 2 (2h): The digital twin**

- `intelligence/twin.py`: step inventory forward tick by tick using the forecast plus random noise sized from the observed forecast residuals; apply arrivals and the candidate shipment; use numpy and a fixed random seed for reproducibility.
- Run 200 to 500 trials with and without the shipment. Output stockout probability before and after, expected unmet liters, and a confidence interval.
- *Learn:* Monte Carlo simulation, random seeds, vectorizing with numpy, uncertainty communication.

**Block 3 (1.5h): Recommendations, approval, audit**

- `decisions/service.py`: a `Recommendation` object (what, why, signals, constraints, expected impact, confidence, alternatives). States: `PROPOSED`, `APPROVED`, `REJECTED`, `SUBMITTED`, `FAILED`.
- Approval endpoint submits `POST /v1/allocations` with a deterministic idempotency key (hash of station, fuel, tick, quantity), so retries cannot double-ship. Map every simulator error code to a human-readable message.
- `decisions/audit.py`: store every recommendation and outcome in SQLite, including the inputs at the time.
- Dashboard: recommendation cards with Approve and Reject buttons, impact numbers, alternatives, and a decision history table.
- *Learn:* state machines, idempotency keys, SQLite with the standard library or SQLModel, audit design.

**Block 4 (1h): Degraded modes and fallbacks**

- `resilience/mode.py`: a single place that computes the current mode.
- Fallback policy (`intelligence/fallback.py`): if the forecast or twin throws or times out, use a plain rule (for example, any tank below 20 percent is CRITICAL; send the largest feasible amount). Low confidence sets `needs_human_review` with a visible badge.
- Malformed or missing simulator fields: reject the snapshot, keep the last good one, raise an alert.
- *Learn:* graceful degradation, timeouts around risky calls, failing safe.

**Block 5 (1h): Fault drills** Run each and capture a screenshot or log for `docs/EVIDENCE.md`: `unavailable`, `error_rate`, `latency`, `stale_data`, `stream_disconnect` (or the lack of SSE if you polled), `route_disruption` mid-plan, `shipment_delay`, `station_outage`, plus killing your own forecaster function.

**Day 3 checkpoint:** run the full story: normal, spike, alert, recommendation with a 72%-to-19%-style number, approve, shipment arrives, route disruption, re-plan, then a simulator fault, degraded banner, recovery.

### Day 4: Proof, operations, documentation

**Goal:** turn a working system into a convincing submission.

**Block 1 (2h): Evaluation harness (your strongest evidence)**

- `tools/evaluate.py` (offline script, may use `/admin`): for each scenario, reset, pause, optionally inject events, then loop: step one tick, read state, ask the policy for allocations, auto-approve them (clearly labeled as an evaluation mode only), and record `/v1/metrics`.
- Compare three policies: **no action**, **naive** (refill when a tank is below 30 percent from the nearest depot), and **yours**.
- Scenarios: normal day, demand spike, route disruption, shipment delay, combined crisis. Same seed for all.
- Report service level, unmet liters, allocation failures, and station-hours with unmet demand. Put the table in `docs/EVIDENCE.md`. If your policy does not win in some scenario, say so and explain; that honesty is a plus.
- *Learn:* controlled experiments, baselines, reproducibility, reporting results honestly.

**Block 2 (1.5h): Observability**

- `observability/metrics.py` with `prometheus-client`, exposed at `/metrics`: request count and latency per endpoint, simulator call latency and errors, breaker state, mode, alerts raised, recommendations by outcome, fallback activations, forecast MAE.
- JSON structured logs with event names (`decision.approved`, `simulator.breaker_open`, `mode.changed`).
- Dashboard status panel reading these. Grafana only if everything else is done.
- *Learn:* metrics vs logs, histograms and percentiles, structured logging.

**Block 3 (1h): Load test**

- `tools/loadtest/locustfile.py`: mixed traffic on `/state`, `/risks`, and `/recommendations`. Record average, p50, p95, p99, requests per second and error rate at a few concurrency levels, plus where it degrades. Note CPU and memory from `docker stats`. Put the table in `docs/EVIDENCE.md`.
- *Learn:* load vs stress, percentiles, finding the bottleneck.

**Block 4 (1h): CI**

- `.github/workflows/ci.yml`: lint, tests, Docker build, on every push. Add the badge to the README.
- *Learn:* GitHub Actions basics.

**Block 5 (2.5h): Documentation and demo**

- `README.md`: what it does, 60-second quick start, architecture picture, what is implemented, evidence summary, known limits, simulated-data disclaimer. Remove anything not built.
- `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/RUNBOOK.md` (how to operate and recover), `docs/EVIDENCE.md`.
- Record a 3 to 5 minute demo video following section 7. Tag a `v1.0` release.

**Day 4 checkpoint:** a stranger can clone, run `docker compose up`, open the dashboard, and read evidence that the system works.

## 5. What the decision records should cover

Write each in about 5 lines (context, decision, why, alternatives, consequence):

1. Adapter layer isolates the simulator.
2. REST polling as the source of truth, with SSE optional (the guide says treat SSE as a hint).
3. EWMA forecast first, with a backtest, rather than a heavy ML model.
4. Greedy constrained planner plus Monte Carlo evaluation, rather than RL.
5. Human approval before any allocation.
6. SQLite for the audit log, chosen for simplicity.
7. Streamlit as a thin client.
8. Explicit degraded modes.

## 6. Resilience matrix (use it in the README)

| Failure | Detection | System behavior | Recovery |
| --- | --- | --- | --- |
| Simulator 503 or high error rate | Client errors, breaker opens | Serve cached state, banner `DEGRADED_CACHED`, block new approvals | Half-open probes, auto-resume |
| Latency fault | Timeouts, p95 metric | Timeouts and retries, no UI freeze | Auto |
| Stale data header | `X-Simulator-Stale` | Mark data stale, lower confidence, refetch | Header disappears |
| Malformed fields | Schema validation | Reject snapshot, keep last good, alert | Next valid poll |
| Forecaster error | Exception or timeout | Fallback rule, `DEGRADED_FALLBACK` | Auto when healthy |
| Low confidence | Confidence below threshold | Flag human review | Operator decides |
| Route disrupted | Route status or `ROUTE_DISRUPTED` | Re-plan via alternate route | Route returns |
| Stream drop | Stream error | Fall back to polling | Reconnect and refetch |
| Duplicate or retried POST | Idempotency key | Same allocation returned | n/a |

## 7. Demo script (3 to 5 minutes)

1. Normal operations on the dashboard, all green, status panel healthy.
2. Inject a `demand_spike`; risk rises; the detection explanation shows the signals.
3. Open the recommendation: constraints, expected impact (before and after probability), alternatives.
4. Approve; show the shipment in the history and in transit; step time.
5. Inject `route_disruption` plus `shipment_delay`; the plan changes; explain the unavoidable-shortage message if one appears.
6. Inject a simulator fault; the banner changes to degraded; the system keeps serving.
7. Clear the fault; recovery. Show the `/metrics` or status page and the evaluation table.

Rehearse it twice, and make it repeatable with `/admin/reset`.

## 8. Cut lines and risks

**If you fall behind, cut in this order:** Grafana, Level 2 forecast, LP version of the planner, alternatives list, SSE, extra scenarios in the evaluation. **Never cut:** the end-to-end loop, the adapter with error handling, fault drills, the twin numbers, the evaluation table, tests plus CI, and the honest README.

**Risks**

- Behaviors the guide leaves undefined (failed-allocation refunds, in-transit capacity). Test them on Day 1.
- Time sink: UI polish. Cap it.
- Scope creep: no Kubernetes, no RL, no microservices. State this as a deliberate decision in `DECISIONS.md`.
- The brief is a draft for organizer review, so confirm the final requirements and any Q&A with the organizers.

## 9. Learning checklist (tick as you go)

- HTTP, status codes, idempotency, retries with backoff and jitter
- pydantic models and validation; FastAPI; asyncio background tasks
- Circuit breaker and graceful degradation
- Time-series smoothing, forecast error metrics, anomaly detection
- Constrained allocation and greedy algorithms
- Monte Carlo simulation with numpy
- Docker, compose, healthchecks
- Prometheus metrics, structured logging, percentiles
- Locust load testing
- pytest, mocking HTTP, GitHub Actions
- Writing design docs and decision records

Keep `NOTES.md` as a short log of what you learned and what surprised you each day. It makes the project clearly yours and gives you material for interviews.
