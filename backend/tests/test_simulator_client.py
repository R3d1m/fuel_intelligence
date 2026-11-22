"""Unit tests for the SimulatorClient adapter using respx mock transports."""

import httpx
import pytest
import respx
from app.adapters.errors import (
    SimulatorDomainError,
    SimulatorTimeoutError,
    SimulatorUnavailableError,
    SimulatorValidationError,
)
from app.adapters.simulator_client import SimulatorClient
from app.config import Settings
from app.domain.models import FuelType


@pytest.fixture
def test_settings() -> Settings:
    """Return fast-failing test settings with zero backoff for quick unit test execution."""
    return Settings(
        simulator_base_url="http://mock-simulator:8000",
        simulator_timeout_seconds=1.0,
        simulator_max_retries=2,
        simulator_backoff_factor=0.01,
    )


@pytest.mark.asyncio
async def test_get_instance_success(test_settings: Settings):
    """Verify normal GET request and InstanceInfo parsing."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        respx_mock.get("/v1/instance").respond(
            200,
            json={
                "id": 1,
                "scenario_id": "baseline",
                "scenario_version": "1.0",
                "seed": 12345,
                "sim_time": "2026-01-01T00:00:00",
                "tick": 4,
                "tick_minutes": 15,
                "status": "PAUSED",
            },
        )

        async with SimulatorClient(settings=test_settings) as client:
            resp = await client.get_instance()
            assert resp.status_code == 200
            assert resp.data.tick == 4
            assert resp.data.tick_minutes == 15
            assert not resp.is_stale


@pytest.mark.asyncio
async def test_stale_data_header_detection(test_settings: Settings):
    """Verify X-Simulator-Stale header is parsed into is_stale=True."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        respx_mock.get("/v1/depots").respond(
            200,
            json=[],
            headers={"X-Simulator-Stale": "true"},
        )

        async with SimulatorClient(settings=test_settings) as client:
            resp = await client.get_depots()
            assert resp.status_code == 200
            assert resp.is_stale is True


@pytest.mark.asyncio
async def test_retry_on_503_succeeds(test_settings: Settings):
    """Verify client retries on transient 503 and returns success when recovery happens."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        # First attempt: 503 fault
        # Second attempt: 200 OK
        respx_mock.get("/v1/health").side_effect = [
            httpx.Response(
                503,
                json={"error": {"code": "FAULT_INJECTED", "message": "API unavailable"}},
            ),
            httpx.Response(
                200,
                json={
                    "status": "ok",
                    "database": "ok",
                    "simulation": {"status": "PAUSED", "tick": 0},
                },
            ),
        ]

        async with SimulatorClient(settings=test_settings) as client:
            resp = await client.get_health()
            assert resp.status_code == 200
            assert resp.data.status == "ok"
            assert respx_mock.calls.call_count == 2


@pytest.mark.asyncio
async def test_retry_on_503_exhausted(test_settings: Settings):
    """Verify client raises SimulatorUnavailableError when 503 persists beyond max_retries."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        # All attempts return 503: 1 initial + 2 retries = 3 total calls
        respx_mock.get("/v1/health").respond(
            503,
            json={"error": {"code": "FAULT_INJECTED", "message": "API unavailable"}},
        )

        async with SimulatorClient(settings=test_settings) as client:
            with pytest.raises(SimulatorUnavailableError) as exc_info:
                await client.get_health()
            assert exc_info.value.code == "FAULT_INJECTED"
            assert respx_mock.calls.call_count == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_retry_on_connect_error_succeeds(test_settings: Settings):
    """Verify client retries on connection error and recovers."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        respx_mock.get("/v1/regions").side_effect = [
            httpx.ConnectError("Connection refused"),
            httpx.Response(
                200,
                json=[{"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0}],
            ),
        ]

        async with SimulatorClient(settings=test_settings) as client:
            resp = await client.get_regions()
            assert len(resp.data) == 1
            assert resp.data[0].id == "region-dhaka"
            assert respx_mock.calls.call_count == 2


@pytest.mark.asyncio
async def test_retry_on_timeout_exhausted(test_settings: Settings):
    """Verify client raises SimulatorTimeoutError when timeout persists."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        respx_mock.get("/v1/stations").side_effect = httpx.ReadTimeout("Read timed out")

        async with SimulatorClient(settings=test_settings) as client:
            with pytest.raises(SimulatorTimeoutError):
                await client.get_stations()
            assert respx_mock.calls.call_count == 3


