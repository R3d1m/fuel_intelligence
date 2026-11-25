"""Tests for Prometheus metrics exposition, structured JSON logging, and dashboard parsing."""

import json
import logging
from datetime import UTC, datetime

from app.api.main import create_app
from app.config import Settings
from app.domain.models import OperationalMode
from app.ingest.store import StateStore
from app.observability.logging import JsonLogFormatter, log_event
from app.observability.metrics import (
    normalize_simulator_path,
    record_alert,
    record_fallback_activation,
    record_recommendation_outcome,
    record_simulator_call,
    record_simulator_error,
    set_forecast_accuracy,
)
from app.resilience.breaker import CircuitBreaker, CircuitState
from app.resilience.mode import ResilienceManager, diagnose_operational_mode
from fastapi.testclient import TestClient

from dashboard.client import _parse_prometheus_text


def test_metrics_endpoint_exposes_prometheus_metrics() -> None:
    """GET /metrics should return 200 with Prometheus text format and tracked metrics."""
    app = create_app(settings=Settings(app_env="testing"), start_poller=False)
    client = TestClient(app)

    # Trigger at least one API call and helper updates
    health_resp = client.get("/health")
    assert health_resp.status_code == 200

    record_simulator_call("GET", "/v1/stations/ST-1", "success", 0.042)
    record_simulator_error("http_503")
    record_alert("CRITICAL")
    record_recommendation_outcome("SUBMITTED")
    record_fallback_activation("forecaster failed")
    set_forecast_accuracy(123.4, 4.5)

    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    assert "text/plain" in metrics_resp.headers["content-type"]

    body = metrics_resp.text
    for expected_metric in (
        "fuel_http_requests_total",
        "fuel_http_request_duration_seconds",
        "fuel_simulator_requests_total",
        "fuel_simulator_request_duration_seconds",
        "fuel_simulator_errors_total",
        "fuel_circuit_breaker_state",
        "fuel_operational_mode",
        "fuel_alerts_raised_total",
        "fuel_recommendations_total",
        "fuel_fallback_activations_total",
        "fuel_forecast_mae_liters",
        "fuel_forecast_mape_percent",
    ):
        assert expected_metric in body


def test_normalize_simulator_path_collapses_ids() -> None:
    """Dynamic IDs in simulator URLs should be normalized to avoid high cardinality."""
    assert normalize_simulator_path("/v1/stations/ST-01?limit=10") == "/v1/stations/{id}"
    assert normalize_simulator_path("/v1/allocations/42/cancel") == "/v1/allocations/{id}/cancel"
    assert normalize_simulator_path("/v1/depots") == "/v1/depots"


def test_json_log_formatter_and_log_event(caplog: object) -> None:
    """JsonLogFormatter should serialize structured event name and context as valid JSON."""
    formatter = JsonLogFormatter()
    test_logger = logging.getLogger("test.observability")

    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = CaptureHandler()
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    try:
        log_event(
            test_logger,
            event="decision.approved",
            message="Recommendation rec-1 approved",
            recommendation_id="rec-1",
            quantity_liters=5000.0,
        )
    finally:
        test_logger.removeHandler(handler)

    assert len(records) == 1
    formatted = formatter.format(records[0])
    parsed = json.loads(formatted)
    assert parsed["event"] == "decision.approved"
    assert parsed["message"] == "Recommendation rec-1 approved"
    assert parsed["level"] == "INFO"
    assert parsed["context"]["recommendation_id"] == "rec-1"
    assert parsed["context"]["quantity_liters"] == 5000.0


def test_breaker_emits_structured_open_and_closed_events() -> None:
    """CircuitBreaker transitions should emit simulator.breaker_open / closed events."""
    breaker_logger = logging.getLogger("app.resilience.breaker")
    captured_events: list[str] = []

    class EventCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            evt = getattr(record, "event", None)
            if evt:
                captured_events.append(evt)

    handler = EventCapture()
    breaker_logger.addHandler(handler)
    try:
        now = 100.0
        breaker = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=5.0,
            name="test-obs",
            clock=lambda: now,
        )
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        assert "simulator.breaker_open" in captured_events

        now = 106.0
        assert breaker.state == CircuitState.HALF_OPEN
        assert "simulator.breaker_half_open" in captured_events

        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED
        assert "simulator.breaker_closed" in captured_events
    finally:
        breaker_logger.removeHandler(handler)


def test_mode_changed_event_emitted_on_transition() -> None:
    """ResilienceManager should emit mode.changed when operational mode transitions."""
    mode_logger = logging.getLogger("app.resilience.mode")
    captured: list[tuple[str, dict[str, object]]] = []

    class EventCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            evt = getattr(record, "event", None)
            ctx = getattr(record, "context", {})
            if evt:
                captured.append((evt, ctx))

    handler = EventCapture()
    mode_logger.addHandler(handler)
    try:
        manager = ResilienceManager()
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, name="mode-obs")
        store = StateStore()

        # First call sets baseline (DEGRADED_CACHED because store has no snapshot)
        diag1 = diagnose_operational_mode(breaker=breaker, store=store, manager=manager)
        assert diag1.mode == OperationalMode.DEGRADED_CACHED

        # Trigger forecaster failure -> transition to DEGRADED_FALLBACK
        manager.record_forecaster_failure("timeout")
        diag2 = diagnose_operational_mode(breaker=breaker, store=store, manager=manager)
        assert diag2.mode == OperationalMode.DEGRADED_FALLBACK

        assert any(
            evt == "mode.changed"
            and ctx.get("from_mode") == "DEGRADED_CACHED"
            and ctx.get("to_mode") == "DEGRADED_FALLBACK"
            for evt, ctx in captured
        )
    finally:
        mode_logger.removeHandler(handler)


def test_parse_prometheus_text_extracts_dashboard_summary() -> None:
    """Dashboard Prometheus text parser should aggregate counters and gauges."""
    sample = "\n".join(
        [
            "# HELP fuel_http_requests_total Total HTTP requests",
            "# TYPE fuel_http_requests_total counter",
            'fuel_http_requests_total{endpoint="/health",method="GET",status_code="200"} 12.0',
            'fuel_http_requests_total{endpoint="/state",method="GET",status_code="200"} 8.0',
            'fuel_simulator_errors_total{error_type="http_503"} 3.0',
            'fuel_fallback_activations_total{reason="forecaster_error"} 2.0',
            "fuel_forecast_mae_liters 42.5",
            'fuel_recommendations_total{outcome="SUBMITTED"} 5.0',
            'fuel_recommendations_total{outcome="FAILED"} 1.0',
        ]
    )
    parsed = _parse_prometheus_text(sample)
    assert parsed["http_requests_total"] == 20.0
    assert parsed["simulator_errors_total"] == 3.0
    assert parsed["fallback_activations_total"] == 2.0
    assert parsed["forecast_mae_liters"] == 42.5
    assert parsed["recommendations_submitted"] == 5.0
    assert parsed["recommendations_failed"] == 1.0
    assert datetime.now(UTC) is not None
