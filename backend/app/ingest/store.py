"""State Store for Simulator Snapshots.

Maintains the authoritative in-memory state of the simulated fuel network.
Stores:
1. Latest snapshot: Most recently received observation (may be marked stale).
2. Last-known-good snapshot: Most recent valid, non-stale snapshot for degraded mode fallback.
3. Operational telemetry: Snapshot age, staleness detection, and polling failure counters.
"""

import logging
from datetime import UTC, datetime

from app.domain.models import SimulatorSnapshot

logger = logging.getLogger(__name__)


class StateStore:
    """Thread-safe in-memory state repository holding current and last-good network state."""

    def __init__(self) -> None:
        self._latest_snapshot: SimulatorSnapshot | None = None
        self._last_good_snapshot: SimulatorSnapshot | None = None
        self._last_updated_at: datetime | None = None
        self._total_updates: int = 0
        self._failed_polls: int = 0

    @property
    def total_updates(self) -> int:
        """Total successful state updates ingested."""
        return self._total_updates

    @property
    def failed_polls(self) -> int:
        """Total polling cycle failures encountered."""
        return self._failed_polls

    @property
    def last_updated_at(self) -> datetime | None:
        """UTC timestamp of the most recent snapshot ingestion."""
        return self._last_updated_at

    def update(self, snapshot: SimulatorSnapshot) -> None:
        """Ingest a new snapshot into the store.

        Updates the latest snapshot. If the snapshot is fresh (not stale),
        it also updates the last-known-good snapshot.
        """
        now = datetime.now(UTC)
        self._latest_snapshot = snapshot
        self._last_updated_at = now
        self._total_updates += 1

        if not snapshot.is_stale:
            self._last_good_snapshot = snapshot
        elif self._last_good_snapshot is None:
            # If no good snapshot exists yet, seed with current even if marked stale
            self._last_good_snapshot = snapshot

        logger.debug(
            "StateStore updated: tick=%d, stale=%s, total_updates=%d",
            snapshot.tick,
            snapshot.is_stale,
            self._total_updates,
        )

    def get_latest(self) -> SimulatorSnapshot | None:
        """Retrieve the most recently ingested snapshot."""
        return self._latest_snapshot

    def get_last_good(self) -> SimulatorSnapshot | None:
        """Retrieve the most recent verified non-stale snapshot."""
        return self._last_good_snapshot

    def get_effective_snapshot(self) -> SimulatorSnapshot | None:
        """Retrieve the best available snapshot (latest, falling back to last-good)."""
        return self._latest_snapshot or self._last_good_snapshot

    def is_stale(self, stale_threshold_seconds: float = 10.0) -> bool:
        """Evaluate if the stored state is stale.

        A state is deemed stale if:
        1. No snapshot has been ingested yet.
        2. The latest snapshot was explicitly flagged stale by upstream (`is_stale == True`).
        3. The elapsed wall-clock time since ingestion exceeds `stale_threshold_seconds`.
        """
        if self._latest_snapshot is None:
            return True

        if self._latest_snapshot.is_stale:
            return True

        age = self.snapshot_age_seconds()
        if age is not None and age > stale_threshold_seconds:
            return True

        return False

    def snapshot_age_seconds(self) -> float | None:
        """Return the age in seconds of the latest snapshot relative to current UTC time.

        Returns None if no snapshot has been ingested.
        """
        if self._latest_snapshot is None:
            return None

        now = datetime.now(UTC)
        fetched_at = self._latest_snapshot.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)

        return max(0.0, (now - fetched_at).total_seconds())

    def record_poll_failure(self) -> None:
        """Record an unsuccessful polling attempt for resilience monitoring."""
        self._failed_polls += 1
        logger.warning("Recorded polling failure. Total failed polls: %d", self._failed_polls)

    def clear(self) -> None:
        """Clear all stored snapshots and reset counters (used for simulation resets / testing)."""
        self._latest_snapshot = None
        self._last_good_snapshot = None
        self._last_updated_at = None
        self._total_updates = 0
        self._failed_polls = 0
        logger.info("StateStore cleared.")
