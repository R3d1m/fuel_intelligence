"""FastAPI Application Entrypoint for Fuel Intelligence & Resilience Platform.

Wires together the adapter layer, in-memory state repository, circuit breaker,
and background poller loop with robust lifecycle management and dependency injection.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.simulator_client import SimulatorClient
from app.api.routes import router as api_router
from app.config import Settings, get_settings
from app.ingest.poller import SimulatorPoller
from app.ingest.store import StateStore
from app.intelligence.forecast import DemandForecaster
from app.intelligence.risk import RiskEngine
from app.resilience.breaker import CircuitBreaker

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    client: SimulatorClient | None = None,
    store: StateStore | None = None,
    breaker: CircuitBreaker | None = None,
    poller: SimulatorPoller | None = None,
    forecaster: DemandForecaster | None = None,
    risk_engine: RiskEngine | None = None,
    start_poller: bool = True,
) -> FastAPI:
    """Application factory for the Fuel Intelligence backend.

    Allows injecting mock or custom components during testing or standalone execution.
    """
    cfg = settings or get_settings()

    # Pre-instantiate singletons so they are bound immediately to app.state
    app_client = client or SimulatorClient(
        base_url=cfg.simulator_base_url,
        timeout_seconds=cfg.simulator_timeout_seconds,
        max_retries=cfg.simulator_max_retries,
        backoff_factor=cfg.simulator_backoff_factor,
        settings=cfg,
    )
    app_store = store or StateStore()
    app_breaker = breaker or CircuitBreaker(
        failure_threshold=cfg.circuit_breaker_failure_threshold,
        recovery_timeout=cfg.circuit_breaker_recovery_timeout,
        name="simulator-poller",
    )
    app_poller = poller or SimulatorPoller(
        client=app_client,
        store=app_store,
        breaker=app_breaker,
        poll_interval_seconds=cfg.poll_interval_seconds,
        settings=cfg,
    )
    app_forecaster = forecaster or DemandForecaster(settings=cfg)
    app_risk_engine = risk_engine or RiskEngine(settings=cfg, forecaster=app_forecaster)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        logger.info(
            "Fuel Intelligence Platform starting (Simulator: %s, Poll interval: %.1fs)",
            cfg.simulator_base_url,
            cfg.poll_interval_seconds,
        )

        # Start background poller if enabled
        if start_poller:
            await app_poller.start()

        try:
            yield
        finally:
            logger.info("Fuel Intelligence Platform shutting down...")
            if start_poller and app_poller.is_running:
                await app_poller.stop()
            await app_client.close()
            logger.info("Teardown complete.")

    app = FastAPI(
        title="Fuel Supply Intelligence & Resilience Platform",
        version="0.1.0",
        description=(
            "Fault-tolerant decision support platform observing simulated fuel operations, "
            "forecasting stockouts, running Monte Carlo digital twins, "
            "and presenting operator dispatches."
        ),
        lifespan=lifespan,
    )

    # Attach singletons immediately to app.state
    app.state.settings = cfg
    app.state.simulator_client = app_client
    app.state.store = app_store
    app.state.breaker = app_breaker
    app.state.poller = app_poller
    app.state.forecaster = app_forecaster
    app.state.risk_engine = app_risk_engine

    # Enable CORS for local dashboards and browser clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Attach API routers
    app.include_router(api_router)

    return app


# Default ASGI application instance
app = create_app()
