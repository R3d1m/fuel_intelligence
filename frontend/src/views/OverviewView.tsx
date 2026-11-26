import { AlertTriangle, ArrowUpRight, Clock, ShieldAlert, Truck } from 'lucide-react';
import { NetworkGraph } from '../components/NetworkGraph';
import {
  EmptyNotice,
  facilityTone,
  formatDecimal,
  formatInt,
  formatLabel,
  formatLiters,
  formatTime,
  MeterBar,
  Panel,
  StatusPill,
  statusTone,
} from '../components/ui';
import type { ForecastResponse, HealthResponse, RiskResponse, StateResponse } from '../types';

interface OverviewViewProps {
  health: HealthResponse | null;
  state: StateResponse | null;
  forecast: ForecastResponse | null;
  risk: RiskResponse | null;
  pendingRecsCount: number;
  onInspectStation: (stationId: string, fuelType?: string) => void;
  onOpenDispatches: () => void;
}

export function OverviewView({
  health,
  state,
  forecast,
  risk,
  pendingRecsCount,
  onInspectStation,
  onOpenDispatches,
}: OverviewViewProps) {
  const snap = state?.snapshot;
  const stations = snap?.stations ?? [];
  const depots = snap?.depots ?? [];
  const routes = snap?.routes ?? [];
  const events = snap?.events ?? [];
  const allocations = snap?.allocations ?? [];
  const arrivals = snap?.supply_arrivals ?? [];

  const fuelTotals: Record<string, { stationInv: number; stationCap: number; depotInv: number; depotCap: number }> = {};
  for (const s of stations) {
    for (const [fuel, val] of Object.entries(s.inventory ?? {})) {
      if (!fuelTotals[fuel]) {
        fuelTotals[fuel] = { stationInv: 0, stationCap: 0, depotInv: 0, depotCap: 0 };
      }
      fuelTotals[fuel].stationInv += val;
      fuelTotals[fuel].stationCap += s.capacity?.[fuel] ?? 0;
    }
  }
  for (const d of depots) {
    for (const [fuel, val] of Object.entries(d.inventory ?? {})) {
      if (!fuelTotals[fuel]) {
        fuelTotals[fuel] = { stationInv: 0, stationCap: 0, depotInv: 0, depotCap: 0 };
      }
      fuelTotals[fuel].depotInv += val;
      fuelTotals[fuel].depotCap += d.capacity?.[fuel] ?? 0;
    }
  }

  const inFlightAllocs = allocations.filter(
    (a) => a.status === 'IN_TRANSIT' || a.status === 'PENDING',
  );
  const scheduledArrivals = arrivals.filter(
    (a) => a.status === 'SCHEDULED' || a.status === 'DELAYED',
  );

  const etaH = risk?.earliest_stockout_hours;
  const hasEta = typeof etaH === 'number' && Number.isFinite(etaH);
  const alerts = risk?.active_alerts ?? [];

  return (
    <div className="space-y-5">
      {/* Operational Mode Alert Banner when degraded */}
      {health && health.mode !== 'NORMAL' && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-[#FDE68A] bg-[#FFFBEB] px-4 py-3 text-xs text-[#92400E]"
        >
          <div className="flex items-center gap-2.5">
            <AlertTriangle className="h-4 w-4 shrink-0 text-[#B45309]" aria-hidden="true" />
            <div>
              <span className="font-semibold">{formatLabel(health.mode)}:</span> {health.reason}
            </div>
          </div>
          {typeof state?.data_age_seconds === 'number' && (
            <span className="font-mono-num">Snapshot age: {state.data_age_seconds.toFixed(1)}s</span>
          )}
        </div>
      )}

      {/* Structural Telemetry Strip */}
      <div className="grid grid-cols-2 divide-y divide-[#E6EBF0] rounded-md border border-[#D6DCE3] bg-white sm:grid-cols-3 lg:grid-cols-6 lg:divide-x lg:divide-y-0">
        <div className="p-3.5">
          <div className="text-xs text-[#526071]">Simulation Clock</div>
          <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
            {typeof snap?.tick === 'number' ? `Tick ${snap.tick}` : '—'}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-[#526071]">
            {snap?.instance ? `${snap.instance.tick_minutes}m/tick · ${snap.instance.scenario_id}` : 'Awaiting snapshot'}
          </div>
        </div>

        <div className="p-3.5">
          <div className="text-xs text-[#526071]">Fleet Consumption</div>
          <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
            {typeof forecast?.total_fleet_demand_per_hour === 'number'
              ? `${formatInt(forecast.total_fleet_demand_per_hour)} L/h`
              : '—'}
          </div>
          <div className="mt-0.5 text-[11px] text-[#526071]">
            {forecast?.active_anomalies_count
              ? `${forecast.active_anomalies_count} demand spike(s)`
              : 'Demand nominal'}
          </div>
        </div>

        <div className="p-3.5">
          <div className="text-xs text-[#526071]">Risk Posture</div>
          <div className="mt-1 flex items-center gap-2">
            <StatusPill tone={statusTone(risk?.overall_severity)}>
              {formatLabel(risk?.overall_severity ?? 'Pending')}
            </StatusPill>
          </div>
          <div className="font-mono-num mt-1 text-[11px] text-[#526071]">
            {risk
              ? `${risk.critical_count} crit · ${risk.watch_count} watch · ${risk.ok_count} ok`
              : '—'}
          </div>
        </div>

        <div className="p-3.5">
          <div className="text-xs text-[#526071]">Nearest Stockout</div>
          <div
            className={`font-mono-num mt-1 text-base font-semibold ${
              hasEta && etaH < 6 ? 'text-[#BE123C]' : 'text-[#0F172A]'
            }`}
          >
            {hasEta ? `${etaH.toFixed(1)} h` : 'None in horizon'}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-[#526071]">
            {risk?.earliest_stockout_station_id
              ? `${risk.earliest_stockout_station_id} (${formatLabel(risk.earliest_stockout_fuel)})`
              : 'All stations covered'}
          </div>
        </div>

        <div className="p-3.5">
          <div className="text-xs text-[#526071]">Model Confidence</div>
          <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
            {typeof risk?.fleet_average_confidence === 'number'
              ? `${Math.round(risk.fleet_average_confidence * 100)}%`
              : '—'}
          </div>
          <div className="font-mono-num mt-0.5 text-[11px] text-[#526071]">
            {forecast ? `MAE ${forecast.average_mae.toFixed(1)} · MAPE ${forecast.average_mape.toFixed(1)}%` : '—'}
          </div>
        </div>

        <div className="p-3.5">
          <div className="text-xs text-[#526071]">Dispatch Queue</div>
          <div className="mt-1 flex items-baseline justify-between gap-2">
            <span className="font-mono-num text-base font-semibold text-[#0F172A]">
              {pendingRecsCount} pending
            </span>
            <button
              type="button"
              onClick={onOpenDispatches}
              className="inline-flex cursor-pointer items-center gap-0.5 text-xs font-medium text-[#1D4ED8] hover:underline focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
            >
              Review
              <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
          <div className="font-mono-num mt-0.5 text-[11px] text-[#526071]">
            {inFlightAllocs.length} shipment(s) in flight
          </div>
        </div>
      </div>

      {/* Main Split: Live Topology + Priority Triage */}
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
        <Panel
          className="xl:col-span-8"
          title="Live Supply Topology"
          subtitle="Click any station to open risk, forecast, and digital twin projections"
          actions={
            <div className="flex flex-wrap items-center gap-3 text-xs text-[#526071]">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#0B6E4F]" aria-hidden="true" /> OK
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#D97706]" aria-hidden="true" /> Watch
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#BE123C]" aria-hidden="true" /> Critical
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#1D4ED8]" aria-hidden="true" /> Active transit
              </span>
            </div>
          }
        >
          <NetworkGraph
            depots={depots}
            stations={stations}
            routes={routes}
            allocations={allocations}
            risk={risk}
            onSelectStation={(stId) => onInspectStation(stId)}
          />

          {/* In-flight shipments & scheduled depot arrivals */}
          <div className="mt-4 grid grid-cols-1 gap-3 border-t border-[#E6EBF0] pt-4 md:grid-cols-2">
            <div className="rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3">
              <div className="mb-2 flex items-center justify-between text-xs font-semibold text-[#0F172A]">
                <span className="inline-flex items-center gap-1.5">
                  <Truck className="h-3.5 w-3.5 text-[#1D4ED8]" aria-hidden="true" />
                  In-Flight Dispatches ({inFlightAllocs.length})
                </span>
              </div>
              {inFlightAllocs.length === 0 ? (
                <p className="text-xs text-[#526071]">No active dispatches currently in transit.</p>
              ) : (
                <ul className="space-y-1.5 text-xs">
                  {inFlightAllocs.slice(0, 4).map((a) => (
                    <li key={a.id} className="flex items-center justify-between gap-2">
                      <span className="truncate text-[#0F172A]">
                        <span className="font-mono-num font-medium">{formatLiters(a.quantity)}</span>{' '}
                        {formatLabel(a.fuel_type)} → {a.destination_station_id}
                      </span>
                      <span className="font-mono-num shrink-0 text-[#526071]">
                        {a.expected_arrival_tick !== null ? `arr t${a.expected_arrival_tick}` : formatLabel(a.status)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3">
              <div className="mb-2 flex items-center justify-between text-xs font-semibold text-[#0F172A]">
                <span className="inline-flex items-center gap-1.5">
                  <Clock className="h-3.5 w-3.5 text-[#0B6E4F]" aria-hidden="true" />
                  Depot Supply Arrivals ({scheduledArrivals.length})
                </span>
              </div>
              {scheduledArrivals.length === 0 ? (
                <p className="text-xs text-[#526071]">No pending pipeline/rail arrivals scheduled.</p>
              ) : (
                <ul className="space-y-1.5 text-xs">
                  {scheduledArrivals.slice(0, 4).map((arr) => (
                    <li key={arr.id} className="flex items-center justify-between gap-2">
                      <span className="truncate text-[#0F172A]">
                        <span className="font-mono-num font-medium">{formatLiters(arr.quantity)}</span>{' '}
                        {formatLabel(arr.fuel_type)} @ {arr.depot_id}
                      </span>
                      <StatusPill tone={statusTone(arr.status)}>
                        t{arr.actual_tick ?? arr.planned_tick}
                      </StatusPill>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </Panel>

        {/* Priority Alerts & Active Simulation Events */}
        <Panel
          className="xl:col-span-4"
          title="Priority Risk Triage"
          subtitle={
            risk
              ? `${formatLiters(risk.total_projected_unmet_liters)} projected unmet over horizon`
              : 'Urgency-ranked stockout risks'
          }
        >
          {alerts.length === 0 ? (
            <EmptyNotice
              title="No active stockout alerts"
              hint="All stations and fuel grades are covered within the forecast horizon."
            />
          ) : (
            <div className="divide-y divide-[#E6EBF0]">
              {alerts.slice(0, 5).map((alert, idx) => (
                <div key={`${alert.station_id}-${alert.fuel_type}-${idx}`} className="py-3 first:pt-0">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusPill tone={statusTone(alert.severity)}>
                          {formatLabel(alert.severity)}
                        </StatusPill>
                        <button
                          type="button"
                          onClick={() => onInspectStation(alert.station_id, String(alert.fuel_type))}
                          className="cursor-pointer truncate text-left text-xs font-semibold text-[#0F172A] hover:text-[#1D4ED8] hover:underline focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                        >
                          {alert.station_name || alert.station_id} · {formatLabel(String(alert.fuel_type))}
                        </button>
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs text-[#526071]">{alert.explanation}</p>
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="font-mono-num text-xs font-semibold text-[#0F172A]">
                        {typeof alert.hours_to_stockout === 'number'
                          ? `${alert.hours_to_stockout.toFixed(1)}h`
                          : 'Watch'}
                      </div>
                      <div className="font-mono-num text-[11px] text-[#526071]">
                        urg {alert.urgency_score.toFixed(1)}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {events.length > 0 && (
            <div className="mt-4 border-t border-[#E6EBF0] pt-3">
              <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-[#0F172A]">
                <ShieldAlert className="h-3.5 w-3.5 text-[#B45309]" aria-hidden="true" />
                Active Simulation Events ({events.length})
              </div>
              <div className="space-y-1.5">
                {events.slice(0, 4).map((ev) => (
                  <div
                    key={ev.id}
                    className="flex items-center justify-between gap-2 rounded border border-[#FDE68A] bg-[#FFFBEB] px-2.5 py-1.5 text-xs text-[#92400E]"
                  >
                    <span className="font-medium">{formatLabel(ev.type)}</span>
                    <span className="font-mono-num">
                      t{ev.start_tick}–{ev.end_tick !== null ? `t${ev.end_tick}` : '∞'} · {formatLabel(ev.status)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>
      </div>

      {/* Bottom Split: Commodity Balance & Forecast Diagnostics */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Panel
          title="Commodity Inventory Balance"
          subtitle={`Retail tanks across ${stations.length} stations vs upstream reserves at ${depots.length} depots`}
        >
          {Object.keys(fuelTotals).length === 0 ? (
            <EmptyNotice title="No inventory telemetry yet" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-[#E6EBF0] text-[#526071]">
                    <th className="pb-2 font-medium">Commodity</th>
                    <th className="pb-2 text-right font-medium">Station Stock</th>
                    <th className="pb-2 text-right font-medium">Retail Fill</th>
                    <th className="pb-2 text-right font-medium">Depot Reserve</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#E6EBF0]">
                  {Object.entries(fuelTotals).map(([fuel, t]) => {
                    const stPct = t.stationCap > 0 ? (t.stationInv / t.stationCap) * 100 : 0;
                    const dpPct = t.depotCap > 0 ? (t.depotInv / t.depotCap) * 100 : 0;
                    const tone = stPct < 25 ? 'critical' : stPct < 50 ? 'watch' : 'ok';
                    return (
                      <tr key={fuel}>
                        <td className="py-2.5 font-semibold text-[#0F172A]">{formatLabel(fuel)}</td>
                        <td className="font-mono-num py-2.5 text-right text-[#0F172A]">
                          {formatLiters(t.stationInv)} / {formatInt(t.stationCap)}
                        </td>
                        <td className="py-2.5 text-right">
                          <StatusPill tone={tone}>{stPct.toFixed(0)}%</StatusPill>
                        </td>
                        <td className="font-mono-num py-2.5 text-right text-[#526071]">
                          {formatLiters(t.depotInv)} ({dpPct.toFixed(0)}%)
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {depots.length > 0 && (
            <div className="mt-4 grid grid-cols-1 gap-2 border-t border-[#E6EBF0] pt-3 sm:grid-cols-2">
              {depots.map((d) => {
                const inv = Object.values(d.inventory ?? {}).reduce((a, b) => a + b, 0);
                const cap = Object.values(d.capacity ?? {}).reduce((a, b) => a + b, 0);
                const pct = cap > 0 ? (inv / cap) * 100 : 0;
                return (
                  <div key={d.id} className="rounded border border-[#E6EBF0] bg-[#F7F9FB] p-2.5">
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span className="font-semibold text-[#0F172A]">{d.name || d.id}</span>
                      <StatusPill tone={facilityTone(d.status)}>{formatLabel(d.status)}</StatusPill>
                    </div>
                    <div className="mt-1.5">
                      <MeterBar percent={pct} tone={pct < 25 ? 'watch' : 'ok'} label={`${d.name} stock`} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Panel>

        <Panel
          title="Demand Forecaster & Anomaly Signals"
          subtitle="Walk-forward backtest error and dual-signal spike detection"
          actions={
            forecast?.evaluated_at && (
              <span className="font-mono-num text-[11px] text-[#526071]">
                Evaluated {formatTime(forecast.evaluated_at)}
              </span>
            )
          }
        >
          {forecast ? (
            <div className="space-y-4">
              <div className="grid grid-cols-3 divide-x divide-[#E6EBF0] rounded border border-[#E6EBF0] bg-[#F7F9FB]">
                <div className="p-3">
                  <div className="text-xs text-[#526071]">Average MAE</div>
                  <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
                    {formatDecimal(forecast.average_mae, ' L/t')}
                  </div>
                </div>
                <div className="p-3">
                  <div className="text-xs text-[#526071]">Average MAPE</div>
                  <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
                    {formatDecimal(forecast.average_mape, '%')}
                  </div>
                </div>
                <div className="p-3">
                  <div className="text-xs text-[#526071]">Demand Spikes</div>
                  <div className="font-mono-num mt-1 text-base font-semibold text-[#0F172A]">
                    {forecast.active_anomalies_count}
                  </div>
                </div>
              </div>

              {forecast.active_anomalies.length > 0 ? (
                <div className="space-y-2">
                  {forecast.active_anomalies.slice(0, 4).map((a, idx) => (
                    <div
                      key={idx}
                      className="rounded border border-[#FDE68A] bg-[#FFFBEB] px-3 py-2 text-xs text-[#92400E]"
                    >
                      <div className="flex items-center justify-between gap-2 font-medium">
                        <span>{a.explanation}</span>
                        <span className="font-mono-num shrink-0">
                          {a.spike_ratio.toFixed(2)}× baseline
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-[#526071]">
                  No abnormal consumption spikes detected across active stations.
                </p>
              )}
            </div>
          ) : (
            <EmptyNotice title="Awaiting forecast evaluation" />
          )}
        </Panel>
      </div>
    </div>
  );
}
