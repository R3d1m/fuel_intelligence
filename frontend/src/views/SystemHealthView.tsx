import { useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import {
  breakerTone,
  EmptyNotice,
  formatDecimal,
  formatInt,
  formatLabel,
  formatTime,
  Panel,
  StatusPill,
  statusTone,
} from '../components/ui';
import type { HealthResponse, ParsedMetrics } from '../types';

interface SystemHealthViewProps {
  health: HealthResponse | null;
  metrics: ParsedMetrics | null;
}

function MetricGroup({
  title,
  entries,
}: {
  title: string;
  entries: Record<string, number>;
}) {
  const list = Object.entries(entries);
  return (
    <div className="rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3">
      <div className="mb-2 text-xs font-semibold text-[#0F172A]">{title}</div>
      {list.length === 0 ? (
        <div className="text-xs text-[#526071]">No samples recorded yet</div>
      ) : (
        <dl className="space-y-1 text-xs">
          {list.map(([k, v]) => (
            <div key={k} className="flex items-center justify-between gap-2">
              <dt className="text-[#526071]">{k}</dt>
              <dd className="font-mono-num font-semibold text-[#0F172A]">{formatInt(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

export function SystemHealthView({ health, metrics }: SystemHealthViewProps) {
  const [showRaw, setShowRaw] = useState(false);

  if (!health) {
    return (
      <EmptyNotice
        title="Backend API unreachable"
        hint="Start the FastAPI backend on port 8001 or update the API endpoint in Settings."
      />
    );
  }

  const sim = health.simulator;
  const store = health.store;
  const poller = health.poller;
  const alerts = Array.isArray(health.alerts) ? health.alerts : [];

  return (
    <div className="space-y-5">
      <Panel
        title="Operational Mode & Resilience State"
        subtitle={health.reason || 'All dependencies within normal latency and freshness tolerances'}
        actions={
          <div className="flex items-center gap-2">
            <StatusPill tone={statusTone(health.mode)}>{formatLabel(health.mode)}</StatusPill>
            <span className="font-mono-num text-xs text-[#526071]">
              {formatTime(health.timestamp)}
            </span>
          </div>
        }
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {(['NORMAL', 'DEGRADED_CACHED', 'DEGRADED_FALLBACK'] as const).map((modeKey) => {
            const active = health.mode === modeKey;
            return (
              <div
                key={modeKey}
                className={`rounded border p-3 text-xs ${
                  active
                    ? 'border-[#0F172A] bg-[#F7F9FB]'
                    : 'border-[#E6EBF0] bg-white opacity-60'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-[#0F172A]">{modeKey}</span>
                  {active && <StatusPill tone={statusTone(modeKey)}>Active</StatusPill>}
                </div>
                <p className="mt-1 text-[#526071]">
                  {modeKey === 'NORMAL'
                    ? 'Simulator connected, snapshot fresh, ML forecaster and digital twin active.'
                    : modeKey === 'DEGRADED_CACHED'
                      ? 'Upstream simulator unreachable or stale; serving last-known-good snapshot.'
                      : 'Forecasting or planner exception; deterministic fallback heuristics active.'}
                </p>
              </div>
            );
          })}
        </div>

        {alerts.length > 0 && (
          <div className="mt-4 space-y-2">
            {alerts.map((msg, idx) => (
              <div
                key={idx}
                className="rounded border border-[#FDE68A] bg-[#FFFBEB] px-3 py-2 text-xs text-[#92400E]"
              >
                {msg}
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Subsystem Diagnostics" subtitle="Adapter circuit breaker, state store, and background poller">
        <div className="divide-y divide-[#E6EBF0]">
          <div className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0">
            <div>
              <div className="text-sm font-semibold text-[#0F172A]">Simulator Adapter & Circuit Breaker</div>
              <div className="font-mono-num mt-0.5 text-xs text-[#526071]">
                State: {sim.circuit_state} · Consecutive failures: {sim.consecutive_failures} · Cooldown:{' '}
                {sim.cooldown_remaining.toFixed(1)}s
              </div>
            </div>
            <div className="flex items-center gap-2">
              <StatusPill tone={breakerTone(sim.circuit_state)}>
                Circuit {sim.circuit_state}
              </StatusPill>
              <StatusPill tone={sim.connected ? 'ok' : 'critical'}>
                {sim.connected ? 'Connected' : 'Unreachable'}
              </StatusPill>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 py-3">
            <div>
              <div className="text-sm font-semibold text-[#0F172A]">In-Memory Snapshot Repository</div>
              <div className="font-mono-num mt-0.5 text-xs text-[#526071]">
                Age:{' '}
                {typeof store.data_age_seconds === 'number'
                  ? `${store.data_age_seconds.toFixed(1)}s`
                  : '—'}{' '}
                · Updates: {formatInt(store.total_updates)} · Failed polls:{' '}
                {formatInt(store.failed_polls)}
              </div>
            </div>
            <StatusPill tone={store.has_snapshot ? (store.is_stale ? 'watch' : 'ok') : 'critical'}>
              {!store.has_snapshot ? 'Empty' : store.is_stale ? 'Stale' : 'Fresh'}
            </StatusPill>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 py-3 last:pb-0">
            <div>
              <div className="text-sm font-semibold text-[#0F172A]">Background Poller Loop</div>
              <div className="font-mono-num mt-0.5 text-xs text-[#526071]">
                Configured interval: {poller.poll_interval_seconds}s
              </div>
            </div>
            <StatusPill tone={poller.is_running ? 'ok' : 'critical'}>
              {poller.is_running ? 'Running' : 'Stopped'}
            </StatusPill>
          </div>
        </div>
      </Panel>

      <Panel
        title="Prometheus Observability (/metrics)"
        subtitle="Live parsed counters and gauges from the FastAPI Prometheus registry"
        actions={
          metrics && (
            <button
              type="button"
              onClick={() => setShowRaw((s) => !s)}
              className="inline-flex cursor-pointer items-center gap-1 rounded border border-[#D6DCE3] bg-[#F7F9FB] px-2.5 py-1 text-xs font-medium text-[#0F172A] hover:bg-[#EEF1F4] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
            >
              {showRaw ? (
                <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {showRaw ? 'Hide raw exposition' : 'Raw Prometheus text'}
            </button>
          )
        }
      >
        {!metrics ? (
          <EmptyNotice title="Metrics endpoint unavailable" />
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 divide-y divide-[#E6EBF0] rounded border border-[#E6EBF0] bg-[#F7F9FB] sm:grid-cols-4 sm:divide-x sm:divide-y-0">
              <div className="p-3">
                <div className="text-xs text-[#526071]">HTTP Requests</div>
                <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
                  {formatInt(metrics.http_requests_total)}
                </div>
              </div>
              <div className="p-3">
                <div className="text-xs text-[#526071]">Simulator Errors</div>
                <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
                  {formatInt(metrics.simulator_errors_total)}
                </div>
              </div>
              <div className="p-3">
                <div className="text-xs text-[#526071]">Fallback Activations</div>
                <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
                  {formatInt(metrics.fallback_activations_total)}
                </div>
              </div>
              <div className="p-3">
                <div className="text-xs text-[#526071]">Forecast Error Gauge</div>
                <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
                  {formatDecimal(metrics.forecast_mae_liters, ' L')} · {formatDecimal(metrics.forecast_mape_percent, '%')}
                </div>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <MetricGroup title="HTTP Status Classes" entries={metrics.http_requests_by_status} />
              <MetricGroup
                title="Simulator Calls by Outcome"
                entries={metrics.simulator_requests_by_outcome}
              />
              <MetricGroup
                title="Recommendations by Outcome"
                entries={metrics.recommendations_by_outcome}
              />
              <MetricGroup title="Alerts Raised by Severity" entries={metrics.alerts_by_severity} />
              <MetricGroup title="Simulator Errors by Type" entries={metrics.simulator_errors_by_type} />
              <MetricGroup title="Fallback Reasons" entries={metrics.fallback_by_reason} />
            </div>

            {showRaw && (
              <pre className="max-h-72 overflow-auto rounded border border-[#D6DCE3] bg-[#0F172A] p-3 font-mono text-[11px] leading-relaxed text-[#E2E8F0]">
                {metrics.raw_text}
              </pre>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}
