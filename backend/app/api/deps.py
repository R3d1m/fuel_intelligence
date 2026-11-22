"""FastAPI Dependency Injection Providers.

Enables modular, testable access to singleton state stores, clients,
circuit breakers, and settings. Supports FastAPI dependency overrides for unit tests.
"""

from typing import cast

from fastapi import Request

from app.adapters.simulator_client import SimulatorClient
from app.config import Settings, get_settings
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.resilience.breaker import CircuitBreaker


def get_app_settings() -> Settings:
    """Return the platform configuration settings."""
    return get_settings()


def get_state_store(request: Request) -> StateStore:
    """Extract the application StateStore singleton from app.state."""
    return cast(StateStore, request.app.state.store)


def get_circuit_breaker(request: Request) -> CircuitBreaker:
    """Extract the application CircuitBreaker singleton from app.state."""
    return cast(CircuitBreaker, request.app.state.breaker)


def get_simulator_client(request: Request) -> SimulatorClient:
    """Extract the application SimulatorClient singleton from app.state."""
    return cast(SimulatorClient, request.app.state.simulator_client)


def get_poller(request: Request) -> SimulatorPoller:
    """Extract the background SimulatorPoller service from app.state."""
    return cast(SimulatorPoller, request.app.state.poller)
