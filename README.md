# Fuel Intelligence & Resilience Platform

[![CI](https://github.com/redwan/fuel_intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/redwan/fuel_intelligence/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

> An intelligent, fault-tolerant decision-support platform built on top of the **BUP Fuel Supply Simulator** for the BUP CSE FEST 2026 Hackathon Finals.

---

## 1. Executive Summary

During fuel supply disruptions, operational centers must balance critical regional demand, transport constraints, depot capacity, and unexpected network crises (route outages, sudden demand spikes, delayed shipments, and upstream API failures). 

This platform observes simulated fuel operations in Bangladesh, forecasts stockouts with uncertainty intervals, runs a local Monte Carlo digital twin to simulate intervention impacts, and empowers operators to review and approve optimal allocation decisions.

### Architectural Core Principles
- **Simulator as an External Untrusted Boundary:** All communication with the simulator is isolated inside an adapter layer with retries, exponential backoff, jitter, and a circuit breaker.
- **Explicit Degraded Operational Modes:** The platform explicitly transitions between `NORMAL`, `DEGRADED_CACHED` (simulator down or stale), and `DEGRADED_FALLBACK` (model unavailable, running safe heuristics).
- **Human-in-the-Loop:** Consequential dispatch actions require operator review and approval, maintaining safety guardrails while presenting transparent impact estimates (e.g., *Stockout risk reduced 72% → 19%*).
- **Complete Auditability:** Every decision, input snapshot, recommendation, and execution result is tracked in an SQLite audit ledger.

---

## 2. System Architecture

```
BUP Simulator (HTTP, SSE)
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
   [api]           FastAPI: /health, /state, /risks, /recommendations, /decisions, /metrics
        │
   [dashboard]     Streamlit (thin client of the API)
   [ops]           Docker, Compose, Prometheus metrics, GitHub Actions CI
```

---

## 3. Quick Start (60 Seconds)

### Prerequisites
- Docker & Docker Compose
- Python 3.11+ (or `uv`)

### 1. Start the BUP Fuel Supply Simulator
```bash
docker compose up -d
curl -s http://localhost:8000/v1/health
```

### 2. Set Up the Platform Environment
```bash
# Clone the repository
git clone https://github.com/your-username/fuel_intelligence.git
cd fuel_intelligence

# Create virtual environment and install dependencies using uv
uv sync --all-extras
# or using standard venv:
# python -m venv .venv
# .venv/Scripts/pip install -e ".[dev]"
```

### 3. Run Verification Tests & Linting
```bash
# Run linter
uv run ruff check .

# Run test suite
uv run pytest
```

---

## 4. Operational Modes & Resilience Matrix

| Failure Condition | Detection Mechanism | Platform Response | Recovery Path |
|---|---|---|---|
| Simulator 503 / Outage | Consecutive failed HTTP calls trip breaker | Serve cached state (`DEGRADED_CACHED`), block automated dispatches | Half-open test probes; auto-resume on recovery |
| Latency Fault | Configured client timeouts | Timeout enforcement; no UI freeze; p95 latency tracked | Auto-clears when latency subsides |
| Stale Data Signal | `X-Simulator-Stale: true` header | Mark snapshot stale, lower forecast confidence | Refresh cache when header clears |
| Schema / Malformed Fields | Pydantic validation failure | Discard corrupt payload, retain last known good snapshot, raise alert | Next valid polling cycle |
| Forecaster Failure | Model execution exception | Fallback heuristic rule (`DEGRADED_FALLBACK`), flag human review | Auto-recovers on next successful forecast |

---

## 5. Repository Layout

```
fuel_intelligence/
  backend/app/
    config.py                  # 12-factor configuration using pydantic-settings
    adapters/simulator_client.py, errors.py
    domain/models.py           # Strict typed Pydantic models & enums
    ingest/poller.py, store.py # Background poll loop & snapshot state store
    intelligence/              # Forecast, risk engine, greedy planner, Monte Carlo twin
    decisions/service.py, audit.py
    resilience/breaker.py, mode.py
    observability/metrics.py, logging.py
    api/routes.py, main.py     # FastAPI application entrypoint
  backend/tests/               # Unit, integration, and fault-injection tests
  dashboard/app.py             # Streamlit operator dashboard
  tools/evaluate.py            # A/B policy evaluation harness
  tools/loadtest/locustfile.py # Locust performance & load testing suite
  docs/                        # Architecture, ADRs, Evidence, Runbook, Simulator Notes
  .github/workflows/ci.yml     # Automated CI pipeline
  docker-compose.yml           # Unified multi-service deployment
  README.md
  NOTES.md                     # Engineering development log
```

---

## 6. Disclaimer
This system operates solely on simulated data provided by the BUP Fuel Supply Simulator for academic and hackathon evaluation purposes. It does not interface with or control real-world physical fuel infrastructure.
