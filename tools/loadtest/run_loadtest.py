"""Automated Locust load-test runner against a live local Uvicorn backend.

Starts the real Fuel Intelligence FastAPI application on 127.0.0.1:8011
with a realistic seeded StateStore, executes headless Locust runs across
multiple concurrency levels (10, 50, 100, 200 users), and records
throughput, latency percentiles (avg, p50, p95, p99), error rate, and
process CPU/memory utilization.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil
import uvicorn

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "backend"))
sys.path.insert(0, str(ROOT_DIR))

from app.adapters.simulator_client import SimulatorClient  # noqa: E402
from app.api.main import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.models import (  # noqa: E402
    Depot,
    DepotStatus,
    FuelMap,
    InstanceInfo,
    Route,
    RouteStatus,
    SimulatorSnapshot,
    Station,
    StationStatus,
)
from app.ingest.store import StateStore  # noqa: E402


def _build_seed_snapshot() -> SimulatorSnapshot:
    """Create a realistic 2-depot, 4-station, 4-route snapshot for load testing."""
    now = datetime.now(UTC)
    sim_time_str = now.isoformat()
    return SimulatorSnapshot(
        tick=12,
        sim_time=sim_time_str,
        instance=InstanceInfo(
            id=1,
            scenario_id="loadtest-sim",
            scenario_version="1.0.0",
            seed=42,
            sim_time=sim_time_str,
            tick=12,
            tick_minutes=8,
            status="PAUSED",
        ),
        depots=[
            Depot(
                id="depot-gazipur",
                name="Gazipur Depot",
                region_id="reg-dhaka",
                status=DepotStatus.OPEN,
                capacity=FuelMap(DIESEL=200000.0, PETROL=150000.0, OCTANE=150000.0),
                inventory=FuelMap(DIESEL=120000.0, PETROL=95000.0, OCTANE=90000.0),
                dispatch_capacity_per_tick=40000.0,
            ),
            Depot(
                id="depot-narayanganj",
                name="Narayanganj Depot",
                region_id="reg-dhaka",
                status=DepotStatus.OPEN,
                capacity=FuelMap(DIESEL=180000.0, PETROL=140000.0, OCTANE=130000.0),
                inventory=FuelMap(DIESEL=100000.0, PETROL=85000.0, OCTANE=80000.0),
                dispatch_capacity_per_tick=35000.0,
            ),
        ],
        stations=[
            Station(
                id="station-mirpur",
                name="Mirpur Filling Station",
                region_id="reg-dhaka",
                status=StationStatus.OPEN,
                demand_profile="urban_high",
                capacity=FuelMap(DIESEL=30000.0, PETROL=25000.0, OCTANE=25000.0),
                inventory=FuelMap(DIESEL=4500.0, PETROL=8000.0, OCTANE=3500.0),
                demand_multiplier=1.4,
            ),
            Station(
                id="station-uttara",
                name="Uttara Service Station",
                region_id="reg-dhaka",
                status=StationStatus.OPEN,
                demand_profile="urban_medium",
                capacity=FuelMap(DIESEL=28000.0, PETROL=24000.0, OCTANE=22000.0),
                inventory=FuelMap(DIESEL=11000.0, PETROL=10000.0, OCTANE=9000.0),
                demand_multiplier=1.0,
            ),
            Station(
                id="station-tongi",
                name="Tongi Highway Pump",
                region_id="reg-dhaka",
                status=StationStatus.OPEN,
                demand_profile="highway_high",
                capacity=FuelMap(DIESEL=25000.0, PETROL=22000.0, OCTANE=20000.0),
                inventory=FuelMap(DIESEL=14000.0, PETROL=11000.0, OCTANE=12000.0),
                demand_multiplier=1.0,
            ),
            Station(
                id="station-dhanmondi",
                name="Dhanmondi Fuel Hub",
                region_id="reg-dhaka",
                status=StationStatus.OPEN,
                demand_profile="urban_medium",
                capacity=FuelMap(DIESEL=32000.0, PETROL=26000.0, OCTANE=26000.0),
                inventory=FuelMap(DIESEL=18000.0, PETROL=14000.0, OCTANE=15000.0),
                demand_multiplier=1.0,
            ),
        ],
        routes=[
            Route(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                status=RouteStatus.AVAILABLE,
                transit_ticks=2,
                max_shipment=10000.0,
            ),
            Route(
                id="route-gazipur-uttara",
                source_depot_id="depot-gazipur",
                destination_station_id="station-uttara",
                status=RouteStatus.AVAILABLE,
                transit_ticks=2,
                max_shipment=10000.0,
            ),
            Route(
                id="route-gazipur-tongi",
                source_depot_id="depot-gazipur",
                destination_station_id="station-tongi",
                status=RouteStatus.AVAILABLE,
                transit_ticks=1,
                max_shipment=12000.0,
            ),
            Route(
                id="route-narayanganj-dhanmondi",
                source_depot_id="depot-narayanganj",
                destination_station_id="station-dhanmondi",
                status=RouteStatus.AVAILABLE,
                transit_ticks=3,
                max_shipment=10000.0,
            ),
        ],
        supply_arrivals=[],
        events=[],
        allocations=[],
        fetched_at=now,
    )


def main() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="fuel_loadtest_")
    db_path = os.path.join(tmp_dir, "loadtest_decisions.db")
    store = StateStore()
    store.update(_build_seed_snapshot())

    settings = Settings(
        app_env="production",
        log_level="WARNING",
        audit_db_path=db_path,
        stale_threshold_seconds=3600.0,
    )

    def _mock_sim_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/demand-history":
            records = [
                {
                    "id": idx,
                    "tick": t,
                    "sim_time": f"2026-11-25T{t % 24:02d}:00:00+00:00",
                    "station_id": st,
                    "fuel_type": fuel,
                    "demand_liters": 420.0 + (t % 5) * 25.0,
                    "served_liters": 420.0 + (t % 5) * 25.0,
                    "unmet_liters": 0.0,
                }
                for idx, (st, fuel, t) in enumerate(
                    (
                        (s, f, tick)
                        for s in (
                            "station-mirpur",
                            "station-uttara",
                            "station-tongi",
                            "station-dhanmondi",
                        )
                        for f in ("DIESEL", "PETROL", "OCTANE")
                        for tick in range(1, 13)
                    ),
                    start=1,
                )
            ]
            return httpx.Response(200, json=records)
        return httpx.Response(200, json={})

    mock_http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_sim_handler))
    sim_client = SimulatorClient(
        base_url="http://mock-simulator:8000",
        client=mock_http_client,
        settings=settings,
    )
    app = create_app(settings=settings, client=sim_client, store=store, start_poller=False)

    port = 8011
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)

    proc = psutil.Process(os.getpid())
    proc.cpu_percent(interval=None)

    locust_bin = str(ROOT_DIR / ".venv" / "Scripts" / "locust.exe")
    locustfile = str(ROOT_DIR / "tools" / "loadtest" / "locustfile.py")

    concurrency_levels = [10, 50, 100, 200]
    results: list[dict[str, str]] = []

    try:
        for users in concurrency_levels:
            csv_prefix = os.path.join(tmp_dir, f"run_{users}")
            spawn_rate = max(5, users // 2)
            cmd = [
                locust_bin,
                "-f",
                locustfile,
                "--headless",
                "--host",
                base_url,
                "-u",
                str(users),
                "-r",
                str(spawn_rate),
                "-t",
                "10s",
                "--csv",
                csv_prefix,
                "--only-summary",
            ]
            proc.cpu_percent(interval=None)
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cpu_pct = proc.cpu_percent(interval=None)
            mem_mb = proc.memory_info().rss / (1024 * 1024)

            stats_csv = f"{csv_prefix}_stats.csv"
            with open(stats_csv, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            agg = next(r for r in rows if r["Name"] == "Aggregated")
            req_count = int(agg["Request Count"])
            fail_count = int(agg["Failure Count"])
            err_pct = (fail_count / req_count * 100.0) if req_count > 0 else 0.0
            results.append(
                {
                    "users": str(users),
                    "rps": f"{float(agg['Requests/s']):.1f}",
                    "avg_ms": f"{float(agg['Average Response Time']):.1f}",
                    "p50_ms": f"{float(agg['50%']):.0f}",
                    "p95_ms": f"{float(agg['95%']):.0f}",
                    "p99_ms": f"{float(agg['99%']):.0f}",
                    "err_pct": f"{err_pct:.1f}%",
                    "cpu_pct": f"{cpu_pct:.1f}%",
                    "mem_mb": f"{mem_mb:.1f} MB",
                }
            )
            print(results[-1])
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)


if __name__ == "__main__":
    main()
