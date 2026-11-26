import { useState } from 'react';
import { ChevronDown, ChevronUp, FileText, Sparkles } from 'lucide-react';
import type { FuelApiClient } from '../api';
import {
  EmptyNotice,
  formatDateTime,
  formatInt,
  formatLabel,
  formatLiters,
  ModalDrawer,
  Panel,
  StatusPill,
  statusTone,
} from '../components/ui';
import type { AuditEvent, PlanResponse, RecommendationItem, RecommendationStatus } from '../types';

interface DispatchesPlanViewProps {
  client: FuelApiClient;
  recommendations: RecommendationItem[];
  plan: PlanResponse | null;
  simulatorStatus?: string;
  currentTick?: number;
  lastGenerateMsg?: string | null;
  onApprove: (id: string, notes?: string) => Promise<void>;
  onReject: (id: string, notes?: string) => Promise<void>;
  onGenerate: () => Promise<void>;
  busyId: string | null;
  generating: boolean;
}

type StatusFilter = 'PROPOSED' | 'SUBMITTED' | 'REJECTED' | 'FAILED' | 'ALL';

export function DispatchesPlanView({
  client,
  recommendations,
  plan,
  simulatorStatus,
  currentTick,
  lastGenerateMsg,
  onApprove,
  onReject,
  onGenerate,
  busyId,
  generating,
}: DispatchesPlanViewProps) {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('PROPOSED');
  const [notesMap, setNotesMap] = useState<Record<string, string>>({});
  const [confirmDeclineId, setConfirmDeclineId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const [detailRec, setDetailRec] = useState<RecommendationItem | null>(null);
  const [detailEvents, setDetailEvents] = useState<AuditEvent[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);

  const openInspector = async (recId: string) => {
    setDetailLoading(true);
    try {
      const [recRes, auditRes] = await Promise.all([
        client.getRecommendationById(recId),
        client.getAuditHistory({ recommendationId: recId, limit: 50 }),
      ]);
      setDetailRec(recRes.recommendation);
      setDetailEvents(auditRes.events ?? []);
    } catch {
      const fallback = recommendations.find((r) => r.recommendation_id === recId) ?? null;
      setDetailRec(fallback);
      setDetailEvents([]);
    } finally {
      setDetailLoading(false);
    }
  };

  const counts: Record<StatusFilter, number> = {
    ALL: recommendations.length,
    PROPOSED: recommendations.filter((r) => r.status === 'PROPOSED').length,
    SUBMITTED: recommendations.filter((r) => r.status === 'SUBMITTED').length,
    REJECTED: recommendations.filter((r) => r.status === 'REJECTED').length,
    FAILED: recommendations.filter((r) => r.status === 'FAILED').length,
  };

  const filtered = recommendations.filter((r) =>
    statusFilter === 'ALL' ? true : r.status === (statusFilter as RecommendationStatus),
  );

  const depotUtil = Object.entries(
    plan?.shared_capacity_summary?.depot_dispatch_utilization ?? {},
  );
  const unmetTargets = plan?.unmet_targets ?? [];

  return (
    <div className="space-y-5">
      {/* Constrained Plan Header */}
      <Panel
        title="Constrained Dispatch Queue & Optimization Plan"
        subtitle={
          plan
            ? `Active ID ${plan.plan_id} (tick ${plan.created_tick}) · ${plan.total_actions} planned action(s) totaling ${formatLiters(plan.total_liters_dispatched)}`
            : 'Human-in-the-loop dispatch proposals backed by deterministic idempotency keys'
        }
        actions={
          <button
            type="button"
            disabled={generating}
            onClick={onGenerate}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded bg-[#0B6E4F] px-3.5 py-2 text-xs font-medium text-white transition-colors hover:bg-[#08553D] disabled:cursor-not-allowed disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            {generating ? 'Generating…' : 'Generate Recommendations'}
          </button>
        }
      >
        {simulatorStatus?.toUpperCase() === 'PAUSED' && (
          <div className="mb-3 rounded border border-[#FDE68A] bg-[#FFFBEB] px-3 py-2 text-xs text-[#92400E]">
            Simulator is currently <strong>PAUSED</strong> at tick {currentTick ?? 0}. Inventory is
            not depleting until the simulation runs, so the planner may find 0 at-risk stations.
          </div>
        )}

        {lastGenerateMsg && (
          <div
            role="status"
            className="mb-3 rounded border border-[#BFDBFE] bg-[#EFF6FF] px-3 py-2 text-xs text-[#1E40AF]"
          >
            {lastGenerateMsg}
          </div>
        )}

        {plan?.summary && (
          <p className="mb-3 rounded border border-[#E6EBF0] bg-[#F7F9FB] px-3 py-2 text-xs text-[#334155]">
            {plan.summary}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-1 rounded border border-[#D6DCE3] bg-[#F7F9FB] p-1">
          {(
            [
              ['PROPOSED', 'Pending Approval'],
              ['SUBMITTED', 'Submitted'],
              ['REJECTED', 'Declined'],
              ['FAILED', 'Failed'],
              ['ALL', 'All'],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setStatusFilter(key)}
              className={`cursor-pointer rounded px-3 py-1.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none ${
                statusFilter === key
                  ? 'bg-white text-[#0F172A] shadow-2xs'
                  : 'text-[#526071] hover:text-[#0F172A]'
              }`}
            >
              {label} ({counts[key]})
            </button>
          ))}
        </div>
      </Panel>

      {/* Recommendations List */}
      {filtered.length === 0 ? (
        <EmptyNotice
          title={`No ${statusFilter === 'ALL' ? '' : formatLabel(statusFilter).toLowerCase() + ' '}recommendations`}
          hint="Run Generate Recommendations to evaluate current network deficits against depot and route limits."
        />
      ) : (
        <div className="space-y-4">
          {filtered.map((rec) => {
            const pBefore = (rec.stockout_probability_before ?? 0) * 100;
            const pAfter = (rec.stockout_probability_after ?? 0) * 100;
            const isOpen = !!expanded[rec.recommendation_id];
            const isBusy = busyId === rec.recommendation_id;
            const isConfirmingDecline = confirmDeclineId === rec.recommendation_id;
            const constraints = rec.constraints;
            const alts = rec.alternatives ?? [];
            const isProposed = rec.status === 'PROPOSED';

            return (
              <Panel key={rec.recommendation_id} className="space-y-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span className="font-mono-num text-lg font-semibold text-[#0F172A]">
                        {formatLiters(rec.quantity_liters)}
                      </span>
                      <span className="text-sm font-semibold text-[#0F172A]">
                        {formatLabel(String(rec.fuel_type))} → {rec.station_name || rec.station_id}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[#526071]">
                      <span>
                        From <strong className="font-medium text-[#0F172A]">{rec.source_depot_name}</strong>
                      </span>
                      <span>
                        Route <span className="font-mono-num text-[#0F172A]">{rec.route_id}</span>
                      </span>
                      <span className="font-mono-num">
                        Depart t{rec.departure_tick} → Arrive t{rec.expected_arrival_tick} ({rec.transit_ticks}t)
                      </span>
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <StatusPill tone={statusTone(rec.status)}>{formatLabel(rec.status)}</StatusPill>
                    {rec.needs_human_review && (
                      <StatusPill tone="critical">Human Review</StatusPill>
                    )}
                    <StatusPill tone={rec.mitigation_type === 'FULL_PREVENTION' ? 'ok' : 'watch'}>
                      {formatLabel(rec.mitigation_type)}
                    </StatusPill>
                  </div>
                </div>

                {/* Digital Twin Impact & Confidence Bar */}
                <div className="grid grid-cols-2 divide-x divide-[#E6EBF0] rounded border border-[#E6EBF0] bg-[#F7F9FB] sm:grid-cols-4">
                  <div className="p-3">
                    <div className="text-[11px] text-[#526071]">Stockout Probability</div>
                    <div className="font-mono-num mt-0.5 text-sm font-semibold text-[#0F172A]">
                      {pBefore.toFixed(0)}% →{' '}
                      <span className="text-[#0B6E4F]">{pAfter.toFixed(0)}%</span>
                    </div>
                  </div>
                  <div className="p-3">
                    <div className="text-[11px] text-[#526071]">Cover Added</div>
                    <div className="font-mono-num mt-0.5 text-sm font-semibold text-[#0F172A]">
                      +{(rec.hours_of_cover_added ?? 0).toFixed(1)} h
                    </div>
                  </div>
                  <div className="p-3">
                    <div className="text-[11px] text-[#526071]">Unmet Prevented</div>
                    <div className="font-mono-num mt-0.5 text-sm font-semibold text-[#0F172A]">
                      {formatLiters(rec.expected_unmet_reduction_liters)}
                    </div>
                  </div>
                  <div className="p-3">
                    <div className="text-[11px] text-[#526071]">Confidence / Urgency</div>
                    <div className="font-mono-num mt-0.5 text-sm font-semibold text-[#0F172A]">
                      {Math.round(rec.confidence_score * 100)}% ({rec.confidence_level}) · {rec.urgency_score.toFixed(1)}
                    </div>
                  </div>
                </div>

                <p className="max-w-4xl text-xs leading-relaxed text-[#334155]">
                  {rec.explanation}
                </p>

                {rec.failure_reason && (
                  <div className="rounded border border-[#FECDD3] bg-[#FFF1F2] px-3 py-2 text-xs text-[#9F1239]">
                    <strong>{rec.failure_code ?? 'Dispatch Failure'}:</strong> {rec.failure_reason}
                  </div>
                )}

                {rec.simulator_allocation_id !== null && (
                  <div className="font-mono-num text-xs text-[#065F46]">
                    Simulator Allocation #{rec.simulator_allocation_id} · Key:{' '}
                    <span translate="no">{rec.idempotency_key}</span>
                  </div>
                )}

                {/* Constraints & Alternatives Toggle + Audit Drawer Trigger */}
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded((prev) => ({
                        ...prev,
                        [rec.recommendation_id]: !isOpen,
                      }))
                    }
                    className="inline-flex cursor-pointer items-center gap-1 text-xs font-medium text-[#526071] hover:text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                  >
                    {isOpen ? (
                      <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
                    ) : (
                      <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
                    )}
                    5-Constraint Audit & Alternatives ({alts.length})
                  </button>

                  <button
                    type="button"
                    onClick={() => openInspector(rec.recommendation_id)}
                    className="inline-flex cursor-pointer items-center gap-1 text-xs font-medium text-[#1D4ED8] hover:underline focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                  >
                    <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                    Inspect Full Record & Audit Trail
                  </button>
                </div>

                {isOpen && constraints && (
                  <div className="grid grid-cols-1 gap-4 rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3.5 text-xs text-[#334155] sm:grid-cols-2">
                    <div className="space-y-1.5">
                      <div className="font-semibold text-[#0F172A]">
                        Binding Bottleneck: {formatLabel(constraints.bottleneck_constraint)}
                      </div>
                      <div className="font-mono-num flex justify-between">
                        <span>Route max shipment:</span>
                        <span>{formatLiters(constraints.route_max_shipment)}</span>
                      </div>
                      <div className="font-mono-num flex justify-between">
                        <span>Depot stock above reserve:</span>
                        <span>{formatLiters(constraints.depot_available_inventory)}</span>
                      </div>
                      <div className="font-mono-num flex justify-between">
                        <span>Depot dispatch remaining:</span>
                        <span>{formatLiters(constraints.depot_remaining_dispatch_capacity)}</span>
                      </div>
                      <div className="font-mono-num flex justify-between">
                        <span>Station effective ullage:</span>
                        <span>{formatLiters(constraints.station_effective_free_space)}</span>
                      </div>
                      <div className="font-mono-num flex justify-between">
                        <span>Calculated deficit needed:</span>
                        <span>{formatLiters(constraints.amount_needed)}</span>
                      </div>
                    </div>

                    <div className="space-y-1.5">
                      <div className="font-semibold text-[#0F172A]">Alternative Routes Evaluated</div>
                      {alts.length === 0 ? (
                        <div className="text-[#526071]">No viable alternative corridors found.</div>
                      ) : (
                        alts.map((alt, idx) => (
                          <div key={idx} className="rounded border border-[#E6EBF0] bg-white p-2">
                            <div className="flex justify-between font-medium text-[#0F172A]">
                              <span>
                                {alt.route_id} ({alt.source_depot_name})
                              </span>
                              <span className="font-mono-num">
                                {formatLiters(alt.quantity_liters)} · {alt.transit_ticks}t
                              </span>
                            </div>
                            <div className="mt-0.5 text-[#526071]">{alt.reason_not_chosen}</div>
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                )}

                {isProposed && (
                  <div className="flex flex-col gap-2 border-t border-[#E6EBF0] pt-3 sm:flex-row sm:items-center">
                    <label htmlFor={`note-${rec.recommendation_id}`} className="sr-only">
                      Operator audit note
                    </label>
                    <input
                      id={`note-${rec.recommendation_id}`}
                      type="text"
                      value={notesMap[rec.recommendation_id] ?? ''}
                      onChange={(e) =>
                        setNotesMap((prev) => ({
                          ...prev,
                          [rec.recommendation_id]: e.target.value,
                        }))
                      }
                      placeholder="Add operator justification or audit note…"
                      className="flex-1 rounded border border-[#D6DCE3] bg-white px-3 py-2 text-xs text-[#0F172A] placeholder:text-[#94A3B8] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                    />
                    <div className="flex items-center gap-2">
                      {isConfirmingDecline ? (
                        <>
                          <button
                            type="button"
                            disabled={isBusy}
                            onClick={async () => {
                              await onReject(
                                rec.recommendation_id,
                                notesMap[rec.recommendation_id],
                              );
                              setConfirmDeclineId(null);
                            }}
                            className="cursor-pointer rounded bg-[#BE123C] px-3.5 py-2 text-xs font-medium text-white transition-colors hover:bg-[#9F1239] disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                          >
                            Confirm Decline
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirmDeclineId(null)}
                            className="cursor-pointer rounded border border-[#D6DCE3] px-3 py-2 text-xs text-[#526071] hover:bg-[#F7F9FB] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                          >
                            Cancel
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          disabled={isBusy}
                          onClick={() => setConfirmDeclineId(rec.recommendation_id)}
                          className="cursor-pointer rounded border border-[#D6DCE3] bg-white px-3.5 py-2 text-xs font-medium text-[#0F172A] transition-colors hover:bg-[#F7F9FB] disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                        >
                          Decline
                        </button>
                      )}
                      <button
                        type="button"
                        disabled={isBusy}
                        onClick={() =>
                          onApprove(rec.recommendation_id, notesMap[rec.recommendation_id])
                        }
                        className="cursor-pointer rounded bg-[#0B6E4F] px-4 py-2 text-xs font-medium text-white transition-colors hover:bg-[#08553D] disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                      >
                        {isBusy ? 'Dispatching…' : 'Approve & Dispatch'}
                      </button>
                    </div>
                  </div>
                )}
              </Panel>
            );
          })}
        </div>
      )}

      {/* Plan Unmet Targets & Shared Capacity Summary */}
      {(unmetTargets.length > 0 || depotUtil.length > 0) && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          <Panel
            title={`Unserved At-Risk Targets (${unmetTargets.length})`}
            subtitle="Stations requiring fuel that could not be scheduled under current constraints"
          >
            {unmetTargets.length === 0 ? (
              <EmptyNotice title="No unserved at-risk targets" />
            ) : (
              <div className="divide-y divide-[#E6EBF0]">
                {unmetTargets.map((u, idx) => (
                  <div key={`${u.station_id}-${u.fuel_type}-${idx}`} className="py-3 first:pt-0 last:pb-0">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <StatusPill tone={statusTone(u.severity)}>{formatLabel(u.severity)}</StatusPill>
                        <span className="text-xs font-semibold text-[#0F172A]">
                          {u.station_name} · {formatLabel(String(u.fuel_type))}
                        </span>
                      </div>
                      <span className="font-mono-num text-xs text-[#BE123C]">{u.reason_code}</span>
                    </div>
                    <p className="mt-1 text-xs text-[#526071]">{u.explanation}</p>
                  </div>
                ))}
              </div>
            )}
          </Panel>

          <Panel
            title="Shared Depot Dispatch Capacity"
            subtitle="Per-tick loading bay utilization across the active optimization plan"
          >
            {depotUtil.length === 0 ? (
              <EmptyNotice title="No depot capacity commitments recorded" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-[#E6EBF0] text-[#526071]">
                      <th className="pb-2 font-medium">Depot</th>
                      <th className="pb-2 text-right font-medium">Cap / Tick</th>
                      <th className="pb-2 text-right font-medium">Committed</th>
                      <th className="pb-2 text-right font-medium">Planned</th>
                      <th className="pb-2 text-right font-medium">Remaining</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#E6EBF0]">
                    {depotUtil.map(([dpId, stats]) => (
                      <tr key={dpId}>
                        <td className="font-mono-num py-2 font-medium text-[#0F172A]">{dpId}</td>
                        <td className="font-mono-num py-2 text-right text-[#526071]">
                          {formatInt(stats.initial_capacity ?? stats.initial)}
                        </td>
                        <td className="font-mono-num py-2 text-right text-[#526071]">
                          {formatInt(stats.committed_prior ?? stats.committed)}
                        </td>
                        <td className="font-mono-num py-2 text-right font-medium text-[#0B6E4F]">
                          {formatInt(stats.allocated_this_plan ?? stats.allocated)}
                        </td>
                        <td className="font-mono-num py-2 text-right text-[#0F172A]">
                          {formatInt(stats.remaining_capacity ?? stats.remaining)}
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

      {/* Recommendation Inspector Drawer */}
      <ModalDrawer
        open={detailRec !== null || detailLoading}
        title={detailRec ? `Recommendation ${detailRec.recommendation_id}` : 'Loading recommendation…'}
        subtitle={
          detailRec
            ? `${detailRec.station_name} · ${formatLabel(String(detailRec.fuel_type))} · Created ${formatDateTime(detailRec.created_at)}`
            : undefined
        }
        onClose={() => setDetailRec(null)}
      >
        {detailLoading && !detailRec ? (
          <div className="py-8 text-center text-xs text-[#526071]">Loading full record…</div>
        ) : (
          detailRec && (
            <div className="space-y-5 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill tone={statusTone(detailRec.status)}>
                  {formatLabel(detailRec.status)}
                </StatusPill>
                <span className="font-mono-num text-[#526071]" translate="no">
                  Key: {detailRec.idempotency_key}
                </span>
              </div>

              <div className="rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3">
                <div className="font-semibold text-[#0F172A]">Decision Impact Summary</div>
                <p className="mt-1 text-[#334155]">{detailRec.impact_summary}</p>
                <p className="mt-2 text-[#526071]">{detailRec.explanation}</p>
              </div>

              {Object.keys(detailRec.signals ?? {}).length > 0 && (
                <div>
                  <h3 className="mb-2 font-semibold text-[#0F172A]">Decision-Time Signals</h3>
                  <div className="grid grid-cols-2 gap-2 rounded border border-[#E6EBF0] p-3">
                    {Object.entries(detailRec.signals).map(([k, v]) => (
                      <div key={k} className="flex justify-between gap-2">
                        <span className="text-[#526071]">{formatLabel(k)}</span>
                        <span className="font-mono-num font-medium text-[#0F172A]">
                          {typeof v === 'boolean' ? (v ? 'Yes' : 'No') : String(v)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div>
                <h3 className="mb-2 font-semibold text-[#0F172A]">
                  Immutable Audit Timeline ({detailEvents.length})
                </h3>
                {detailEvents.length === 0 ? (
                  <p className="text-[#526071]">No transition events logged for this record.</p>
                ) : (
                  <div className="divide-y divide-[#E6EBF0] rounded border border-[#E6EBF0]">
                    {detailEvents.map((ev) => (
                      <div key={ev.id} className="p-3">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-semibold text-[#0F172A]">
                            {formatLabel(ev.event_type)} ({ev.from_status ?? 'NEW'} → {ev.to_status})
                          </span>
                          <span className="font-mono-num text-[#526071]">
                            {formatDateTime(ev.timestamp)}
                          </span>
                        </div>
                        {ev.operator_notes && (
                          <p className="mt-1 text-[#526071]">{ev.operator_notes}</p>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )
        )}
      </ModalDrawer>
    </div>
  );
}
