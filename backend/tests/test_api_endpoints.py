"""Integration tests for FastAPI health and state endpoints."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from app.adapters.simulator_client import SimulatorClient
from app.api.main import create_app
from app.config import Settings
from app.domain.models import InstanceInfo, OperationalMode, SimulatorSnapshot
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.resilience.breaker import CircuitBreaker


def _make_dummy_snapshot(is_stale: bool = False, tick: int = 15) -> SimulatorSnapshot:
    return SimulatorSnapshot(
        instance=InstanceInfo(
            id=1,
            scenario_id="bup-standard",
            scenario_version="1.0",
            seed=12345,
            sim_time="2026-11-22T10:00:00Z",
            tick=tick,
            tick_minutes=15,
            status="RUNNING",
        ),
        depots=[],
        stations=[],
        routes=[],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=tick,
        sim_time="2026-11-22T10:00:00Z",
        fetched_at=datetime.now(UTC),
        is_stale=is_stale,
    )


@pytest.fixture
def test_setup() -> dict[str, Any]:
    settings = Settings(
        simulator_base_url="http://test-sim:8000",
        poll_interval_seconds=1.0,
        stale_threshold_seconds=10.0,
        circuit_breaker_failure_threshold=3,
        circuit_breaker_recovery_timeout=5.0,
    )
    client = SimulatorClient(base_url="http://test-sim:8000")
    store = StateStore()
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=5.0, name="test-breaker")
    poller = SimulatorPoller(client=client, store=store, breaker=breaker, settings=settings)
    app = create_app(
        settings=settings,
        client=client,
        store=store,
        breaker=breaker,
        poller=poller,
        start_poller=False,
    )
    return {
        "app": app,
        "store": store,
        "breaker": breaker,
        "poller": poller,
        "client": client,
        "settings": settings,
    }


@pytest.mark.asyncio
async def test_health_endpoint_healthy(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    store.update(_make_dummy_snapshot(is_stale=False))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "healthy"
        assert data["mode"] == OperationalMode.NORMAL.value
        assert data["simulator"]["connected"] is True
        assert data["simulator"]["circuit_state"] == "CLOSED"
        assert data["store"]["has_snapshot"] is True
        assert data["store"]["is_stale"] is False
        assert data["store"]["total_updates"] == 1


@pytest.mark.asyncio
async def test_health_endpoint_degraded_when_breaker_open(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    breaker: CircuitBreaker = test_setup["breaker"]
    store: StateStore = test_setup["store"]
    store.update(_make_dummy_snapshot(is_stale=False))

    # Trip breaker
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state.value == "OPEN"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "degraded"
        assert data["mode"] == OperationalMode.DEGRADED_CACHED.value
        assert data["simulator"]["connected"] is False
        assert data["simulator"]["circuit_state"] == "OPEN"
        assert data["simulator"]["cooldown_remaining"] > 0.0


@pytest.mark.asyncio
async def test_state_endpoint_empty_store_returns_503(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/state")
        assert resp.status_code == 503
        data = resp.json()
        assert "No simulator snapshot available yet" in data["detail"]["message"]
        assert data["detail"]["mode"] == OperationalMode.DEGRADED_CACHED.value


@pytest.mark.asyncio
async def test_state_endpoint_serves_snapshot_in_normal_mode(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    store.update(_make_dummy_snapshot(is_stale=False, tick=42))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/state")
        assert resp.status_code == 200
        data = resp.json()

        assert data["mode"] == OperationalMode.NORMAL.value
        assert not data["is_stale"]
        assert data["circuit_state"] == "CLOSED"
        assert data["snapshot"]["tick"] == 42
        assert data["snapshot"]["instance"]["scenario_id"] == "bup-standard"


@pytest.mark.asyncio
async def test_state_endpoint_serves_cached_snapshot_during_breaker_trip(
    test_setup: dict[str, Any],
) -> None:
    app = test_setup["app"]
    store: StateStore = test_setup["store"]
    breaker: CircuitBreaker = test_setup["breaker"]

    # Store a good snapshot
    store.update(_make_dummy_snapshot(is_stale=False, tick=88))

    # Simulate simulator outage tripping the circuit breaker
    for _ in range(3):
        breaker.record_failure()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/state")
        assert resp.status_code == 200
        data = resp.json()

        # CRITICAL RESILIENCE BEHAVIOR:
        # Platform answers from cached last-known-good state with mode DEGRADED_CACHED
        assert data["mode"] == OperationalMode.DEGRADED_CACHED.value
        assert data["circuit_state"] == "OPEN"
        assert data["snapshot"] is not None
        assert data["snapshot"]["tick"] == 88


@pytest.mark.asyncio
@respx.mock
async def test_sync_endpoint_success(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    base = "http://test-sim:8000"

    # Mock simulator endpoints
    inst_data = {
        "id": 1,
        "scenario_id": "bup-standard",
        "scenario_version": "1.0",
        "seed": 12345,
        "sim_time": "2026-11-22T08:00:00Z",
        "tick": 7,
        "tick_minutes": 15,
        "status": "RUNNING",
    }
    respx.get(f"{base}/v1/instance").respond(json=inst_data)
    respx.get(f"{base}/v1/depots").respond(json=[])
    respx.get(f"{base}/v1/stations").respond(json=[])
    respx.get(f"{base}/v1/routes").respond(json=[])
    respx.get(f"{base}/v1/supply-arrivals").respond(json=[])
    respx.get(f"{base}/v1/events").respond(json=[])
    respx.get(f"{base}/v1/allocations").respond(json=[])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["tick"] == 7
        assert "Successfully synchronized" in data["message"]


@pytest.mark.asyncio
async def test_sync_endpoint_rejected_when_circuit_open(test_setup: dict[str, Any]) -> None:
    app = test_setup["app"]
    breaker: CircuitBreaker = test_setup["breaker"]
    for _ in range(3):
        breaker.record_failure()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "Sync rejected" in data["message"]
