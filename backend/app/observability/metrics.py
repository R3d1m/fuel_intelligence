"""Prometheus metrics registry and instrumentation helpers.

Exposes platform-wide observability counters, gauges, and histograms for:
- FastAPI HTTP request throughput and latency per endpoint
- Upstream simulator call latency, outcomes, and errors
- Circuit breaker state and active operational mode
- Risk alerts raised, recommendation outcomes, and fallback activations
- Demand forecast backtest accuracy (MAE and MAPE)
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ---------------------------------------------------------------------------
# HTTP API Metrics
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "fuel_http_requests_total",
    "Total HTTP requests handled by the Fuel Intelligence API.",
    labelnames=("method", "endpoint", "status_code"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "fuel_http_request_duration_seconds",
    "HTTP request latency in seconds per endpoint.",
    labelnames=("method", "endpoint"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ---------------------------------------------------------------------------
# Upstream Simulator Metrics
# ---------------------------------------------------------------------------

SIMULATOR_REQUESTS_TOTAL = Counter(
    "fuel_simulator_requests_total",
    "Total HTTP requests dispatched to the upstream fuel simulator.",
    labelnames=("method", "path", "outcome"),
)

SIMULATOR_REQUEST_DURATION_SECONDS = Histogram(
    "fuel_simulator_request_duration_seconds",
    "Upstream simulator HTTP request latency in seconds.",
    labelnames=("method", "path"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

SIMULATOR_ERRORS_TOTAL = Counter(
    "fuel_simulator_errors_total",
    "Total upstream simulator errors encountered by error category.",
    labelnames=("error_type",),
)

# ---------------------------------------------------------------------------
# Resilience & Operational Mode Metrics
# ---------------------------------------------------------------------------

_BREAKER_STATE_MAP: dict[str, int] = {
    "CLOSED": 0,
    "HALF_OPEN": 1,
    "OPEN": 2,
}

CIRCUIT_BREAKER_STATE = Gauge(
    "fuel_circuit_breaker_state",
    "Current circuit breaker state (0=CLOSED, 1=HALF_OPEN, 2=OPEN).",
    labelnames=("name",),
)

_OPERATIONAL_MODE_MAP: dict[str, int] = {
    "NORMAL": 0,
    "DEGRADED_CACHED": 1,
    "DEGRADED_FALLBACK": 2,
}

OPERATIONAL_MODE = Gauge(
    "fuel_operational_mode",
    "Current platform operational mode (0=NORMAL, 1=DEGRADED_CACHED, 2=DEGRADED_FALLBACK).",
)

OPERATIONAL_MODE_ACTIVE = Gauge(
    "fuel_operational_mode_active",
    "One-hot indicator of the active operational mode.",
    labelnames=("mode",),
)

FALLBACK_ACTIVATIONS_TOTAL = Counter(
    "fuel_fallback_activations_total",
    "Total number of times the deterministic fallback planner was activated.",
    labelnames=("reason",),
)

# ---------------------------------------------------------------------------
# Intelligence & Decision Metrics
# ---------------------------------------------------------------------------

ALERTS_RAISED_TOTAL = Counter(
    "fuel_alerts_raised_total",
    "Total risk alerts raised by severity.",
    labelnames=("severity",),
)

RECOMMENDATIONS_TOTAL = Counter(
    "fuel_recommendations_total",
    "Total dispatch recommendations processed by lifecycle outcome.",
    labelnames=("outcome",),
)

FORECAST_MAE_LITERS = Gauge(
    "fuel_forecast_mae_liters",
    "Latest fleet-wide demand forecast Mean Absolute Error (liters per tick).",
)

FORECAST_MAPE_PERCENT = Gauge(
    "fuel_forecast_mape_percent",
    "Latest fleet-wide demand forecast Mean Absolute Percentage Error (%).",
)


# ---------------------------------------------------------------------------
# Instrumentation Helper Functions
# ---------------------------------------------------------------------------


def normalize_simulator_path(path: str) -> str:
    """Collapse dynamic IDs in simulator paths to prevent high-cardinality labels."""
    clean = path.split("?")[0].strip("/")
    parts = clean.split("/")
    normalized: list[str] = []
    for idx, part in enumerate(parts):
        if idx > 0 and parts[idx - 1] in {"stations", "depots", "allocations"} and part != "cancel":
            normalized.append("{id}")
        else:
            normalized.append(part)
    return "/" + "/".join(normalized) if normalized else "/"


def record_http_request(
    method: str,
    endpoint: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    """Record HTTP API request count and latency."""
    HTTP_REQUESTS_TOTAL.labels(
        method=method.upper(),
        endpoint=endpoint,
        status_code=str(status_code),
    ).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(
        method=method.upper(),
        endpoint=endpoint,
    ).observe(max(0.0, duration_seconds))


def record_simulator_call(
    method: str,
    path: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    """Record an upstream simulator call's outcome and latency."""
    norm_path = normalize_simulator_path(path)
    SIMULATOR_REQUESTS_TOTAL.labels(
        method=method.upper(),
        path=norm_path,
        outcome=outcome,
    ).inc()
    SIMULATOR_REQUEST_DURATION_SECONDS.labels(
        method=method.upper(),
        path=norm_path,
    ).observe(max(0.0, duration_seconds))


