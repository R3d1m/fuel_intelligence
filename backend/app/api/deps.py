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
from app.intelligence.forecast import DemandForecaster
from app.intelligence.risk import RiskEngine
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


def get_forecaster(request: Request) -> DemandForecaster:
    """Extract the DemandForecaster intelligence service from app.state."""
    if hasattr(request.app.state, "forecaster") and request.app.state.forecaster is not None:
        return cast(DemandForecaster, request.app.state.forecaster)
    return DemandForecaster(settings=get_app_settings())


def get_risk_engine(request: Request) -> RiskEngine:
    """Extract the RiskEngine intelligence service from app.state."""
    if hasattr(request.app.state, "risk_engine") and request.app.state.risk_engine is not None:
        return cast(RiskEngine, request.app.state.risk_engine)
    return RiskEngine(settings=get_app_settings(), forecaster=get_forecaster(request))
