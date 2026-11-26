"""Synchronous HTTP client for the Fuel Intelligence FastAPI backend.

Acts as a resilient thin boundary between the Streamlit dashboard and the backend API,
gracefully handling network interruptions, degraded states, and error payloads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApiResponse:
    """Wrapper holding API result, connection status, and error diagnostics."""

    success: bool
    data: dict[str, Any] | None = None
    status_code: int | None = None
    error: str | None = None
    is_connected: bool = True


class BackendApiClient:
    """Resilient HTTP client for interacting with the Fuel Intelligence backend API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8001",
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str, params: dict[str, Any] | None = None) -> ApiResponse:
        """Perform a synchronous GET request with comprehensive error handling."""
        url = f"{self.base_url}{path}"
        timeout = httpx.Timeout(self.timeout_seconds, connect=1.5)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url, params=params)
                if response.is_success:
                    return ApiResponse(
                        success=True,
                        data=response.json(),
                        status_code=response.status_code,
                    )
                # Handle structured backend errors (e.g. 503 during degraded/empty store)
                try:
                    error_payload = response.json()
                    detail = error_payload.get("detail", response.text)
                    if isinstance(detail, dict):
                        err_msg = detail.get("message", str(detail))
                    else:
                        err_msg = str(detail)
                except Exception:
                    err_msg = response.text or f"HTTP {response.status_code}"

                return ApiResponse(
                    success=False,
                    data=None,
                    status_code=response.status_code,
                    error=err_msg,
                )
        except httpx.ConnectError:
            return ApiResponse(
                success=False,
                error=f"Cannot connect to backend at {self.base_url}. Is the service running?",
                is_connected=False,
            )
        except httpx.TimeoutException:
            return ApiResponse(
                success=False,
                error=f"Request to {path} timed out after {self.timeout_seconds:.1f}s.",
                is_connected=False,
            )
        except Exception as exc:
            return ApiResponse(
                success=False,
                error=f"Unexpected error communicating with backend: {exc}",
                is_connected=False,
            )

    def _post(self, path: str, json_data: dict[str, Any] | None = None) -> ApiResponse:
        """Perform a synchronous POST request with comprehensive error handling."""
        url = f"{self.base_url}{path}"
        timeout = httpx.Timeout(self.timeout_seconds, connect=1.5)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=json_data)
                if response.is_success:
                    return ApiResponse(
                        success=True,
                        data=response.json(),
                        status_code=response.status_code,
                    )
                try:
                    error_payload = response.json()
                    detail = error_payload.get("detail", response.text)
                    if isinstance(detail, dict):
                        err_msg = detail.get("message", str(detail))
                    else:
                        err_msg = str(detail)
                except Exception:
                    err_msg = response.text or f"HTTP {response.status_code}"

                return ApiResponse(
                    success=False,
                    data=None,
                    status_code=response.status_code,
                    error=err_msg,
                )
        except httpx.ConnectError:
            return ApiResponse(
                success=False,
                error=f"Cannot connect to backend at {self.base_url}.",
                is_connected=False,
            )
        except httpx.TimeoutException:
            return ApiResponse(
                success=False,
                error=f"Request to {path} timed out after {self.timeout_seconds:.1f}s.",
                is_connected=False,
            )
        except Exception as exc:
            return ApiResponse(
                success=False,
                error=f"Unexpected error communicating with backend: {exc}",
                is_connected=False,
            )

    def get_health(self) -> ApiResponse:
        """Fetch platform liveness, circuit state, and poller telemetry."""
        return self._get("/health")

    def get_state(self) -> ApiResponse:
        """Fetch authoritative network snapshot and operational mode."""
        return self._get("/state")

    def get_forecasts(self) -> ApiResponse:
        """Fetch fleet-wide demand forecasts and backtest error metrics."""
        return self._get("/forecasts")

    def get_risks(self) -> ApiResponse:
        """Fetch fleet-wide inventory risk projections and urgency alerts."""
        return self._get("/risks")

    def trigger_sync(self) -> ApiResponse:
        """Trigger an immediate manual synchronization poll cycle."""
        return self._post("/sync")

    def get_recommendations(
        self,
        status: str | None = None,
        station_id: str | None = None,
        limit: int | None = None,
    ) -> ApiResponse:
        """Fetch actionable recommendations from backend."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if station_id:
            params["station_id"] = station_id
        if limit is not None:
            params["limit"] = limit
        return self._get("/recommendations", params=params or None)

    def generate_recommendations(self) -> ApiResponse:
        """Trigger recommendation generation on the latest snapshot."""
        return self._post("/recommendations/generate")

    def approve_recommendation(
        self,
        recommendation_id: str,
        operator_notes: str | None = None,
    ) -> ApiResponse:
        """Approve and dispatch a recommendation to the simulator."""
        payload = {"operator_notes": operator_notes} if operator_notes else {}
        return self._post(f"/recommendations/{recommendation_id}/approve", json_data=payload)

    def reject_recommendation(
        self,
        recommendation_id: str,
        operator_notes: str | None = None,
    ) -> ApiResponse:
        """Reject a proposed recommendation with operator rationale."""
        payload = {"operator_notes": operator_notes} if operator_notes else {}
        return self._post(f"/recommendations/{recommendation_id}/reject", json_data=payload)

    def get_audit_history(
        self,
        recommendation_id: str | None = None,
        limit: int = 50,
    ) -> ApiResponse:
        """Fetch audit log history of operator decisions and transitions."""
        params: dict[str, Any] = {"limit": limit}
        if recommendation_id:
            params["recommendation_id"] = recommendation_id
        return self._get("/decisions/audit", params=params)

    def get_audit_stats(self) -> ApiResponse:
        """Fetch consolidated decision statistics from the SQLite audit ledger."""
        return self._get("/decisions/stats")

    def get_metrics(self) -> ApiResponse:
        """Fetch Prometheus /metrics exposition text and parse key counters/gauges."""
        try:
            url = f"{self.base_url}/metrics"
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(url)
                if response.is_success:
                    parsed = _parse_prometheus_text(response.text)
                    return ApiResponse(
                        success=True,
                        data=parsed,
                        status_code=response.status_code,
                        is_connected=True,
                    )
                return ApiResponse(
                    success=False,
                    error=f"HTTP {response.status_code}",
                    status_code=response.status_code,
                    is_connected=True,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return ApiResponse(
                success=False,
                error=f"Backend unreachable: {exc}",
                is_connected=False,
            )
        except Exception as exc:
            return ApiResponse(
                success=False,
                error=f"Unexpected error reading /metrics: {exc}",
                is_connected=False,
            )


def _parse_prometheus_text(text: str) -> dict[str, float]:
    """Parse Prometheus exposition text into summary totals for the dashboard."""
    summary: dict[str, float] = {
        "http_requests_total": 0.0,
        "simulator_errors_total": 0.0,
        "fallback_activations_total": 0.0,
        "forecast_mae_liters": 0.0,
        "recommendations_submitted": 0.0,
        "recommendations_failed": 0.0,
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        metric_part, val_str = parts
        try:
            val = float(val_str)
        except ValueError:
            continue

        if metric_part.startswith("fuel_http_requests_total"):
            summary["http_requests_total"] += val
        elif metric_part.startswith("fuel_simulator_errors_total"):
            summary["simulator_errors_total"] += val
        elif metric_part.startswith("fuel_fallback_activations_total"):
            summary["fallback_activations_total"] += val
        elif metric_part.startswith("fuel_forecast_mae_liters"):
            summary["forecast_mae_liters"] = val
        elif metric_part.startswith("fuel_recommendations_total"):
            if 'outcome="SUBMITTED"' in metric_part:
                summary["recommendations_submitted"] += val
            elif 'outcome="FAILED"' in metric_part:
                summary["recommendations_failed"] += val
    return summary
