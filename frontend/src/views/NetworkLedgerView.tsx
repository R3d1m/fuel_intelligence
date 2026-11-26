import { useState } from 'react';
import {
  EmptyNotice,
  facilityTone,
  formatDateTime,
  formatInt,
  formatLabel,
  formatLiters,
  MeterBar,
  Panel,
  StatusPill,
  statusTone,
} from '../components/ui';
import type { AuditEvent, AuditStats, RecommendationItem, RiskResponse, StateResponse } from '../types';

interface NetworkLedgerViewProps {
  state: StateResponse | null;
  risk: RiskResponse | null;
  allRecommendations: RecommendationItem[];
  auditEvents: AuditEvent[];
  auditStats: AuditStats | null;
  onInspectStation: (stationId: string) => void;
}

type SubTab = 'stations' | 'depots' | 'routes' | 'events' | 'audit';

export function NetworkLedgerView({
  state,
  risk,
  allRecommendations,
  auditEvents,
  auditStats,
  onInspectStation,
}: NetworkLedgerViewProps) {
  const [subTab, setSubTab] = useState<SubTab>('stations');
  const [auditFilter, setAuditFilter] = useState<string>('ALL');
  const [search, setSearch] = useState('');

  const snap = state?.snapshot;
  const stations = snap?.stations ?? [];
  const depots = snap?.depots ?? [];
  const routes = snap?.routes ?? [];
  const arrivals = snap?.supply_arrivals ?? [];
  const allocations = snap?.allocations ?? [];
  const events = snap?.events ?? [];

  const filteredRecs = allRecommendations.filter((r) => {
    if (auditFilter !== 'ALL' && r.status !== auditFilter) return false;
    if (search.trim()) {
      const q = search.toLowerCase();
      const hay = `${r.station_name} ${r.station_id} ${r.fuel_type} ${r.status} ${r.idempotency_key}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  return (
    <div className="space-y-5">
      <div
        role="tablist"
        aria-label="Network and ledger views"
        className="flex flex-wrap gap-1 border-b border-[#D6DCE3] pb-2"
      >
        {(
          [
            ['stations', `Stations (${stations.length})`],
            ['depots', `Depots & Arrivals (${depots.length})`],
            ['routes', `Routes & Allocations (${routes.length})`],
            ['events', `Sim Events (${events.length})`],
            ['audit', `SQLite Audit Ledger (${auditStats?.total_recommendations ?? allRecommendations.length})`],
          ] as Array<[SubTab, string]>
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={subTab === key}
            onClick={() => setSubTab(key)}
            className={`cursor-pointer rounded px-3 py-1.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none ${
              subTab === key
                ? 'bg-[#0F172A] text-white'
                : 'text-[#526071] hover:bg-white hover:text-[#0F172A]'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {subTab === 'stations' &&
        (stations.length === 0 ? (
          <EmptyNotice title="No stations loaded in snapshot" />
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {stations.map((s) => {
              const stRisks = risk?.station_risks?.[s.id] ?? {};
              return (
                <Panel key={s.id} className="space-y-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <button
                        type="button"
                        onClick={() => onInspectStation(s.id)}
                        className="cursor-pointer text-left text-sm font-semibold text-[#0F172A] hover:text-[#1D4ED8] hover:underline focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                      >
                        {s.name}
                      </button>
                      <div className="font-mono-num text-xs text-[#526071]">
                        {s.id} · {s.region_id} · {s.demand_profile}
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5">
                      {s.demand_multiplier > 1.05 && (
                        <StatusPill tone="watch">{s.demand_multiplier.toFixed(1)}× demand</StatusPill>
                      )}
                      <StatusPill tone={facilityTone(s.status)}>{formatLabel(s.status)}</StatusPill>
                    </div>
                  </div>

                  <div className="space-y-2.5">
                    {Object.entries(s.inventory ?? {}).map(([fuel, inv]) => {
                      const cap = s.capacity?.[fuel] ?? 1;
                      const pct = cap > 0 ? (inv / cap) * 100 : 0;
                      const fuelRisk = stRisks[fuel];
                      const tone = fuelRisk
                        ? statusTone(fuelRisk.severity)
                        : pct < 20
                          ? 'critical'
                          : pct < 45
                            ? 'watch'
                            : 'ok';
                      return (
                        <div key={fuel} className="space-y-1">
                          <div className="flex justify-between text-xs">
                            <span className="font-medium text-[#0F172A]">{formatLabel(fuel)}</span>
                            <span className="font-mono-num text-[#526071]">
                              {formatInt(inv)} / {formatLiters(cap)} ({pct.toFixed(0)}%)
                              {typeof fuelRisk?.hours_to_stockout === 'number'
                                ? ` · ${fuelRisk.hours_to_stockout.toFixed(1)}h ETA`
                                : ''}
                            </span>
                          </div>
                          <MeterBar percent={pct} tone={tone} label={`${s.name} ${fuel}`} />
                        </div>
                      );
                    })}
                  </div>
                </Panel>
              );
            })}
          </div>
        ))}

      {subTab === 'depots' && (
        <div className="space-y-5">
          {depots.length === 0 ? (
            <EmptyNotice title="No depots loaded in snapshot" />
          ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {depots.map((d) => (
                <Panel key={d.id} className="space-y-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="text-sm font-semibold text-[#0F172A]">{d.name}</div>
                      <div className="font-mono-num text-xs text-[#526071]">
                        {d.id} · {d.region_id} · Max {formatLiters(d.dispatch_capacity_per_tick)}/tick
                      </div>
                    </div>
                    <StatusPill tone={facilityTone(d.status)}>{formatLabel(d.status)}</StatusPill>
                  </div>
                  <div className="space-y-2.5">
                    {Object.entries(d.inventory ?? {}).map(([fuel, inv]) => {
                      const cap = d.capacity?.[fuel] ?? 1;
                      const pct = cap > 0 ? (inv / cap) * 100 : 0;
                      const dr = risk?.depot_risks?.[d.id]?.[fuel];
                      return (
                        <div key={fuel} className="space-y-1">
                          <div className="flex justify-between text-xs">
                            <span className="font-medium text-[#0F172A]">{formatLabel(fuel)}</span>
                            <span className="font-mono-num text-[#526071]">
                              {formatInt(inv)} / {formatLiters(cap)} ({pct.toFixed(0)}%)
                              {dr ? ` · Avail ${formatLiters(dr.effective_available_liters)}` : ''}
                            </span>
                          </div>
                          <MeterBar percent={pct} tone={pct < 25 ? 'watch' : 'ok'} label={`${d.name} ${fuel}`} />
                        </div>
                      );
                    })}
                  </div>
                </Panel>
              ))}
            </div>
          )}

          <Panel
            title={`Upstream Supply Arrivals (${arrivals.length})`}
            subtitle="Scheduled, delayed, and completed pipeline or rail replenishments"
          >
            {arrivals.length === 0 ? (
              <EmptyNotice title="No upstream supply arrivals recorded" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-[#E6EBF0] text-[#526071]">
                      <th className="pb-2 font-medium">Arrival ID</th>
                      <th className="pb-2 font-medium">Depot</th>
                      <th className="pb-2 font-medium">Fuel</th>
                      <th className="pb-2 text-right font-medium">Quantity</th>
                      <th className="pb-2 text-right font-medium">Planned / Actual Tick</th>
                      <th className="pb-2 text-right font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#E6EBF0]">
                    {arrivals.map((a) => (
                      <tr key={a.id}>
                        <td className="font-mono-num py-2 text-[#0F172A]">{a.id}</td>
                        <td className="font-mono-num py-2 text-[#526071]">{a.depot_id}</td>
                        <td className="py-2 text-[#0F172A]">{formatLabel(String(a.fuel_type))}</td>
                        <td className="font-mono-num py-2 text-right font-medium text-[#0F172A]">
                          {formatLiters(a.quantity)}
                        </td>
                        <td className="font-mono-num py-2 text-right text-[#526071]">
                          t{a.planned_tick}
                          {a.actual_tick !== null ? ` → t${a.actual_tick}` : ''}
                        </td>
                        <td className="py-2 text-right">
                          <StatusPill tone={statusTone(a.status)}>{formatLabel(a.status)}</StatusPill>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}

      {subTab === 'routes' && (
        <div className="space-y-5">
          <Panel
            title={`Transit Corridors (${routes.length})`}
            subtitle="Physical depot-to-station transport links, transit times, and tanker shipment caps"
          >
            {routes.length === 0 ? (
              <EmptyNotice title="No routes available" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-[#E6EBF0] text-[#526071]">
                      <th className="pb-2 font-medium">Route ID</th>
                      <th className="pb-2 font-medium">Source Depot</th>
                      <th className="pb-2 font-medium">Destination Station</th>
                      <th className="pb-2 font-medium">Status</th>
                      <th className="pb-2 text-right font-medium">Transit</th>
                      <th className="pb-2 text-right font-medium">Max Shipment</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#E6EBF0]">
                    {routes.map((r) => (
                      <tr key={r.id}>
                        <td className="font-mono-num py-2.5 font-medium text-[#0F172A]">{r.id}</td>
                        <td className="font-mono-num py-2.5 text-[#526071]">{r.source_depot_id}</td>
                        <td className="font-mono-num py-2.5 text-[#526071]">
                          {r.destination_station_id}
                        </td>
                        <td className="py-2.5">
                          <StatusPill tone={statusTone(r.status)}>{formatLabel(r.status)}</StatusPill>
                        </td>
                        <td className="font-mono-num py-2.5 text-right text-[#526071]">
                          {r.transit_ticks} ticks
                        </td>
                        <td className="font-mono-num py-2.5 text-right font-medium text-[#0F172A]">
                          {formatLiters(r.max_shipment)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel
            title={`Simulator Allocations Ledger (${allocations.length})`}
            subtitle="All pending, in-transit, arrived, and failed dispatches reported by the simulator"
          >
            {allocations.length === 0 ? (
              <EmptyNotice title="No simulator allocations recorded yet" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-[#E6EBF0] text-[#526071]">
                      <th className="pb-2 font-medium">ID</th>
                      <th className="pb-2 font-medium">Corridor</th>
                      <th className="pb-2 font-medium">Fuel</th>
                      <th className="pb-2 text-right font-medium">Quantity</th>
                      <th className="pb-2 text-right font-medium">Ticks</th>
                      <th className="pb-2 font-medium">Status</th>
                      <th className="pb-2 font-medium">Notes / Failure</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#E6EBF0]">
                    {allocations.slice(0, 50).map((a) => (
                      <tr key={a.id}>
                        <td className="font-mono-num py-2 font-medium text-[#0F172A]">#{a.id}</td>
                        <td className="font-mono-num py-2 text-[#526071]">
                          {a.source_depot_id} → {a.destination_station_id} ({a.route_id})
                        </td>
                        <td className="py-2 text-[#0F172A]">{formatLabel(String(a.fuel_type))}</td>
                        <td className="font-mono-num py-2 text-right font-medium text-[#0F172A]">
                          {formatLiters(a.quantity)}
                        </td>
                        <td className="font-mono-num py-2 text-right text-[#526071]">
                          c{a.created_tick}
                          {a.expected_arrival_tick !== null ? ` → a${a.expected_arrival_tick}` : ''}
                        </td>
                        <td className="py-2">
                          <StatusPill tone={statusTone(a.status)}>{formatLabel(a.status)}</StatusPill>
                        </td>
                        <td className="py-2 text-[#526071]">
                          {a.failure_reason || (
                            <span className="font-mono-num text-[11px]" translate="no">
                              {a.idempotency_key}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}

      {subTab === 'events' && (
        <Panel
          title={`Simulation Events (${events.length})`}
          subtitle="Injected disruptions, demand spikes, supply delays, and outages"
        >
          {events.length === 0 ? (
            <EmptyNotice title="No active or scheduled simulation events" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-[#E6EBF0] text-[#526071]">
                    <th className="pb-2 font-medium">ID</th>
                    <th className="pb-2 font-medium">Event Type</th>
                    <th className="pb-2 font-medium">Status</th>
                    <th className="pb-2 font-medium">Tick Window</th>
                    <th className="pb-2 font-medium">Parameters</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#E6EBF0]">
                  {events.map((e) => (
                    <tr key={e.id}>
                      <td className="font-mono-num py-2.5 text-[#526071]">#{e.id}</td>
                      <td className="py-2.5 font-semibold text-[#0F172A]">
                        {formatLabel(e.type)}
                      </td>
                      <td className="py-2.5">
                        <StatusPill tone={statusTone(e.status)}>{formatLabel(e.status)}</StatusPill>
                      </td>
                      <td className="font-mono-num py-2.5 text-[#526071]">
                        t{e.start_tick} → {e.end_tick !== null ? `t${e.end_tick}` : 'ongoing'}
                      </td>
                      <td className="font-mono-num py-2.5 text-[#0F172A]">
                        {Object.keys(e.parameters ?? {}).length === 0
                          ? '—'
                          : Object.entries(e.parameters)
                              .map(([k, v]) => `${k}: ${String(v)}`)
                              .join(' · ')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}

      {subTab === 'audit' && (
        <div className="space-y-5">
          <div className="grid grid-cols-2 divide-y divide-[#E6EBF0] rounded-md border border-[#D6DCE3] bg-white sm:grid-cols-5 sm:divide-x sm:divide-y-0">
            <div className="p-3.5">
              <div className="text-xs text-[#526071]">Recommendations</div>
              <div className="font-mono-num mt-1 text-lg font-semibold text-[#0F172A]">
                {formatInt(auditStats?.total_recommendations ?? 0)}
              </div>
            </div>
            <div className="p-3.5">
              <div className="text-xs text-[#526071]">Recommended Volume</div>
              <div className="font-mono-num mt-1 text-lg font-semibold text-[#0F172A]">
                {formatLiters(auditStats?.total_liters_recommended ?? 0)}
              </div>
            </div>
            <div className="p-3.5">
              <div className="text-xs text-[#526071]">Submitted</div>
              <div className="font-mono-num mt-1 text-lg font-semibold text-[#0B6E4F]">
                {formatInt(auditStats?.submitted_count ?? 0)}
              </div>
            </div>
            <div className="p-3.5">
              <div className="text-xs text-[#526071]">Declined / Failed</div>
              <div className="font-mono-num mt-1 text-lg font-semibold text-[#0F172A]">
                {formatInt(auditStats?.rejected_count ?? 0)} / {formatInt(auditStats?.failed_count ?? 0)}
              </div>
            </div>
            <div className="p-3.5">
              <div className="text-xs text-[#526071]">Decline Rate</div>
              <div className="font-mono-num mt-1 text-lg font-semibold text-[#0F172A]">
                {((auditStats?.rejection_rate ?? 0) * 100).toFixed(1)}%
              </div>
            </div>
          </div>

          <Panel
            title="Decision Records"
            subtitle="Durable SQLite recommendation ledger"
            actions={
              <div className="flex flex-wrap items-center gap-2">
                <div className="flex items-center gap-1 rounded border border-[#D6DCE3] bg-[#F7F9FB] p-0.5">
                  {(
                    [
                      ['ALL', 'All'],
                      ['PROPOSED', 'Proposed'],
                      ['SUBMITTED', 'Submitted'],
                      ['REJECTED', 'Declined'],
                      ['FAILED', 'Failed'],
                    ] as const
                  ).map(([st, label]) => (
                    <button
                      key={st}
                      type="button"
                      onClick={() => setAuditFilter(st)}
                      className={`cursor-pointer rounded px-2 py-1 text-xs font-medium focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none ${
                        auditFilter === st ? 'bg-white text-[#0F172A] shadow-2xs' : 'text-[#526071]'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <label htmlFor="audit-search" className="sr-only">
                  Filter audit records
                </label>
                <input
                  id="audit-search"
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search station, fuel, key…"
                  className="rounded border border-[#D6DCE3] bg-white px-2.5 py-1.5 text-xs text-[#0F172A] placeholder:text-[#94A3B8] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                />
              </div>
            }
          >
            {filteredRecs.length === 0 ? (
              <EmptyNotice title="No matching decision records" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-[#E6EBF0] text-[#526071]">
                      <th className="pb-2 font-medium">Created</th>
                      <th className="pb-2 font-medium">Station</th>
                      <th className="pb-2 font-medium">Fuel</th>
                      <th className="pb-2 text-right font-medium">Volume</th>
                      <th className="pb-2 font-medium">Risk Drop</th>
                      <th className="pb-2 font-medium">Status</th>
                      <th className="pb-2 font-medium">Notes / Allocation</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#E6EBF0]">
                    {filteredRecs.slice(0, 50).map((r) => (
                      <tr key={r.recommendation_id}>
                        <td className="font-mono-num py-2 text-[#526071]">
                          {formatDateTime(r.created_at)}
                        </td>
                        <td className="py-2 font-medium text-[#0F172A]">{r.station_name}</td>
                        <td className="py-2 text-[#526071]">{formatLabel(String(r.fuel_type))}</td>
                        <td className="font-mono-num py-2 text-right font-medium text-[#0F172A]">
                          {formatLiters(r.quantity_liters)}
                        </td>
                        <td className="font-mono-num py-2 text-[#526071]">
                          {(r.stockout_probability_before * 100).toFixed(0)}% →{' '}
                          {(r.stockout_probability_after * 100).toFixed(0)}%
                        </td>
                        <td className="py-2">
                          <StatusPill tone={statusTone(r.status)}>{formatLabel(r.status)}</StatusPill>
                        </td>
                        <td className="py-2 text-[#526071]">
                          {r.failure_reason ||
                            r.operator_notes ||
                            (r.simulator_allocation_id !== null
                              ? `Alloc #${r.simulator_allocation_id}`
                              : '—')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel
            title={`State Transition Log (${auditEvents.length})`}
            subtitle="Append-only event history from decision_audit_log"
          >
            {auditEvents.length === 0 ? (
              <EmptyNotice title="No audit transitions recorded yet" />
            ) : (
              <div className="divide-y divide-[#E6EBF0] text-xs">
                {auditEvents.slice(0, 25).map((e) => (
                  <div key={e.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5">
                    <div className="flex flex-wrap items-center gap-2.5">
                      <span className="font-mono-num text-[#526071]">
                        {formatDateTime(e.timestamp)}
                      </span>
                      <span className="font-semibold text-[#0F172A]">
                        {formatLabel(e.event_type)}
                      </span>
                      <span className="font-mono-num text-[#526071]">
                        {e.from_status ?? 'NEW'} → {e.to_status}
                      </span>
                      <span className="font-mono-num text-[11px] text-[#526071]" translate="no">
                        {e.recommendation_id}
                      </span>
                    </div>
                    <span className="text-[#526071]">{e.operator_notes || ''}</span>
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}
