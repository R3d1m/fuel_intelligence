# Operations Runbook: Fuel Intelligence Platform

A quick operational guide for running, monitoring, and troubleshooting the Fuel Intelligence Platform during testing and judging.

## 1. Quick Start

### Starting the Simulator & Backend Stack
```bash
# Start simulator
docker compose up -d

# Start backend locally (development)
uv run uvicorn backend.app.api.main:app --host 0.0.0.0 --port 8001 --reload

# Start dashboard
uv run streamlit run dashboard/app.py --server.port 8501
```

### Health Check Verification
```bash
curl -s http://localhost:8000/v1/health  # Simulator health
curl -s http://localhost:8001/health     # Platform backend health
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
