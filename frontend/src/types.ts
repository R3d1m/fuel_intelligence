export type OperationalMode = 'NORMAL' | 'DEGRADED_CACHED' | 'DEGRADED_FALLBACK' | string;
export type Severity = 'CRITICAL' | 'WATCH' | 'OK' | string;
export type FuelType = 'DIESEL' | 'PETROL' | 'OCTANE';
export type RecommendationStatus = 'PROPOSED' | 'APPROVED' | 'REJECTED' | 'SUBMITTED' | 'FAILED';

export interface FuelMap {
  DIESEL: number;
  PETROL: number;
  OCTANE: number;
  [key: string]: number;
}

export interface HealthResponse {
  status: string;
  mode: OperationalMode;
  reason: string;
  simulator: {
    connected: boolean;
    circuit_state: 'CLOSED' | 'HALF_OPEN' | 'OPEN' | string;
    consecutive_failures: number;
    cooldown_remaining: number;
  };
  store: {
    has_snapshot: boolean;
    is_stale: boolean;
    data_age_seconds: number | null;
    total_updates: number;
    failed_polls: number;
  };
  poller: {
    is_running: boolean;
    poll_interval_seconds: number;
  };
  alerts: string[];
  timestamp: string;
}

export interface InstanceInfo {
  id: number;
  scenario_id: string;
  scenario_version: string;
  seed: number;
  sim_time: string;
  tick: number;
  tick_minutes: number;
  status: string;
}

export interface DepotSnapshot {
  id: string;
  name: string;
  region_id: string;
  status: 'OPEN' | 'CONSTRAINED' | 'CLOSED' | string;
  dispatch_capacity_per_tick: number;
  capacity: FuelMap;
  inventory: FuelMap;
}

export interface StationSnapshot {
  id: string;
  name: string;
  region_id: string;
  status: 'OPEN' | 'CONSTRAINED' | 'CLOSED' | 'OUTAGE' | string;
  demand_profile: string;
  demand_multiplier: number;
  capacity: FuelMap;
  inventory: FuelMap;
}

export interface RouteSnapshot {
  id: string;
  source_depot_id: string;
  destination_station_id: string;
  transit_ticks: number;
  max_shipment: number;
  status: 'AVAILABLE' | 'CONGESTED' | 'DISRUPTED' | string;
}

export interface SupplyArrival {
  id: string;
  depot_id: string;
  fuel_type: FuelType | string;
  quantity: number;
  planned_tick: number;
  actual_tick: number | null;
  status: 'SCHEDULED' | 'ARRIVED' | 'DELAYED' | 'CANCELLED' | string;
}

export interface SimEvent {
  id: number;
  type: string;
  start_tick: number;
  end_tick: number | null;
  status: string;
  parameters: Record<string, unknown>;
}

export interface AllocationSnapshot {
  id: number;
  idempotency_key: string;
  source_depot_id: string;
  destination_station_id: string;
  route_id: string;
  fuel_type: FuelType | string;
  quantity: number;
  created_tick: number;
  departure_tick: number | null;
  expected_arrival_tick: number | null;
  actual_arrival_tick: number | null;
  status: 'PENDING' | 'IN_TRANSIT' | 'ARRIVED' | 'FAILED' | 'CANCELLED' | string;
  failure_reason: string | null;
}

export interface SimulatorSnapshot {
  instance: InstanceInfo;
  depots: DepotSnapshot[];
  stations: StationSnapshot[];
  routes: RouteSnapshot[];
  supply_arrivals: SupplyArrival[];
  events: SimEvent[];
  allocations: AllocationSnapshot[];
  tick: number;
  sim_time: string;
  fetched_at: string;
  is_stale: boolean;
}

export interface StateResponse {
  mode: OperationalMode;
  reason: string;
  data_age_seconds: number | null;
  is_stale: boolean;
  circuit_state: string;
  snapshot: SimulatorSnapshot | null;
  alerts: string[];
  evaluated_at: string;
}

export interface SyncResponse {
  success: boolean;
  tick?: number | null;
  is_stale?: boolean | null;
  message: string;
  details?: Record<string, unknown> | null;
}

export interface ForecastPoint {
  tick: number;
  sim_time?: string | null;
  forecast_liters_per_tick: number;
  forecast_liters_per_hour: number;
  lower_bound_liters: number;
  upper_bound_liters: number;
}

export interface ForecastMetrics {
  mae: number;
  mape: number;
  rmse: number;
  residual_std: number;
  sample_count: number;
}

export interface AnomalyReport {
  is_spike: boolean;
  severity: Severity;
  multiplier_spike: boolean;
  statistical_spike: boolean;
  demand_multiplier: number;
  recent_demand_mean: number;
  baseline_demand_mean: number;
  spike_ratio: number;
  explanation: string;
}

export interface StationFuelForecast {
  station_id: string;
  fuel_type: FuelType | string;
  forecast_per_tick: number;
  forecast_per_hour: number;
  horizon_ticks: number;
  model_used: string;
  trajectory: ForecastPoint[];
  metrics: ForecastMetrics;
  anomaly: AnomalyReport;
  computed_at: string;
}

