# Simulator Discovery & Empirical Behavioral Notes

*Verified empirical findings from probing the BUP Fuel Supply Simulator (`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`) running locally at `http://localhost:8000`.*

---

## 1. Instance Configuration & Tick Granularity

* **Observed `tick_minutes`:** **`15`** (from `GET /v1/instance`).
* **Discrepancy Warning:** Although `docker-compose.yml` specifies `TICK_MINUTES: ${TICK_MINUTES:-8}`, the baseline scenario baked into the container overrides or initializes with `tick_minutes = 15`.
* **Engineering Rule:** **NEVER hard-code `tick_minutes` anywhere in the platform.** Always dynamically read `tick_minutes` from `/v1/instance` and pass it to rate calculations, forecasters, and arrival time estimators.

---

## 2. Demand Profile Verification & Noise Mechanics

* **Baseline Formula:**
  $$\text{Ticks Per Day} = \frac{1440}{\text{tick\_minutes}} = \frac{1440}{15} = 96 \text{ ticks/day}$$
  $$\text{Base Nominal Per Tick} = \frac{\text{Daily Baseline}}{\text{Ticks Per Day}}$$

* **Empirical Verification (Mirpur Diesel at Tick 0):**
  * Station: `station-mirpur`, Fuel: `DIESEL`, Profile: `urban_high`.
  * Daily Baseline: 8,500 L/day $\rightarrow$ Base per tick: $8500 / 96 = 88.5417\text{ L}$.
  * Time: `00:00:00` (off-peak hours for `urban_high`, factor = $0.70$).
  * Expected Nominal: $88.5417 \times 0.70 = 61.979\text{ L}$.
  * Observed Value in `/v1/demand-history`: **$59.588\text{ L}$**.
  * Residual: $\approx -3.86\%$, matching the configured `urban_high` noise coefficient ($\pm 10\%$).
* **Takeaway:** Demand math strictly adheres to the stated profile equations with Gaussian/uniform jitter around the baseline.

---

## 3. Allocation Lifecycle & Depot Inventory Mechanics

* **Immediate Depot Inventory Effect:**
  * When `POST /v1/allocations` succeeds, the allocation is created with `status = "PENDING"`.
  * **Depot inventory is deducted IMMEDIATELY upon creation**, before departure! (e.g. Gazipur Diesel dropped from 60,000 L to 57,000 L while `PENDING`).
* **Departure Timing:**
  * Created at tick $T$ with `departure_tick: null`.
  * At tick $T+1$ (on the next tick advance), status transitions to `IN_TRANSIT` with `departure_tick: T`.
* **Arrival Timing:**
  * For `route-gazipur-mirpur` (`transit_ticks: 2`), after tick $T+1$ and $T+2$ in transit, status transitions to `ARRIVED` with `actual_arrival_tick: T+2`.

---

## 4. Destination Capacity vs. In-Transit Fuel (Critical Finding)

* **Experimental Setup:**
  * Mirpur Diesel: Capacity = 15,000 L, Current Inventory = 9,000 L $\rightarrow$ Free space = 6,000 L.
  * Dispatched Allocation 1: 4,000 L (`PENDING`).
  * Immediately dispatched Allocation 2: 3,000 L (`PENDING`).
  * Combined in-transit: $4,000 + 3,000 = 7,000\text{ L} > 6,000\text{ L}$ free space.
* **Observed Simulator Behavior:**
  * Allocation 2 was **ACCEPTED (HTTP 201)**!
  * The simulator only checks: $\text{station.inventory}[\text{fuel}] + \text{quantity} \le \text{station.capacity}[\text{fuel}]$.
  * **The simulator DOES NOT count pending or in-transit shipments against destination capacity!**
* **Architectural Consequence:**
  * If our planner relies solely on the simulator's checks, it will over-allocate fuel and cause station overflow when shipments arrive!
  * **Our planner MUST locally calculate effective tank ullage:**
    $$\text{Effective Free Space} = \text{Capacity} - (\text{Current Inventory} + \sum \text{Pending/In-Transit Shipments})$$

---

## 5. Failure Semantics (Route Disruption & Lost Fuel)

* **Experimental Setup:**
  * Allocation created at tick 0 (`PENDING`, 3,000 L deducted from depot).
  * Disrupted `route-gazipur-mirpur` at tick 0 before departure.
  * Advanced tick to 1.
* **Observed Simulator Behavior:**
  * Allocation status transitioned to **`FAILED`** with `failure_reason: "ROUTE_UNAVAILABLE"`.
  * Depot inventory remained at **57,000 L** (the 3,000 L was **NOT refunded**).
  * Calling `POST /v1/allocations/{id}/cancel` on the `FAILED` allocation rejected with **409 `CANNOT_CANCEL: Only pending allocations can be cancelled`**.
* **Critical Takeaway:**
  * **Failed allocations result in PERMANENT FUEL LOSS from the depot.** The simulator does not refund fuel on departure route failures.
  * Our planner must proactively check route status and active disruption events before allocating.

---

## 6. Cancellation & Idempotency Key Semantics

* **Cancellation (`POST /v1/allocations/{id}/cancel`):**
  * Only valid when `status == "PENDING"`.
  * Returns **HTTP 200 OK** with updated allocation (`status: "CANCELLED"`).
  * **Depot inventory is immediately refunded** (returned from 57,500 L to 60,000 L).
* **Idempotency Replay (Same Key, Same Payload):**
  * Returns **HTTP 201 Created** (not 200) with the existing allocation record.
* **Idempotency Mismatch (Same Key, Altered Payload):**
  * Returns **HTTP 409 Conflict** with:
    `{"detail": {"code": "IDEMPOTENCY_KEY_MISMATCH", "message": "Idempotency key was already used with different parameters."}}`
* **Key Reuse After Cancellation:**
  * Replaying a cancelled key with the original payload returns **HTTP 201** with `status: "CANCELLED"`.
  * **A cancelled key CANNOT be reused to create a new shipment.** The key remains permanently occupied.

---

## 7. Error Shapes Cheat Sheet

The simulator returns errors in three distinct formats that our adapter must handle:

1. **Domain Business Errors (409 Conflict / 404 Not Found):**
   ```json
   {
     "detail": {
       "code": "ROUTE_DISRUPTED",
       "message": "Route route-gazipur-mirpur is not available"
     }
   }
   ```
2. **Injected Chaos Faults (503 Service Unavailable):**
   ```json
   {
     "error": {
       "code": "FAULT_INJECTED",
       "message": "Simulator API temporarily unavailable."
     }
   }
   ```
3. **Pydantic / Request Validation Errors (422 Unprocessable Entity):**
   ```json
   {
     "detail": [
       {
         "type": "missing",
         "loc": ["body", "source_depot_id"],
         "msg": "Field required"
       }
     ]
   }
   ```
4. **Stale Data Signal:**
   * When `stale_data` fault is injected, HTTP GET responses include:
     `X-Simulator-Stale: true`

---

## 8. Determinism Verification

* **Experimental Setup:**
  * Ran 5 deterministic ticks (`POST /admin/step` $\times 5$) after a fresh `/admin/reset`.
  * Captured snapshot (instance, depots, stations, demand history, metrics).
  * Ran `/admin/reset` again and stepped 5 ticks.
* **Observed Result:**
  * Both snapshots are **100% byte-identical** (`is_deterministic: True`).
  * Replays using `/admin/step` and fixed seeds guarantee completely reproducible evaluation benchmarks.
