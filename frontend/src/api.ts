import type {
  AuditEvent,
  AuditStats,
  ForecastResponse,
  HealthResponse,
  ParsedMetrics,
  PlanResponse,
  RecommendationDetailResponse,
  RecommendationListResponse,
  RiskResponse,
  StateResponse,
  StationForecastResponse,
  StationRiskResponse,
  SyncResponse,
  TwinSimulationResponse,
} from './types';

export function getDefaultBackendUrl(): string {
  const saved = localStorage.getItem('fuel_backend_url');
  if (saved) return saved;
  return import.meta.env.VITE_BACKEND_API_URL || 'http://127.0.0.1:8001';
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 8000);
  try {
    const res = await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers ?? {}),
      },
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (typeof body?.detail === 'string') msg = body.detail;
        else if (body?.detail?.message) msg = body.detail.message;
        else if (body?.detail?.error) msg = body.detail.error;
      } catch {
        // ignore JSON parse failures on error responses
      }
      throw new Error(msg);
    }
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

function extractLabel(metricPart: string, key: string): string | null {
  const re = new RegExp(`${key}="([^"]*)"`);
  const match = metricPart.match(re);
  return match ? match[1] : null;
}

export function parsePrometheusText(text: string): ParsedMetrics {
  const summary: ParsedMetrics = {
    http_requests_total: 0,
    http_requests_by_status: {},
    simulator_requests_by_outcome: {},
    simulator_errors_total: 0,
    simulator_errors_by_type: {},
    fallback_activations_total: 0,
    fallback_by_reason: {},
    alerts_by_severity: {},
    recommendations_by_outcome: {},
    forecast_mae_liters: 0,
    forecast_mape_percent: 0,
    circuit_breaker_state: 0,
    operational_mode_code: 0,
    raw_text: text,
  };

  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const idx = line.lastIndexOf(' ');
    if (idx === -1) continue;
    const metricPart = line.slice(0, idx);
    const val = Number(line.slice(idx + 1));
    if (!Number.isFinite(val)) continue;

    if (metricPart.startsWith('fuel_http_requests_total')) {
      summary.http_requests_total += val;
      const sc = extractLabel(metricPart, 'status_code') ?? 'unknown';
      const bucket = `${sc[0]}xx`;
      summary.http_requests_by_status[bucket] =
        (summary.http_requests_by_status[bucket] ?? 0) + val;
    } else if (metricPart.startsWith('fuel_simulator_requests_total')) {
      const outcome = extractLabel(metricPart, 'outcome') ?? 'unknown';
      summary.simulator_requests_by_outcome[outcome] =
        (summary.simulator_requests_by_outcome[outcome] ?? 0) + val;
    } else if (metricPart.startsWith('fuel_simulator_errors_total')) {
      summary.simulator_errors_total += val;
      const errType = extractLabel(metricPart, 'error_type') ?? 'unknown';
      summary.simulator_errors_by_type[errType] =
        (summary.simulator_errors_by_type[errType] ?? 0) + val;
    } else if (metricPart.startsWith('fuel_fallback_activations_total')) {
      summary.fallback_activations_total += val;
      const reason = extractLabel(metricPart, 'reason') ?? 'unknown';
      summary.fallback_by_reason[reason] = (summary.fallback_by_reason[reason] ?? 0) + val;
    } else if (metricPart.startsWith('fuel_alerts_raised_total')) {
      const sev = extractLabel(metricPart, 'severity') ?? 'UNKNOWN';
      summary.alerts_by_severity[sev] = (summary.alerts_by_severity[sev] ?? 0) + val;
    } else if (metricPart.startsWith('fuel_recommendations_total')) {
      const outcome = extractLabel(metricPart, 'outcome') ?? 'UNKNOWN';
      summary.recommendations_by_outcome[outcome] =
        (summary.recommendations_by_outcome[outcome] ?? 0) + val;
    } else if (metricPart.startsWith('fuel_forecast_mae_liters')) {
      summary.forecast_mae_liters = val;
    } else if (metricPart.startsWith('fuel_forecast_mape_percent')) {
      summary.forecast_mape_percent = val;
    } else if (metricPart.startsWith('fuel_circuit_breaker_state')) {
      summary.circuit_breaker_state = val;
    } else if (metricPart.startsWith('fuel_operational_mode ')) {
      summary.operational_mode_code = val;
    }
  }
  return summary;
}

