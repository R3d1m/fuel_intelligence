"""Unit tests for the SimulatorPoller background ingestion loop."""

import asyncio

import httpx
import pytest
import respx
from app.adapters.simulator_client import SimulatorClient
from app.config import Settings
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.resilience.breaker import CircuitBreaker


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        simulator_base_url="http://mock-simulator:8000",
        simulator_timeout_seconds=0.5,
        simulator_max_retries=1,
        simulator_backoff_factor=0.01,
        poll_interval_seconds=0.05,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_recovery_timeout=1.0,
    )


def mock_all_endpoints(respx_mock, tick: int = 2, is_stale: bool = False):
    headers = {"X-Simulator-Stale": "true"} if is_stale else {}

    respx_mock.get("/v1/instance").respond(
        200,
        headers=headers,
        json={
            "id": 1,
            "scenario_id": "baseline",
            "scenario_version": "1.0",
            "seed": 12345,
            "sim_time": "2026-01-01T00:30:00",
            "tick": tick,
            "tick_minutes": 15,
            "status": "RUNNING",
        },
    )
    respx_mock.get("/v1/depots").respond(
        200,
        headers=headers,
        json=[
            {
                "id": "depot-gazipur",
                "name": "Gazipur Depot",
                "region_id": "region-dhaka",
                "status": "OPEN",
                "dispatch_capacity_per_tick": 12000,
                "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
                "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
            }
        ],
    )
    respx_mock.get("/v1/stations").respond(
        200,
        headers=headers,
        json=[
            {
                "id": "station-mirpur",
                "name": "Mirpur Station",
                "region_id": "region-dhaka",
                "status": "OPEN",
                "demand_profile": "urban_high",
                "demand_multiplier": 1.0,
                "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
                "inventory": {"DIESEL": 9000, "PETROL": 9000, "OCTANE": 5000},
            }
        ],
    )
    respx_mock.get("/v1/routes").respond(
        200,
        headers=headers,
        json=[
            {
                "id": "route-gazipur-mirpur",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "transit_ticks": 2,
                "max_shipment": 7000,
                "status": "AVAILABLE",
            }
        ],
    )
    respx_mock.get("/v1/supply-arrivals").respond(
        200,
        headers=headers,
        json=[
            {
                "id": "supply-001",
                "depot_id": "depot-gazipur",
                "fuel_type": "DIESEL",
                "quantity": 18000,
                "planned_tick": 12,
                "actual_tick": None,
                "status": "SCHEDULED",
            }
        ],
    )
    respx_mock.get("/v1/events").respond(
        200,
        headers=headers,
        json=[],
    )
    respx_mock.get("/v1/allocations").respond(
        200,
        headers=headers,
        json=[],
    )


@pytest.mark.asyncio
async def test_poller_successful_poll_cycle(test_settings: Settings):
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        mock_all_endpoints(respx_mock, tick=7, is_stale=False)

        client = SimulatorClient(settings=test_settings)
        store = StateStore()
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0)
        poller = SimulatorPoller(
            client=client,
            store=store,
            breaker=breaker,
            settings=test_settings,
        )

        snapshot = await poller.poll_once()

        assert snapshot is not None
        assert snapshot.tick == 7
        assert snapshot.is_stale is False
        assert len(snapshot.depots) == 1
        assert len(snapshot.stations) == 1
        assert len(snapshot.routes) == 1
        assert snapshot.depots[0].id == "depot-gazipur"

        # Verify state store was updated
        assert store.get_latest() == snapshot
        assert store.get_last_good() == snapshot
        assert store.total_updates == 1
        assert store.failed_polls == 0

        # Verify breaker remains CLOSED
        assert breaker.is_closed is True
        assert breaker.failure_count == 0

        await client.close()


@pytest.mark.asyncio
async def test_poller_detects_stale_header(test_settings: Settings):
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        mock_all_endpoints(respx_mock, tick=8, is_stale=True)

        client = SimulatorClient(settings=test_settings)
        store = StateStore()
        poller = SimulatorPoller(client=client, store=store, settings=test_settings)

        snapshot = await poller.poll_once()

        assert snapshot is not None
        assert snapshot.is_stale is True
        assert store.get_latest().is_stale is True
        assert store.is_stale() is True

        await client.close()


@pytest.mark.asyncio
async def test_poller_failure_trips_circuit_breaker(test_settings: Settings):
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        # Simulator returns 503 for instance endpoint
        respx_mock.get("/v1/instance").respond(
            503,
            json={"error": {"code": "FAULT_INJECTED", "message": "Service down"}},
        )
        respx_mock.get(httpx.URL("http://mock-simulator:8000")).pass_through()

        client = SimulatorClient(settings=test_settings)
        store = StateStore()
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0)
        poller = SimulatorPoller(
            client=client,
            store=store,
            breaker=breaker,
            settings=test_settings,
        )

        # 1st failure
        snap1 = await poller.poll_once()
        assert snap1 is None
        assert store.failed_polls == 1
        assert breaker.failure_count == 1
        assert breaker.is_closed is True

        # 2nd failure -> trips circuit breaker to OPEN
        snap2 = await poller.poll_once()
        assert snap2 is None
        assert store.failed_polls == 2
        assert breaker.is_open is True
        assert breaker.can_execute() is False

        # 3rd cycle: Breaker is OPEN -> skips HTTP call entirely
        snap3 = await poller.poll_once()
        assert snap3 is None
        assert store.failed_polls == 3

        await client.close()


@pytest.mark.asyncio
async def test_poller_background_runner_lifecycle(test_settings: Settings):
    with respx.mock(base_url="http://mock-simulator:8000") as respx_mock:
        mock_all_endpoints(respx_mock, tick=3, is_stale=False)

        client = SimulatorClient(settings=test_settings)
        store = StateStore()
        poller = SimulatorPoller(
            client=client,
            store=store,
            poll_interval_seconds=0.02,
            settings=test_settings,
        )

        assert poller.is_running is False
        await poller.start()
        assert poller.is_running is True

        # Allow loop to execute a few cycles
        await asyncio.sleep(0.08)
        assert store.total_updates >= 1

        await poller.stop()
        assert poller.is_running is False

        await client.close()