export interface ForecastResponse {
  mode: OperationalMode;
  reason: string;
  total_fleet_demand_per_hour: number;
  average_mae: number;
  average_mape: number;
  active_anomalies_count: number;
  active_anomalies: AnomalyReport[];
  forecasts: Record<string, Record<string, StationFuelForecast>>;
  evaluated_at: string;
}

export interface StationForecastResponse {
  mode: OperationalMode;
  station_id: string;
  forecasts: Record<string, StationFuelForecast>;
  anomalies: AnomalyReport[];
  evaluated_at: string;
}

export interface InventoryTrajectoryPoint {
  step: number;
  tick: number;
  sim_time?: string | null;
  projected_inventory: number;
  fill_ratio: number;
  incoming_liters: number;
  forecast_demand_liters: number;
  unmet_demand_liters: number;
  projected_overflow_liters: number;
  is_stockout: boolean;
}

export interface RiskDrivers {
  initial_inventory_liters: number;
  tank_capacity_liters: number;
  initial_fill_ratio: number;
  is_low_inventory: boolean;
  is_demand_spike: boolean;
  demand_rate_liters_per_hour: number;
  in_transit_shipments_count: number;
  in_transit_liters_total: number;
  pending_shipments_count: number;
  pending_liters_total: number;
  available_supply_routes_count: number;
  total_supply_routes_count: number;
  all_supply_routes_disrupted: boolean;
  min_transit_ticks: number | null;
  forecast_mape: number;
  data_age_seconds: number;
  is_stale_data: boolean;
  summary_reasons: string[];
}

export interface RiskAlertItem {
  station_id: string;
  station_name: string;
  fuel_type: FuelType | string;
  severity: Severity;
  hours_to_stockout: number | null;
  first_stockout_tick: number | null;
  current_fill_ratio: number;
  urgency_score: number;
  headline: string;
  explanation: string;
  created_tick: number;
}

export interface StationFuelRisk {
  station_id: string;
  station_name: string;
  fuel_type: FuelType | string;
  severity: Severity;
  confidence: number;
  confidence_level: string;
  needs_human_review: boolean;
  current_inventory_liters: number;
  capacity_liters: number;
  current_fill_ratio: number;
  hours_to_stockout: number | null;
  first_stockout_tick: number | null;
  ticks_to_stockout: number | null;
  is_unavoidable_stockout: boolean;
  unavoidable_shortage_hours: number | null;
  total_unmet_liters: number;
  min_projected_inventory: number;
  min_projected_fill_ratio: number;
  burn_rate_liters_per_hour: number;
  runout_hours_no_arrivals: number | null;
  drivers: RiskDrivers;
  trajectory: InventoryTrajectoryPoint[];
  explanation: string;
  evaluated_at: string;
}

export interface DepotFuelRisk {
  depot_id: string;
  depot_name: string;
  fuel_type: FuelType | string;
  severity: Severity;
  current_inventory_liters: number;
  capacity_liters: number;
  current_fill_ratio: number;
  pending_dispatch_liters: number;
  scheduled_replenishment_liters: number;
  effective_available_liters: number;
  explanation: string;
}

export interface RiskResponse {
  mode: OperationalMode;
  reason: string;
  overall_severity: Severity;
  critical_count: number;
  watch_count: number;
  ok_count: number;
  total_projected_unmet_liters: number;
  earliest_stockout_station_id: string | null;
  earliest_stockout_fuel: string | null;
  earliest_stockout_hours: number | null;
  fleet_average_confidence: number;
  active_alerts_count: number;
  active_alerts: RiskAlertItem[];
  station_risks: Record<string, Record<string, StationFuelRisk>>;
  depot_risks: Record<string, Record<string, DepotFuelRisk>>;
  evaluated_at: string;
}

export interface StationRiskResponse {
  mode: OperationalMode;
  reason: string;
  station_id: string;
  station_name: string;
  highest_severity: Severity;
  risks: Record<string, StationFuelRisk>;
  alerts: RiskAlertItem[];
  evaluated_at: string;
}

export interface AppliedConstraints {
  route_max_shipment: number;
  depot_available_inventory: number;
  depot_remaining_dispatch_capacity: number;
  station_effective_free_space: number;
  amount_needed: number;
  bottleneck_constraint: string;
  allocated_quantity: number;
}

export interface PlanActionAlternative {
  route_id: string;
  source_depot_id: string;
  source_depot_name: string;
  quantity_liters: number;
  departure_tick: number;
  arrival_tick: number;
  transit_ticks: number;
  is_timely: boolean;
  mitigation_type: string;
  bottleneck_constraint: string;
  score: number;
  reason_not_chosen: string;
}

export interface CandidateImpact {
  expected_unmet_reduction_liters: number;
  hours_of_cover_added: number;
  stockout_probability_before: number;
  stockout_probability_after: number;
  score: number;
  scoring_details: Record<string, number>;
}