export class FuelApiClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }

  getHealth() {
    return requestJson<HealthResponse>(`${this.baseUrl}/health`);
  }

  getState() {
    return requestJson<StateResponse>(`${this.baseUrl}/state`);
  }

  triggerSync() {
    return requestJson<SyncResponse>(`${this.baseUrl}/sync`, { method: 'POST' });
  }

  getForecasts() {
    return requestJson<ForecastResponse>(`${this.baseUrl}/forecasts`);
  }

  getStationForecast(stationId: string) {
    return requestJson<StationForecastResponse>(
      `${this.baseUrl}/forecasts/${encodeURIComponent(stationId)}`,
    );
  }

  getRisks() {
    return requestJson<RiskResponse>(`${this.baseUrl}/risks`);
  }

  getStationRisk(stationId: string) {
    return requestJson<StationRiskResponse>(
      `${this.baseUrl}/risks/${encodeURIComponent(stationId)}`,
    );
  }

  getPlan() {
    return requestJson<PlanResponse>(`${this.baseUrl}/plan`);
  }

  simulateTwin(payload: {
    station_id: string;
    fuel_type: string;
    candidate_shipment_quantity: number;
    candidate_arrival_tick?: number | null;
    trials?: number;
    seed?: number;
  }) {
    return requestJson<TwinSimulationResponse>(`${this.baseUrl}/twin/simulate`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  getStationFuelTwin(stationId: string, fuelType: string, trials?: number, seed?: number) {
    const params = new URLSearchParams();
    if (trials !== undefined) params.set('trials', String(trials));
    if (seed !== undefined) params.set('seed', String(seed));
    const qs = params.toString() ? `?${params.toString()}` : '';
    return requestJson<TwinSimulationResponse>(
      `${this.baseUrl}/twin/${encodeURIComponent(stationId)}/${encodeURIComponent(fuelType)}${qs}`,
    );
  }

  getRecommendations(opts?: { status?: string; stationId?: string; limit?: number }) {
    const params = new URLSearchParams();
    if (opts?.status) params.set('status', opts.status);
    if (opts?.stationId) params.set('station_id', opts.stationId);
    if (opts?.limit) params.set('limit', String(opts.limit));
    const qs = params.toString() ? `?${params.toString()}` : '';
    return requestJson<RecommendationListResponse>(`${this.baseUrl}/recommendations${qs}`);
  }

  generateRecommendations() {
    return requestJson<RecommendationListResponse>(`${this.baseUrl}/recommendations/generate`, {
      method: 'POST',
    });
  }

  getRecommendationById(id: string) {
    return requestJson<RecommendationDetailResponse>(
      `${this.baseUrl}/recommendations/${encodeURIComponent(id)}`,
    );
  }

  approveRecommendation(id: string, operatorNotes?: string) {
    return requestJson<RecommendationDetailResponse>(
      `${this.baseUrl}/recommendations/${encodeURIComponent(id)}/approve`,
      {
        method: 'POST',
        body: JSON.stringify(operatorNotes ? { operator_notes: operatorNotes } : {}),
      },
    );
  }

  rejectRecommendation(id: string, operatorNotes?: string) {
    return requestJson<RecommendationDetailResponse>(
      `${this.baseUrl}/recommendations/${encodeURIComponent(id)}/reject`,
      {
        method: 'POST',
        body: JSON.stringify(operatorNotes ? { operator_notes: operatorNotes } : {}),
      },
    );
  }

  getAuditHistory(opts?: { recommendationId?: string; limit?: number }) {
    const params = new URLSearchParams();
    if (opts?.recommendationId) params.set('recommendation_id', opts.recommendationId);
    params.set('limit', String(opts?.limit ?? 100));
    return requestJson<{ total_events: number; events: AuditEvent[]; evaluated_at: string }>(
      `${this.baseUrl}/decisions/audit?${params.toString()}`,
    );
  }

  getAuditStats() {
    return requestJson<{ stats: AuditStats; evaluated_at: string }>(
      `${this.baseUrl}/decisions/stats`,
    );
  }

  async getMetrics(): Promise<ParsedMetrics> {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const res = await fetch(`${this.baseUrl}/metrics`, { signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return parsePrometheusText(await res.text());
    } finally {
      window.clearTimeout(timeout);
    }
  }
}