def record_simulator_error(error_type: str) -> None:
    """Increment upstream simulator error counter."""
    SIMULATOR_ERRORS_TOTAL.labels(error_type=error_type).inc()


def set_circuit_breaker_state(name: str, state: str) -> None:
    """Update the numeric circuit breaker state gauge."""
    value = _BREAKER_STATE_MAP.get(state.upper(), -1)
    CIRCUIT_BREAKER_STATE.labels(name=name).set(value)


def set_operational_mode(mode: str) -> None:
    """Update both numeric and one-hot operational mode gauges."""
    mode_upper = mode.upper()
    OPERATIONAL_MODE.set(_OPERATIONAL_MODE_MAP.get(mode_upper, -1))
    for known_mode in _OPERATIONAL_MODE_MAP:
        OPERATIONAL_MODE_ACTIVE.labels(mode=known_mode).set(
            1.0 if known_mode == mode_upper else 0.0
        )


def record_alert(severity: str) -> None:
    """Increment the alerts raised counter for a given severity."""
    ALERTS_RAISED_TOTAL.labels(severity=severity.upper()).inc()


def record_recommendation_outcome(outcome: str) -> None:
    """Increment the recommendation lifecycle counter for a given outcome."""
    RECOMMENDATIONS_TOTAL.labels(outcome=outcome.upper()).inc()


def record_fallback_activation(reason: str = "model_degraded") -> None:
    """Increment fallback activation counter with a bounded reason label."""
    short_reason = reason.strip().lower()[:64] or "unknown"
    if "force_fallback" in short_reason:
        label = "force_fallback"
    elif "forecaster" in short_reason or "risk" in short_reason:
        label = "forecaster_error"
    elif "twin" in short_reason:
        label = "twin_error"
    elif "timeout" in short_reason:
        label = "timeout"
    else:
        label = "model_degraded"
    FALLBACK_ACTIVATIONS_TOTAL.labels(reason=label).inc()


def set_forecast_accuracy(mae_liters: float, mape_percent: float) -> None:
    """Update fleet-wide forecast MAE and MAPE gauges."""
    FORECAST_MAE_LITERS.set(max(0.0, float(mae_liters)))
    FORECAST_MAPE_PERCENT.set(max(0.0, float(mape_percent)))


def render_prometheus_metrics() -> tuple[bytes, str]:
    """Serialize all registered Prometheus metrics into exposition format."""
    return generate_latest(), CONTENT_TYPE_LATEST
