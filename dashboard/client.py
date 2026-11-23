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

    def _get(self, path: str) -> ApiResponse:
        """Perform a synchronous GET request with comprehensive error handling."""
        url = f"{self.base_url}{path}"
        timeout = httpx.Timeout(self.timeout_seconds, connect=1.5)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url)
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