@pytest.mark.asyncio
async def test_domain_error_not_retried(test_settings: Settings):
    """Verify 4xx domain errors fail fast on the first call without wasteful retries."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        respx_mock.post("/v1/allocations").respond(
            409,
            json={
                "detail": {
                    "code": "DESTINATION_CAPACITY_EXCEEDED",
                    "message": "Allocation exceeds free capacity",
                }
            },
        )

        async with SimulatorClient(settings=test_settings) as client:
            with pytest.raises(SimulatorDomainError) as exc_info:
                await client.create_allocation(
                    {
                        "idempotency_key": "key-1",
                        "source_depot_id": "depot-gazipur",
                        "destination_station_id": "station-mirpur",
                        "route_id": "route-gazipur-mirpur",
                        "fuel_type": FuelType.DIESEL,
                        "quantity": 999999.0,
                    }
                )
            assert exc_info.value.code == "DESTINATION_CAPACITY_EXCEEDED"
            assert respx_mock.calls.call_count == 1  # No retries!


@pytest.mark.asyncio
async def test_validation_error_not_retried(test_settings: Settings):
    """Verify 422 validation errors fail immediately on first call."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        respx_mock.post("/v1/allocations").respond(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "quantity"],
                        "msg": "Input should be greater than 0",
                        "type": "greater_than",
                    }
                ]
            },
        )

        async with SimulatorClient(settings=test_settings) as client:
            with pytest.raises(SimulatorValidationError) as exc_info:
                await client.post("/v1/allocations", json={"invalid": True})
            assert exc_info.value.code == "VALIDATION_ERROR"
            assert respx_mock.calls.call_count == 1


@pytest.mark.asyncio
async def test_create_and_cancel_allocation(test_settings: Settings):
    """Verify allocation creation and cancellation helper flows."""
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        respx_mock.post("/v1/allocations").respond(
            201,
            json={
                "id": 101,
                "idempotency_key": "alloc-key-101",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur",
                "fuel_type": "DIESEL",
                "quantity": 2500.0,
                "created_tick": 1,
                "departure_tick": None,
                "expected_arrival_tick": None,
                "actual_arrival_tick": None,
                "status": "PENDING",
                "failure_reason": None,
            },
        )
        respx_mock.post("/v1/allocations/101/cancel").respond(
            200,
            json={
                "id": 101,
                "idempotency_key": "alloc-key-101",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur",
                "fuel_type": "DIESEL",
                "quantity": 2500.0,
                "created_tick": 1,
                "departure_tick": None,
                "expected_arrival_tick": None,
                "actual_arrival_tick": None,
                "status": "CANCELLED",
                "failure_reason": None,
            },
        )

        async with SimulatorClient(settings=test_settings) as client:
            created = await client.create_allocation(
                {
                    "idempotency_key": "alloc-key-101",
                    "source_depot_id": "depot-gazipur",
                    "destination_station_id": "station-mirpur",
                    "route_id": "route-gazipur-mirpur",
                    "fuel_type": FuelType.DIESEL,
                    "quantity": 2500.0,
                }
            )
            assert created.data.id == 101
            assert created.data.status == "PENDING"

            cancelled = await client.cancel_allocation(101)
            assert cancelled.data.id == 101
            assert cancelled.data.status == "CANCELLED"
