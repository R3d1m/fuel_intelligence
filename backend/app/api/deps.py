"""FastAPI Dependency Injection Providers.

Enables modular, testable access to singleton state stores, clients,
circuit breakers, and settings. Supports FastAPI dependency overrides for unit tests.
"""

from typing import cast

from fastapi import Request

from app.adapters.simulator_client import SimulatorClient
from app.config import Settings, get_settings
from app.decisions.audit import AuditLedger
from app.decisions.service import DecisionService
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.intelligence.fallback import FallbackPlanner
from app.intelligence.forecast import DemandForecaster
from app.intelligence.planner import ConstrainedPlanner
from app.intelligence.risk import RiskEngine
from app.intelligence.twin import DigitalTwin
from app.resilience.breaker import CircuitBreaker
from app.resilience.mode import ResilienceManager


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


def get_digital_twin(request: Request) -> DigitalTwin:
    """Extract the DigitalTwin intelligence service from app.state."""
    if hasattr(request.app.state, "digital_twin") and request.app.state.digital_twin is not None:
        return cast(DigitalTwin, request.app.state.digital_twin)
    return DigitalTwin(settings=get_app_settings(), forecaster=get_forecaster(request))


def get_planner(request: Request) -> ConstrainedPlanner:
    """Extract the ConstrainedPlanner intelligence service from app.state."""
    if hasattr(request.app.state, "planner") and request.app.state.planner is not None:
        return cast(ConstrainedPlanner, request.app.state.planner)
    return ConstrainedPlanner(
        settings=get_app_settings(),
        risk_engine=get_risk_engine(request),
        twin_evaluator=get_digital_twin(request),
    )


def get_audit_ledger(request: Request) -> AuditLedger:
    """Extract the AuditLedger singleton from app.state."""
    if hasattr(request.app.state, "audit_ledger") and request.app.state.audit_ledger is not None:
        return cast(AuditLedger, request.app.state.audit_ledger)
    return AuditLedger(db_path=get_app_settings().audit_db_path)


def get_decision_service(request: Request) -> DecisionService:
    """Extract the DecisionService singleton from app.state."""
    if (
        hasattr(request.app.state, "decision_service")
        and request.app.state.decision_service is not None
    ):
        return cast(DecisionService, request.app.state.decision_service)
    return DecisionService(
        client=get_simulator_client(request),
        audit_ledger=get_audit_ledger(request),
        planner=get_planner(request),
        store=get_state_store(request),
        settings=get_app_settings(),
    )


def get_resilience_manager(request: Request) -> ResilienceManager:
    """Extract the ResilienceManager singleton from app.state."""
    if (
        hasattr(request.app.state, "resilience_manager")
        and request.app.state.resilience_manager is not None
    ):
        return cast(ResilienceManager, request.app.state.resilience_manager)
    return ResilienceManager()


def get_fallback_planner(request: Request) -> FallbackPlanner:
    """Extract the FallbackPlanner service from app.state."""
    if (
        hasattr(request.app.state, "fallback_planner")
        and request.app.state.fallback_planner is not None
    ):
        return cast(FallbackPlanner, request.app.state.fallback_planner)
    return FallbackPlanner(settings=get_app_settings())
