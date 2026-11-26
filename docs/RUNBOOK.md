# Operations Runbook: Fuel Intelligence Platform

A quick operational guide for running, monitoring, and troubleshooting the Fuel Intelligence Platform during testing and judging.

## 1. Quick Start

### Option A: Full Stack via Docker Compose
```bash
# Build and start simulator (8000), backend (8001), dashboard (8501), and React console (3000)
docker compose up -d --build
docker compose ps
```

### Option B: Local Development
```bash
# 1. Start simulator
docker compose up -d simulator

# 2. Start backend locally
uv run uvicorn backend.app.api.main:app --host 0.0.0.0 --port 8001 --reload

# 3. Start Streamlit dashboard (optional)
uv run streamlit run dashboard/app.py --server.port 8501

# 4. Start React operator console (optional)
cd frontend && npm run dev
```

### Health Check & Test Verification
```bash
curl -s http://localhost:8000/v1/health  # Simulator health
curl -s http://localhost:8001/health     # Platform backend health
curl -s http://localhost:8001/metrics    # Prometheus metrics

# Run linter and unit/integration tests
uv run ruff check .
uv run pytest
```

### Verification & Benchmarking Tools
```bash
# Run live fault drills (requires simulator + backend running)
uv run python tools/run_fault_drills.py

# Run A/B policy evaluation harness
uv run python tools/evaluate.py

# Run Locust load test against backend
uv run python tools/loadtest/run_loadtest.py
```

---

## 2. Operational Modes & Indicator

| Mode | Meaning | Recommended Action |
|---|---|---|
| `NORMAL` | All services green, fresh data flowing. | Regular monitoring. |
| `DEGRADED_CACHED` | Simulator API down or stale. Serving cached snapshot. Dispatches blocked. | Check simulator container logs (`docker logs fuel-simulator`). Clear faults if testing. |
| `DEGRADED_FALLBACK` | Intelligence forecaster/twin failed. Heuristic planner active with human review badge. | Check backend logs for model errors or NaN values. |

---

## 3. Simulator Admin Controls & Testing Shortcuts

- **Web Admin UI:** `http://localhost:8000/admin`
- **Pause/Resume:**
  ```bash
  curl -X POST http://localhost:8000/admin/pause
  curl -X POST http://localhost:8000/admin/run
  ```
- **Single Deterministic Tick Step:**
  ```bash
  curl -X POST http://localhost:8000/admin/step
  ```
- **Hard Reset (Wipe & Reload Scenario):**
  ```bash
  curl -X POST http://localhost:8000/admin/reset
  ```
- **Clear All Faults:**
  ```bash
  curl -X POST http://localhost:8000/admin/faults/clear
  ```
