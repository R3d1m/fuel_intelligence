"""Simulator HTTP Client Adapter.

Encapsulates all external network communication with the BUP Fuel Supply Simulator.
Implements:
1. Unified GET and POST interfaces with strict per-request timeouts.
2. Exponential backoff retry loop with jitter on 503 and network connection errors.
3. Transparent X-Simulator-Stale header detection returning SimulatorResponse[T].
4. Strict conversion of raw simulator payloads into validated domain models.
"""

import asyncio
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import httpx

from app.adapters.errors import (
    SimulatorConnectionError,
    SimulatorTimeoutError,
    SimulatorUnavailableError,
    parse_simulator_error,
)
from app.config import Settings, get_settings
from app.domain.models import (
    Allocation,
    AllocationCreate,
    DemandHistoryRecord,
    Depot,
    FaultType,
    InstanceInfo,
    Region,
    Route,
    SimEvent,
    SimulatorHealth,
    SimulatorMetrics,
    Station,
    SupplyArrival,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient network errors eligible for retry
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)


@dataclass(frozen=True)
class SimulatorResponse(Generic[T]):
    """Response wrapper containing payload data, staleness status, and metadata."""

    data: T
    is_stale: bool
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)


class SimulatorClient:
    """Asynchronous HTTP client adapter for the BUP Fuel Supply Simulator."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_factor: float | None = None,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.base_url = (base_url or cfg.simulator_base_url).rstrip("/")
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else cfg.simulator_timeout_seconds
        )
        self.max_retries = max_retries if max_retries is not None else cfg.simulator_max_retries
        self.backoff_factor = (
            backoff_factor if backoff_factor is not None else cfg.simulator_backoff_factor
        )

        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

    async def __aenter__(self) -> "SimulatorClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close underlying HTTP client session if internally created."""
        if not self._external_client:
            await self._client.aclose()

    def _is_stale(self, headers: Mapping[str, str]) -> bool:
        """Inspect headers for X-Simulator-Stale indicator."""
        stale_hdr = headers.get("x-simulator-stale", "")
        return stale_hdr.strip().lower() == "true"

    def _compute_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff duration with randomized jitter."""
        base_delay = self.backoff_factor * (2**attempt)
        jitter = random.uniform(0.0, 0.1 * (2**attempt))
        return base_delay + jitter

    async def _execute_with_retry(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> tuple[int, Any, dict[str, str]]:
        """Core request dispatch engine with backoff/jitter retry loop."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        timeout = kwargs.pop("timeout", self.timeout_seconds)

        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    timeout=timeout,
                    **kwargs,
                )

                # Check if simulator returned 503 Service Unavailable (e.g. injected fault)
                if response.status_code == 503:
                    if attempt < self.max_retries:
                        delay = self._compute_backoff(attempt)
                        logger.warning(
                            "Simulator returned 503 on %s %s (attempt %d/%d). Retrying in %.2fs...",
                            method,
                            path,
                            attempt + 1,
                            self.max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # Retries exhausted on 503
                    payload = self._parse_json_or_text(response)
                    raise parse_simulator_error(
                        status_code=503,
                        payload=payload,
                        raw_text=response.text,
                    )

                # Handle other 4xx/5xx status codes immediately (do not retry domain errors)
                if response.is_error:
                    payload = self._parse_json_or_text(response)
                    raise parse_simulator_error(
                        status_code=response.status_code,
                        payload=payload,
                        raw_text=response.text,
                    )

                # Successful HTTP response (2xx)
                payload = self._parse_json_or_text(response)
                return response.status_code, payload, dict(response.headers)

            except RETRYABLE_EXCEPTIONS as exc:
                last_exception = exc
                if attempt < self.max_retries:
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        "Simulator network error (%s) on %s %s (attempt %d/%d). "
                        "Retrying in %.2fs...",
                        type(exc).__name__,
                        method,
                        path,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if isinstance(exc, httpx.TimeoutException):
                    raise SimulatorTimeoutError(
                        f"Simulator timed out on {method} {path} "
                        f"after {self.max_retries} retries: {exc}"
                    ) from exc
                raise SimulatorConnectionError(
                    f"Failed to connect to simulator on {method} {path} "
                    f"after {self.max_retries} retries: {exc}"
                ) from exc

        # Safety catch (unreachable in standard flow)
        if last_exception:
            raise SimulatorConnectionError(f"Request failed: {last_exception}") from last_exception
        raise SimulatorUnavailableError("Simulator request failed with unknown error")

    @staticmethod
    def _parse_json_or_text(response: httpx.Response) -> Any:
        """Safely parse response body as JSON, falling back to raw text."""
        try:
            return response.json()
        except Exception:
            return response.text

    # -------------------------------------------------------------------------
    # Generic HTTP Primitive Operations
    # -------------------------------------------------------------------------

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SimulatorResponse[Any]:
        """Execute HTTP GET with configured timeouts, retries, and stale detection."""
        status_code, payload, headers = await self._execute_with_retry(
            "GET",
            path,
            params=params,
            **kwargs,
        )
        return SimulatorResponse(
            data=payload,
            is_stale=self._is_stale(headers),
            status_code=status_code,
            headers=headers,
        )

    async def post(
        self,
        path: str,
        json: Any = None,
        **kwargs: Any,
    ) -> SimulatorResponse[Any]:
        """Execute HTTP POST with configured timeouts, retries, and stale detection."""
        status_code, payload, headers = await self._execute_with_retry(
            "POST",
            path,
            json=json,
            **kwargs,
        )
        return SimulatorResponse(
            data=payload,
            is_stale=self._is_stale(headers),
            status_code=status_code,
            headers=headers,
        )

    # -------------------------------------------------------------------------
    # Typed Resource Operations (Domain Models)
    # -------------------------------------------------------------------------

    async def get_health(self) -> SimulatorResponse[SimulatorHealth]:
        """Fetch simulator health status."""
        resp = await self.get("/v1/health")
        return SimulatorResponse(
            data=SimulatorHealth.model_validate(resp.data),
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_instance(self) -> SimulatorResponse[InstanceInfo]:
        """Fetch simulator instance configuration and clock state."""
        resp = await self.get("/v1/instance")
        return SimulatorResponse(
            data=InstanceInfo.model_validate(resp.data),
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_regions(self) -> SimulatorResponse[list[Region]]:
        """Fetch list of geographic regions."""
        resp = await self.get("/v1/regions")
        regions = [Region.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=regions,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_depots(self) -> SimulatorResponse[list[Depot]]:
        """Fetch all fuel supply depots."""
        resp = await self.get("/v1/depots")
        depots = [Depot.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=depots,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_depot(self, depot_id: str) -> SimulatorResponse[Depot]:
        """Fetch a specific depot by ID."""
        resp = await self.get(f"/v1/depots/{depot_id}")
        return SimulatorResponse(
            data=Depot.model_validate(resp.data),
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_stations(self) -> SimulatorResponse[list[Station]]:
        """Fetch all retail fuel stations."""
        resp = await self.get("/v1/stations")
        stations = [Station.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=stations,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_station(self, station_id: str) -> SimulatorResponse[Station]:
        """Fetch a specific station by ID."""
        resp = await self.get(f"/v1/stations/{station_id}")
        return SimulatorResponse(
            data=Station.model_validate(resp.data),
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_routes(self) -> SimulatorResponse[list[Route]]:
        """Fetch all transit routes."""
        resp = await self.get("/v1/routes")
        routes = [Route.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=routes,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_supply_arrivals(self) -> SimulatorResponse[list[SupplyArrival]]:
        """Fetch scheduled/active upstream supply arrivals."""
        resp = await self.get("/v1/supply-arrivals")
        arrivals = [SupplyArrival.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=arrivals,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_events(self) -> SimulatorResponse[list[SimEvent]]:
        """Fetch active and scheduled simulation events."""
        resp = await self.get("/v1/events")
        events = [SimEvent.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=events,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_allocations(self) -> SimulatorResponse[list[Allocation]]:
        """Fetch all past and current allocations."""
        resp = await self.get("/v1/allocations")
        allocations = [Allocation.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=allocations,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_demand_history(
        self,
        station_id: str | None = None,
        limit: int = 200,
    ) -> SimulatorResponse[list[DemandHistoryRecord]]:
        """Fetch historical demand and consumption records."""
        params: dict[str, Any] = {"limit": limit}
        if station_id:
            params["station_id"] = station_id

        resp = await self.get("/v1/demand-history", params=params)
        records = [DemandHistoryRecord.model_validate(item) for item in resp.data]
        return SimulatorResponse(
            data=records,
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def get_metrics(self) -> SimulatorResponse[SimulatorMetrics]:
        """Fetch simulation metrics."""
        resp = await self.get("/v1/metrics")
        return SimulatorResponse(
            data=SimulatorMetrics.model_validate(resp.data),
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def create_allocation(
        self,
        allocation: AllocationCreate | dict[str, Any],
    ) -> SimulatorResponse[Allocation]:
        """Dispatch a new fuel allocation shipment."""
        payload = (
            allocation.model_dump(mode="json")
            if isinstance(allocation, AllocationCreate)
            else allocation
        )
        resp = await self.post("/v1/allocations", json=payload)
        return SimulatorResponse(
            data=Allocation.model_validate(resp.data),
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    async def cancel_allocation(self, allocation_id: int) -> SimulatorResponse[Allocation]:
        """Cancel a pending allocation before departure."""
        resp = await self.post(f"/v1/allocations/{allocation_id}/cancel")
        return SimulatorResponse(
            data=Allocation.model_validate(resp.data),
            is_stale=resp.is_stale,
            status_code=resp.status_code,
            headers=resp.headers,
        )

    # -------------------------------------------------------------------------
    # Admin & Simulation Control Methods (For Testing & Fault Drills)
    # -------------------------------------------------------------------------

    async def admin_step(self) -> SimulatorResponse[dict[str, Any]]:
        """Advance simulation clock by one tick."""
        return await self.post("/admin/step")

    async def admin_pause(self) -> SimulatorResponse[dict[str, Any]]:
        """Pause continuous simulation progression."""
        return await self.post("/admin/pause")

    async def admin_run(self) -> SimulatorResponse[dict[str, Any]]:
        """Resume continuous simulation clock."""
        return await self.post("/admin/run")

    async def admin_reset(self) -> SimulatorResponse[dict[str, Any]]:
        """Reset simulation back to initial tick 0 state."""
        return await self.post("/admin/reset")

    async def admin_inject_fault(
        self,
        fault_type: str | FaultType,
        duration_seconds: int,
        parameters: dict[str, Any] | None = None,
    ) -> SimulatorResponse[dict[str, Any]]:
        """Inject a chaos reliability fault into the simulator API."""
        f_type = fault_type.value if isinstance(fault_type, FaultType) else fault_type
        payload: dict[str, Any] = {
            "type": f_type,
            "duration_seconds": duration_seconds,
            "parameters": parameters or {},
        }
        return await self.post("/admin/faults", json=payload)

    async def admin_clear_faults(self) -> SimulatorResponse[dict[str, Any]]:
        """Clear all active simulator faults immediately."""
        return await self.post("/admin/faults/clear")
