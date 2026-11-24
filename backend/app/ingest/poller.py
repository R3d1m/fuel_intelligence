"""Simulator Background Poller.

Runs an asynchronous periodic background polling loop that fetches all simulation
resources from the simulator API, bundles them into an atomic, validated
SimulatorSnapshot, and updates the StateStore.

Integrates with the CircuitBreaker to protect against cascading failures and
prevent hammering an unavailable simulator during outages.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.adapters.simulator_client import SimulatorClient
from app.config import Settings, get_settings
from app.domain.models import SimulatorSnapshot
from app.ingest.store import StateStore
from app.resilience.breaker import CircuitBreaker

logger = logging.getLogger(__name__)


class SimulatorPoller:
    """Async background polling service that synchronizes simulator state into the StateStore."""

    def __init__(
        self,
        client: SimulatorClient,
        store: StateStore,
        breaker: CircuitBreaker | None = None,
        poll_interval_seconds: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.client = client
        self.store = store
        self.poll_interval_seconds = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else cfg.poll_interval_seconds
        )
        self.breaker = breaker or CircuitBreaker(
            failure_threshold=cfg.circuit_breaker_failure_threshold,
            recovery_timeout=cfg.circuit_breaker_recovery_timeout,
            name="simulator-poller",
        )

        self._running: bool = False
        self._task: asyncio.Task[Any] | None = None

    @property
    def is_running(self) -> bool:
        """Check whether the background polling loop is active."""
        return self._running and self._task is not None and not self._task.done()

    async def poll_once(self) -> SimulatorSnapshot | None:
        """Execute a single atomic polling cycle across all simulation resources.

        Returns:
            SimulatorSnapshot on success.
            None if polling failed or was skipped due to an OPEN circuit breaker.
        """
        # Guard against calling simulator when circuit breaker is OPEN
        if not self.breaker.can_execute():
            logger.warning(
                "Circuit breaker '%s' is OPEN. Skipping poll cycle. Cooldown remaining: %.2fs",
                self.breaker.name,
                self.breaker.cooldown_remaining,
            )
            self.store.record_poll_failure()
            return None

        try:
            # Fetch all core entities concurrently for minimal polling latency
            (
                inst_resp,
                depots_resp,
                stations_resp,
                routes_resp,
                arrivals_resp,
                events_resp,
                allocs_resp,
            ) = await asyncio.gather(
                self.client.get_instance(),
                self.client.get_depots(),
                self.client.get_stations(),
                self.client.get_routes(),
                self.client.get_supply_arrivals(),
                self.client.get_events(),
                self.client.get_allocations(),
            )

            # Determine whether any individual resource returned a stale data header
            is_stale = any(
                resp.is_stale
                for resp in [
                    inst_resp,
                    depots_resp,
                    stations_resp,
                    routes_resp,
                    arrivals_resp,
                    events_resp,
                    allocs_resp,
                ]
            )

            # Assemble validated atomic snapshot
            snapshot = SimulatorSnapshot(
                instance=inst_resp.data,
                depots=depots_resp.data,
                stations=stations_resp.data,
                routes=routes_resp.data,
                supply_arrivals=arrivals_resp.data,
                events=events_resp.data,
                allocations=allocs_resp.data,
                tick=inst_resp.data.tick,
                sim_time=str(inst_resp.data.sim_time),
                fetched_at=datetime.now(UTC),
                is_stale=is_stale,
            )

            # Ingest into state store and signal circuit success
            accepted = self.store.update(snapshot)
            if not accepted:
                logger.warning(
                    "Snapshot at tick %d was rejected by StateStore due to data validation errors. "
                    "Preserving last-known-good state.",
                    snapshot.tick,
                )
                self.breaker.record_failure()
                self.store.record_poll_failure()
                return None

            self.breaker.record_success()
            return snapshot

        except Exception as exc:
            logger.warning(
                "Polling cycle failed: %s (%s). Recording failure in circuit breaker.",
                type(exc).__name__,
                exc,
            )
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                val_errors = [
                    f"{'.'.join(str(loc) for loc in err.get('loc', []))}: "
                    f"{err.get('msg', 'invalid field')}"
                    for err in exc.errors()
                ]
                self.store.record_malformed_rejection(errors=val_errors)

            self.breaker.record_failure()
            self.store.record_poll_failure()
            return None

    async def _run_loop(self) -> None:
        """Internal continuous polling loop."""
        logger.info(
            "Simulator background poller started (interval: %.2fs).", self.poll_interval_seconds
        )
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Unexpected error inside poller loop: %s", exc)

            try:
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                break
        logger.info("Simulator background poller stopped.")

    async def start(self) -> None:
        """Start the background polling task."""
        if self._running:
            logger.warning("Poller is already running.")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="simulator-poller-task")

    async def stop(self) -> None:
        """Gracefully stop the background polling task."""
        if not self._running:
            return

        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def __aenter__(self) -> "SimulatorPoller":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()
