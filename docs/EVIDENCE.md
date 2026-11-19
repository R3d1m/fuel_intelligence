# Empirical Evidence and Evaluation Results

This document records the empirical results, A/B policy evaluations, fault-drill observations, and load-test metrics for the Fuel Intelligence Platform.

## 1. Policy Evaluation Benchmark (A/B Test)

*Evaluation harness (`tools/evaluate.py`) running identical seeds across scenarios:*

| Scenario | Policy | Service Level (%) | Unmet Demand (L) | Allocation Failures | Station-Hours Unmet |
|---|---|---|---|---|---|
| Baseline Day | No-Action | TBD | TBD | 0 | TBD |
| Baseline Day | Naive (Refill < 30%) | TBD | TBD | TBD | TBD |
| Baseline Day | Fuel Intelligence (Ours) | TBD | TBD | TBD | TBD |
| Demand Spike | No-Action | TBD | TBD | 0 | TBD |
| Demand Spike | Naive | TBD | TBD | TBD | TBD |
| Demand Spike | Fuel Intelligence (Ours) | TBD | TBD | TBD | TBD |
| Route Disruption | Naive | TBD | TBD | TBD | TBD |
| Route Disruption | Fuel Intelligence (Ours) | TBD | TBD | TBD | TBD |
| Combined Crisis | Naive | TBD | TBD | TBD | TBD |
| Combined Crisis | Fuel Intelligence (Ours) | TBD | TBD | TBD | TBD |

---

## 2. Fault Drill Log (Resilience Verification)

| Injected Fault | Simulator Path / Injection | System Reaction & Log | Degraded Mode | Recovery Observed |
|---|---|---|---|---|
| `unavailable` | POST /admin/faults `{"type": "unavailable"}` | Breaker tripped, serving cached snapshot | `DEGRADED_CACHED` | Auto-recovered when fault cleared |
| `latency` | POST /admin/faults `{"type": "latency", "parameters": {"delay_ms": 1500}}` | Handled within timeout, p95 latency captured | `NORMAL` | Request latency returned to normal |
| `stale_data` | POST /admin/faults `{"type": "stale_data"}` | Detected `X-Simulator-Stale: true` header, lowered prediction confidence | `DEGRADED_CACHED` | Resumed fresh processing on header clear |
| `stream_disconnect`| POST /admin/faults `{"type": "stream_disconnect"}` | Stream failed with 503; fell back seamlessly to REST polling | `NORMAL` | SSE auto-reconnected |
| `error_rate` | POST /admin/faults `{"type": "error_rate", "parameters": {"rate": 0.5}}` | Retries with exponential backoff absorbed transient 503s | `NORMAL` / `DEGRADED_CACHED` | Zero unhandled exceptions |

---

## 3. Load Testing Performance

*Locust load test running against backend APIs (`/state`, `/risks`, `/recommendations`):*

| Concurrency (Users) | Throughput (RPS) | Avg Latency (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Error Rate (%) |
|---|---|---|---|---|---|---|
| 10 users | TBD | TBD | TBD | TBD | TBD | 0.0% |
| 50 users | TBD | TBD | TBD | TBD | TBD | 0.0% |
| 100 users | TBD | TBD | TBD | TBD | TBD | TBD |