export interface PlanAction {
  action_id: string;
  station_id: string;
  station_name: string;
  fuel_type: FuelType | string;
  urgency_score: number;
  severity: Severity;
  route_id: string;
  source_depot_id: string;
  source_depot_name: string;
  quantity_liters: number;
  departure_tick: number;
  arrival_tick: number;
  transit_ticks: number;
  is_timely: boolean;
  mitigation_type: string;
  unavoidable_shortage_hours: number | null;
  constraints: AppliedConstraints;
  impact: CandidateImpact;
  alternatives: PlanActionAlternative[];
  explanation: string;
  confidence_score: number;
  confidence_level: string;
  needs_human_review: boolean;
  is_fallback: boolean;
}

export interface UnmetTarget {
  station_id: string;
  station_name: string;
  fuel_type: FuelType | string;
  severity: Severity;
  urgency_score: number;
  hours_to_stockout: number | null;
  reason_code: string;
  explanation: string;
}

export interface SharedCapacitySummary {
  depot_dispatch_utilization: Record<string, Record<string, number>>;
  depot_inventory_utilization: Record<string, Record<string, Record<string, number>>>;
  station_ullage_utilization: Record<string, Record<string, Record<string, number>>>;
}

export interface PlanResponse {
  mode: OperationalMode;
  reason: string;
  plan_id: string;
  created_tick: number;
  total_actions: number;
  total_liters_dispatched: number;
  actions: PlanAction[];
  unmet_targets: UnmetTarget[];
  shared_capacity_summary: SharedCapacitySummary;
  summary: string;
  evaluated_at: string;
}

export interface ConfidenceInterval {
  ci_percent: number;
  lower: number;
  median: number;
  upper: number;
}

export interface ScenarioSimulation {
  label: string;
  has_shipment: boolean;
  shipment_arrival_tick: number | null;
  shipment_quantity_liters: number;
  stockout_probability: number;
  expected_unmet_liters: number;
  unmet_ci: ConfidenceInterval;
  min_inventory_ci: ConfidenceInterval;
  median_trajectory: number[];
  p10_trajectory: number[];
  p90_trajectory: number[];
}

export interface TwinSimulationResult {
  station_id: string;
  station_name: string;
  fuel_type: FuelType | string;
  current_tick: number;
  horizon_ticks: number;
  trials_count: number;
  random_seed: number;
  residual_std: number;
  baseline: ScenarioSimulation;
  with_shipment: ScenarioSimulation;
  stockout_probability_reduction: number;
  expected_unmet_reduction_liters: number;
  relative_risk_reduction_percent: number;
}

export interface TwinSimulationResponse {
  mode: OperationalMode;
  reason: string;
  simulation: TwinSimulationResult;
  evaluated_at: string;
}

export interface RecommendationItem {
  recommendation_id: string;
  plan_id: string | null;
  action_id: string | null;
  created_tick: number;
  created_at: string;
  updated_at: string;
  station_id: string;
  station_name: string;
  fuel_type: FuelType | string;
  quantity_liters: number;
  source_depot_id: string;
  source_depot_name: string;
  route_id: string;
  departure_tick: number;
  expected_arrival_tick: number;
  transit_ticks: number;
  urgency_score: number;
  severity: Severity;
  mitigation_type: string;
  unavoidable_shortage_hours: number | null;
  explanation: string;
  signals: Record<string, unknown>;
  constraints: AppliedConstraints;
  expected_unmet_reduction_liters: number;
  hours_of_cover_added: number;
  stockout_probability_before: number;
  stockout_probability_after: number;
  impact_summary: string;
  confidence_score: number;
  confidence_level: string;
  needs_human_review: boolean;
  alternatives: PlanActionAlternative[];
  idempotency_key: string;
  status: RecommendationStatus | string;
  operator_notes: string | null;
  approved_at: string | null;
  rejected_at: string | null;
  submitted_at: string | null;
  simulator_allocation_id: number | null;
  failure_code: string | null;
  failure_reason: string | null;
}

export interface RecommendationListResponse {
  mode: OperationalMode;
  reason: string;
  total_count: number;
  recommendations: RecommendationItem[];
  evaluated_at: string;
}

export interface RecommendationDetailResponse {
  recommendation: RecommendationItem;
  evaluated_at: string;
}

export interface AuditEvent {
  id: number;
  recommendation_id: string;
  event_type: string;
  from_status: string | null;
  to_status: string;
  operator_notes: string | null;
  details: Record<string, unknown>;
  timestamp: string;
}

export interface AuditStats {
  total_recommendations: number;
  total_liters_recommended: number;
  total_audit_events: number;
  status_breakdown: Record<string, { count: number; total_liters: number }>;
  submitted_count: number;
  rejected_count: number;
  failed_count: number;
  rejection_rate: number;
}

export interface ParsedMetrics {
  http_requests_total: number;
  http_requests_by_status: Record<string, number>;
  simulator_requests_by_outcome: Record<string, number>;
  simulator_errors_total: number;
  simulator_errors_by_type: Record<string, number>;
  fallback_activations_total: number;
  fallback_by_reason: Record<string, number>;
  alerts_by_severity: Record<string, number>;
  recommendations_by_outcome: Record<string, number>;
  forecast_mae_liters: number;
  forecast_mape_percent: number;
  circuit_breaker_state: number;
  operational_mode_code: number;
  raw_text: string;
}
