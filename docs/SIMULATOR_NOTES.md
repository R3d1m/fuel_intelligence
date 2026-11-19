# Simulator Discovery & Empirical Behavioral Notes

_Documented experimental findings from probing the Fuel Supply Simulator (`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`)._

## Key Empirical Findings

### 1. Instance Configuration & Tick Granularity

- **Observed `tick_minutes`:** [Pending Block 2 discovery via GET /v1/instance]
- **Default Speed:** [Pending Block 2 verification]
- **Takeaway:** Never hard-code tick minutes; dynamically parse `tick_minutes` from `/v1/instance` into all rate calculations.

### 2. Demand Profile Verification

- **Mirpur Diesel Profile Calculation:**
  - Base Daily: 8,500 L/day
  - Target per tick: [Pending Block 2 comparison with /v1/demand-history]
- **Hour-of-Day Multipliers:** [Pending verification]

### 3. Allocation Lifecycle & Inventory Deductions

- **Immediate Depot Inventory Effect:** [Pending Block 2 test]
- **Departure Timing:** [Pending Block 2 test]
- **Destination Capacity & In-Transit Fuel:** [Pending Block 2 test: Does in-transit fuel count against station capacity check?]

### 4. Failure and Cancellation Semantics

- **Depot Inventory on Route Disruption (`FAILED`):** [Pending Block 2 test]
- **Cancellation Refund:** [Pending Block 2 test on PENDING allocations]
- **Idempotency Key Reuse & Replay Status Code:** [Pending Block 2 test: 200 vs 201 behavior]

### 5. Determinism Verification

- **Reset & Step Comparison:** [Pending Block 2 byte/value comparison after two clean resets]
