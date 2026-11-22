"""Unit tests for the StateStore in-memory snapshot repository."""

from datetime import UTC, datetime, timedelta

from app.domain.models import InstanceInfo, SimulatorSnapshot
from app.ingest.store import StateStore


def create_sample_snapshot(
    tick: int = 1, is_stale: bool = False, fetched_at: datetime | None = None
) -> SimulatorSnapshot:
    """Helper to construct a valid test SimulatorSnapshot."""
    inst = InstanceInfo(
        id=1,
        scenario_id="baseline",
        scenario_version="1.0",
        seed=12345,
        sim_time="2026-01-01T00:15:00",
        tick=tick,
        tick_minutes=15,
        status="RUNNING",
    )
    return SimulatorSnapshot(
        instance=inst,
        depots=[],
        stations=[],
        routes=[],
        supply_arrivals=[],
        events=[],
        allocations=[],
        tick=tick,
        sim_time="2026-01-01T00:15:00",
        fetched_at=fetched_at or datetime.now(UTC),
        is_stale=is_stale,
    )


def test_state_store_initialization():
    store = StateStore()
    assert store.get_latest() is None
    assert store.get_last_good() is None
    assert store.get_effective_snapshot() is None
    assert store.is_stale() is True
    assert store.snapshot_age_seconds() is None
    assert store.total_updates == 0
    assert store.failed_polls == 0


def test_state_store_update_fresh_snapshot():
    store = StateStore()
    snap = create_sample_snapshot(tick=5, is_stale=False)

    store.update(snap)

    assert store.get_latest() == snap
    assert store.get_last_good() == snap
    assert store.get_effective_snapshot() == snap
    assert store.total_updates == 1
    assert store.is_stale(stale_threshold_seconds=10.0) is False
    assert store.snapshot_age_seconds() is not None
    assert store.snapshot_age_seconds() >= 0.0


def test_state_store_preserves_last_good_on_stale_update():
    store = StateStore()
    fresh_snap = create_sample_snapshot(tick=10, is_stale=False)
    store.update(fresh_snap)

    stale_snap = create_sample_snapshot(tick=11, is_stale=True)
    store.update(stale_snap)

    # Latest is the stale one, but last_good remains the fresh one!
    assert store.get_latest() == stale_snap
    assert store.get_last_good() == fresh_snap
    assert store.is_stale() is True
    assert store.total_updates == 2


def test_state_store_staleness_by_threshold():
    store = StateStore()
    old_time = datetime.now(UTC) - timedelta(seconds=15)
    old_snap = create_sample_snapshot(tick=1, is_stale=False, fetched_at=old_time)

    store.update(old_snap)

    # Threshold is 10.0s, age is ~15s -> should evaluate to stale
    assert store.is_stale(stale_threshold_seconds=10.0) is True
    # If threshold is 30.0s -> not stale
    assert store.is_stale(stale_threshold_seconds=30.0) is False


def test_state_store_poll_failure_tracking():
    store = StateStore()
    assert store.failed_polls == 0

    store.record_poll_failure()
    store.record_poll_failure()

    assert store.failed_polls == 2


def test_state_store_clear():
    store = StateStore()
    snap = create_sample_snapshot(tick=1, is_stale=False)
    store.update(snap)
    store.record_poll_failure()

    store.clear()

    assert store.get_latest() is None
    assert store.get_last_good() is None
    assert store.total_updates == 0
    assert store.failed_polls == 0
    assert store.is_stale() is True
