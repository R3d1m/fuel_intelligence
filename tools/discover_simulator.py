"""Automated experimental discovery probe for Fuel Supply Simulator.

Executes controlled empirical tests against the running simulator to verify
real behavioral mechanics for Day 1 Block 2:
1. Actual tick_minutes from /v1/instance.
2. Demand profile math vs observed demand per tick.
3. Allocation lifecycle and immediate depot inventory deduction.
4. Destination capacity check and in-transit fuel consideration.
5. Failed allocation semantics (route disruption at departure).
6. Cancellation refund and idempotency key reuse / replay status codes.
7. Determinism across resets.
"""

import json

import httpx

BASE_URL = "http://localhost:8000"
client = httpx.Client(base_url=BASE_URL, timeout=10.0)


def heading(title: str):
    print("\n" + "=" * 70)
    print(f"EXPERIMENT: {title}")
    print("=" * 70)


def reset_sim():
    r = client.post("/admin/reset")
    r.raise_for_status()
    # ensure it is paused
    client.post("/admin/pause")


def step_sim():
    r = client.post("/admin/step")
    r.raise_for_status()
    return r.json()


def run_discovery():
    results = {}

    # -------------------------------------------------------------
    # Experiment 1: Instance Configuration & tick_minutes
    # -------------------------------------------------------------
    heading("1. Instance Configuration & tick_minutes")
    reset_sim()
    inst = client.get("/v1/instance").json()
    print("Instance payload:", json.dumps(inst, indent=2))
    results["tick_minutes"] = inst.get("tick_minutes")
    results["seed"] = inst.get("seed")
    results["scenario_id"] = inst.get("scenario_id")
    print(f"--> Observed tick_minutes: {results['tick_minutes']}")

    # -------------------------------------------------------------
    # Experiment 2: Demand Profile Math vs Observed Demand
    # -------------------------------------------------------------
    heading("2. Demand Profile Math vs Observed Demand")
    # Step 1 tick from tick 0 to tick 1
    step_res = step_sim()
    print("Stepped to tick:", step_res)
    history = client.get("/v1/demand-history?station_id=station-mirpur&limit=5").json()
    print("Mirpur demand history row at tick 1:", json.dumps(history[0], indent=2))

    # Mirpur diesel calculations:
    # Baseline: 8500 L/day. Day = 1440 minutes.
    # Ticks per day = 1440 / tick_minutes
    tick_mins = results["tick_minutes"]
    ticks_per_day = 1440.0 / tick_mins
    base_per_tick = 8500.0 / ticks_per_day

    # sim_time started at 2026-01-01T00:00:00. At tick 1, sim_time is 00:00 + tick_mins minutes
    # Urban high off-peak factor is 0.70 (from guide: 07-09 or 16-20 -> 1.45, else 0.70)
    expected_nominal = base_per_tick * 0.70
    actual_observed = history[0]["demand_liters"]
    print(f"--> Expected nominal (without noise): {expected_nominal:.3f} L")
    print(f"--> Observed demand: {actual_observed:.3f} L")
    diff_pct = abs(actual_observed - expected_nominal) / expected_nominal * 100
    print(f"--> Discrepancy from nominal (due to profile noise ~0.10): {diff_pct:.2f}%")
    results["demand_test"] = {
        "nominal": expected_nominal,
        "observed": actual_observed,
        "diff_pct": diff_pct,
    }

    # -------------------------------------------------------------
    # Experiment 3: Allocation Timing & Immediate Depot Deduction
    # -------------------------------------------------------------
    heading("3. Allocation Timing & Immediate Depot Deduction")
    reset_sim()
    depot_before = client.get("/v1/depots/depot-gazipur").json()
    diesel_before = depot_before["inventory"]["DIESEL"]
    print(f"Depot Gazipur Diesel before allocation: {diesel_before} L")

    alloc_payload = {
        "idempotency_key": "probe-exp3-001",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "route_id": "route-gazipur-mirpur",
        "fuel_type": "DIESEL",
        "quantity": 3000.0,
    }
    alloc_resp = client.post("/v1/allocations", json=alloc_payload)
    print("POST /v1/allocations status:", alloc_resp.status_code)
    alloc_data = alloc_resp.json()
    print("Allocation created:", json.dumps(alloc_data, indent=2))

    depot_after_create = client.get("/v1/depots/depot-gazipur").json()
    diesel_after_create = depot_after_create["inventory"]["DIESEL"]
    print(f"Depot Gazipur Diesel immediately after POST (PENDING): {diesel_after_create} L")
    depot_deducted_immediately = (diesel_before - diesel_after_create) == 3000.0
    print(f"--> Depot inventory deducted immediately on PENDING? {depot_deducted_immediately}")
    results["depot_deducted_immediately"] = depot_deducted_immediately

    # Step ticks to watch departure and arrival
    # route-gazipur-mirpur has transit_ticks = 2
    step_1 = step_sim()
    alloc_at_step1 = client.get("/v1/allocations").json()[0]
    print(
        f"After Step 1 (tick {step_1['tick']}): "
        f"status = {alloc_at_step1['status']}, "
        f"departure_tick = {alloc_at_step1['departure_tick']}"
    )

    step_2 = step_sim()
    alloc_at_step2 = client.get("/v1/allocations").json()[0]
    print(f"After Step 2 (tick {step_2['tick']}): alloc status = {alloc_at_step2['status']}")

    step_3 = step_sim()
    alloc_at_step3 = client.get("/v1/allocations").json()[0]
    print(
        f"After Step 3 (tick {step_3['tick']}): "
        f"status = {alloc_at_step3['status']}, "
        f"arrival_tick = {alloc_at_step3['actual_arrival_tick']}"
    )

    results["allocation_lifecycle"] = {
        "created_status": alloc_data["status"],
        "step1_status": alloc_at_step1["status"],
        "step1_departure_tick": alloc_at_step1["departure_tick"],
        "step2_status": alloc_at_step2["status"],
        "step3_status": alloc_at_step3["status"],
    }

    # -------------------------------------------------------------
    # Experiment 4: Destination Capacity Check & In-Transit Fuel
    # -------------------------------------------------------------
    heading("4. Destination Capacity Check & In-Transit Fuel")
    reset_sim()
    station = client.get("/v1/stations/station-mirpur").json()
    st_diesel = station["inventory"]["DIESEL"]
    st_cap = station["capacity"]["DIESEL"]
    free_space = st_cap - st_diesel
    print(f"Mirpur Diesel capacity: {st_cap}, inventory: {st_diesel}, free space: {free_space}")

    # Allocate 4000L (fits in free space 6000L)
    r1 = client.post(
        "/v1/allocations",
        json={
            "idempotency_key": "probe-exp4-001",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 4000.0,
        },
    )
    print(f"Alloc 1 (4000L) status: {r1.status_code}")

    # Now attempt to allocate another 3000L while alloc 1 is PENDING:
    # 4000 + 3000 = 7000L > free space 6000L!
    # Does simulator reject with DESTINATION_CAPACITY_EXCEEDED (409)?
    r2 = client.post(
        "/v1/allocations",
        json={
            "idempotency_key": "probe-exp4-002",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 3000.0,
        },
    )
    print(f"Alloc 2 (3000L while 4000L PENDING) status: {r2.status_code}")
    if r2.status_code == 409:
        print("Alloc 2 rejected with:", r2.json())
        print("--> RESULT: In-transit / pending fuel counts against destination capacity!")
        results["in_transit_counted_against_station_capacity"] = True
    else:
        print("Alloc 2 accepted! In-transit was NOT counted against station capacity check!")
        results["in_transit_counted_against_station_capacity"] = False

    # -------------------------------------------------------------
    # Experiment 5: Failure Semantics (Route Disruption at Departure)
    # -------------------------------------------------------------
    heading("5. Failure Semantics (Route Disruption & Depot Inventory)")
    reset_sim()
    depot_start = client.get("/v1/depots/depot-gazipur").json()["inventory"]["DIESEL"]
    # Create allocation at tick 0
    client.post(
        "/v1/allocations",
        json={
            "idempotency_key": "probe-exp5-001",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 3000.0,
        },
    )
    depot_after_pnd = client.get("/v1/depots/depot-gazipur").json()["inventory"]["DIESEL"]
    diff_deducted = depot_start - depot_after_pnd
    print(f"Depot Diesel after PENDING: {depot_after_pnd} (deducted by {diff_deducted})")

    # Inject route disruption event starting at tick 1 (departure time)
    client.post(
        "/admin/events",
        json={
            "type": "route_disruption",
            "start_tick": 1,
            "duration_ticks": 5,
            "parameters": {"route_ids": ["route-gazipur-mirpur"]},
        },
    )
    # Step to tick 1 where departure should fail
    step_sim()
    alloc_fail = client.get("/v1/allocations").json()[0]
    print(
        f"Alloc status at tick 1: {alloc_fail['status']}, "
        f"failure_reason: {alloc_fail.get('failure_reason')}"
    )
    depot_after_fail = client.get("/v1/depots/depot-gazipur").json()["inventory"]["DIESEL"]
    print(f"Depot Diesel after FAILED: {depot_after_fail} (Original: {depot_start})")
    refunded_on_fail = depot_after_fail == depot_start
    print(f"--> Was depot refunded on FAILED? {refunded_on_fail}")
    results["depot_refunded_on_failed"] = refunded_on_fail

    # -------------------------------------------------------------
    # Experiment 6: Cancellation & Idempotency Key Semantics
    # -------------------------------------------------------------
    heading("6. Cancellation & Idempotency Key Semantics")
    reset_sim()
    depot_0 = client.get("/v1/depots/depot-gazipur").json()["inventory"]["DIESEL"]
    r_al = client.post(
        "/v1/allocations",
        json={
            "idempotency_key": "probe-exp6-key1",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 2500.0,
        },
    )
    al_id = r_al.json()["id"]
    depot_pnd = client.get("/v1/depots/depot-gazipur").json()["inventory"]["DIESEL"]
    print(f"Depot after create: {depot_pnd} (down by {depot_0 - depot_pnd})")

    # Idempotent replay: same key, same body
    r_replay = client.post(
        "/v1/allocations",
        json={
            "idempotency_key": "probe-exp6-key1",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 2500.0,
        },
    )
    print(f"Idempotent replay HTTP status: {r_replay.status_code}")
    results["idempotent_replay_status"] = r_replay.status_code

    # Mismatch key replay: same key, different body
    r_mismatch = client.post(
        "/v1/allocations",
        json={
            "idempotency_key": "probe-exp6-key1",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
        },
    )
    print(f"Mismatch key HTTP status: {r_mismatch.status_code}, detail: {r_mismatch.json()}")
    results["idempotency_mismatch_status"] = r_mismatch.status_code

    # Cancel the allocation
    r_cancel = client.post(f"/v1/allocations/{al_id}/cancel")
    print(f"Cancel allocation status: {r_cancel.status_code}, data: {r_cancel.json()}")
    depot_after_cancel = client.get("/v1/depots/depot-gazipur").json()["inventory"]["DIESEL"]
    print(f"Depot inventory after cancel: {depot_after_cancel} (Original: {depot_0})")
    results["cancel_refunds_depot"] = depot_after_cancel == depot_0

    # Can we reuse the idempotency key after cancellation?
    r_reuse_after_cancel = client.post(
        "/v1/allocations",
        json={
            "idempotency_key": "probe-exp6-key1",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 2500.0,
        },
    )
    print(f"Post with key after cancellation HTTP status: {r_reuse_after_cancel.status_code}")
    print(f"Response: {r_reuse_after_cancel.text}")
    status_ok = r_reuse_after_cancel.status_code in [200, 201]
    is_pending = r_reuse_after_cancel.json().get("status") == "PENDING"
    results["key_reusable_after_cancel"] = status_ok and is_pending

    # -------------------------------------------------------------
    # Experiment 7: Determinism Verification
    # -------------------------------------------------------------
    heading("7. Determinism Across Resets")
    # Run A: reset, step 5 ticks, collect state
    reset_sim()
    for _ in range(5):
        step_sim()
    state_a = {
        "instance": client.get("/v1/instance").json(),
        "depots": client.get("/v1/depots").json(),
        "stations": client.get("/v1/stations").json(),
        "demand": client.get("/v1/demand-history?limit=20").json(),
        "metrics": client.get("/v1/metrics").json(),
    }

    # Run B: reset, step 5 ticks, collect state
    reset_sim()
    for _ in range(5):
        step_sim()
    state_b = {
        "instance": client.get("/v1/instance").json(),
        "depots": client.get("/v1/depots").json(),
        "stations": client.get("/v1/stations").json(),
        "demand": client.get("/v1/demand-history?limit=20").json(),
        "metrics": client.get("/v1/metrics").json(),
    }

    json_a = json.dumps(state_a, sort_keys=True)
    json_b = json.dumps(state_b, sort_keys=True)
    is_identical = json_a == json_b
    print(f"--> Are 5 ticks after fresh resets byte-identical? {is_identical}")
    results["is_deterministic"] = is_identical

    heading("DISCOVERY COMPLETE - SUMMARY")
    print(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    run_discovery()
