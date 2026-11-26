import { useCallback, useEffect, useState } from 'react';
import { Play, RefreshCw } from 'lucide-react';
import type { FuelApiClient } from '../api';
import { TrajectoryChart } from '../components/ForecastTrajectoryChart';
import { TwinChart } from '../components/TwinChart';
import {
  EmptyNotice,
  formatDecimal,
  formatInt,
  formatLabel,
  formatLiters,
  MeterBar,
  Panel,
  StatusPill,
  statusTone,
} from '../components/ui';
import type {
  RiskResponse,
  StationForecastResponse,
  StationRiskResponse,
  TwinSimulationResult,
} from '../types';

interface IntelligenceTwinViewProps {
  client: FuelApiClient;
  risk: RiskResponse | null;
  currentTick: number;
  selectedStationId?: string;
  selectedFuelType?: string;
  onSelectTarget: (stationId: string, fuelType: string) => void;
}

export function IntelligenceTwinView({
  client,
  risk,
  currentTick,
  selectedStationId,
  selectedFuelType,
  onSelectTarget,
}: IntelligenceTwinViewProps) {
  const [sevFilter, setSevFilter] = useState<'ALL' | 'CRITICAL' | 'WATCH'>('ALL');
  const stationIds = Object.keys(risk?.station_risks ?? {});

  const [stationId, setStationId] = useState<string>(selectedStationId || stationIds[0] || '');
  const [fuelType, setFuelType] = useState<string>(selectedFuelType || 'DIESEL');
  const [shipmentQty, setShipmentQty] = useState<number>(10000);
  const [arrivalOffset, setArrivalOffset] = useState<number>(2);
  const [trials, setTrials] = useState<number>(300);
  const [seed, setSeed] = useState<number>(42);

  const [stationRiskDetail, setStationRiskDetail] = useState<StationRiskResponse | null>(null);
  const [stationForecastDetail, setStationForecastDetail] = useState<StationForecastResponse | null>(
    null,
  );
  const [simResult, setSimResult] = useState<TwinSimulationResult | null>(null);
  const [simLoading, setSimLoading] = useState(false);
  const [simError, setSimError] = useState<string | null>(null);

  useEffect(() => {
    if (selectedStationId && selectedStationId !== stationId) {
      setStationId(selectedStationId);
    } else if (!stationId && stationIds.length > 0) {
      setStationId(stationIds[0]);
    }
  }, [selectedStationId, stationIds.join(',')]);

  useEffect(() => {
    if (selectedFuelType && selectedFuelType !== fuelType) {
      setFuelType(selectedFuelType.toUpperCase());
    }
  }, [selectedFuelType]);

  // Fetch per-station deep-dive endpoints (/risks/{id} and /forecasts/{id}) + baseline twin when station changes
  useEffect(() => {
    if (!stationId) return;
    let cancelled = false;
    Promise.allSettled([
      client.getStationRisk(stationId),
      client.getStationForecast(stationId),
    ]).then(([rRes, fRes]) => {
      if (cancelled) return;
      if (rRes.status === 'fulfilled') setStationRiskDetail(rRes.value);
      if (fRes.status === 'fulfilled') setStationForecastDetail(fRes.value);
    });
    return () => {
      cancelled = true;
    };
  }, [client, stationId, currentTick]);

  const runSimulation = useCallback(
    async (
      targetStation = stationId,
      targetFuel = fuelType,
      qty = shipmentQty,
      offset = arrivalOffset,
    ) => {
      if (!targetStation) return;
      setSimLoading(true);
      setSimError(null);
      try {
        const res = await client.simulateTwin({
          station_id: targetStation,
          fuel_type: targetFuel.toUpperCase(),
          candidate_shipment_quantity: qty,
          candidate_arrival_tick: currentTick + Math.max(1, offset),
          trials,
          seed,
        });
        setSimResult(res.simulation ?? null);
      } catch (err) {
        setSimError(err instanceof Error ? err.message : 'Simulation failed');
      } finally {
        setSimLoading(false);
      }
    },
    [client, stationId, fuelType, shipmentQty, arrivalOffset, currentTick, trials, seed],
  );

  const runBaselineTwin = async () => {
    if (!stationId) return;
    setSimLoading(true);
    setSimError(null);
    try {
      const res = await client.getStationFuelTwin(stationId, fuelType, trials, seed);
      setSimResult(res.simulation ?? null);
    } catch (err) {
      setSimError(err instanceof Error ? err.message : 'Baseline simulation failed');
    } finally {
      setSimLoading(false);
    }
  };

  const fuelTypes = Array.from(
    new Set(stationIds.flatMap((id) => Object.keys(risk?.station_risks?.[id] ?? {}))),
  );
  const alerts = (risk?.active_alerts ?? []).filter((a) =>
    sevFilter === 'ALL' ? true : a.severity === sevFilter,
  );

  const activeRisk =
    stationRiskDetail?.risks?.[fuelType] ?? risk?.station_risks?.[stationId]?.[fuelType] ?? null;
  const activeForecast = stationForecastDetail?.forecasts?.[fuelType] ?? null;

  const depotIds = Object.keys(risk?.depot_risks ?? {});

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
        {/* Left Column: Readiness Heatmap + Depot Reserve Risks */}
        <div className="space-y-5 xl:col-span-5">
          <Panel
            title="Station Readiness Matrix"
            subtitle="Select any station and fuel cell to inspect trajectory and simulate a shipment"
          >
            {stationIds.length === 0 ? (
              <EmptyNotice title="No station risk telemetry yet" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-separate border-spacing-1.5 text-left text-xs">
                  <thead>
                    <tr className="text-[#526071]">
                      <th className="pb-1 font-medium">Station</th>
                      {fuelTypes.map((f) => (
                        <th key={f} className="pb-1 text-center font-medium">
                          {formatLabel(f)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {stationIds.map((stId) => {
                      const fuels = risk?.station_risks?.[stId] ?? {};
                      const first = Object.values(fuels)[0];
                      const stName = first?.station_name || stId;

                      return (
                        <tr key={stId}>
                          <td className="pr-2 font-medium text-[#0F172A]">{stName}</td>
                          {fuelTypes.map((f) => {
                            const cell = fuels[f];
                            if (!cell) {
                              return (
                                <td
                                  key={f}
                                  className="rounded bg-[#F7F9FB] px-2.5 py-2 text-center text-[#94A3B8]"
                                >
                                  —
                                </td>
                              );
                            }
                            const fill = (cell.current_fill_ratio ?? 0) * 100;
                            const eta = cell.hours_to_stockout;
                            const sev = cell.severity;
                            const isSelected = stationId === stId && fuelType === f.toUpperCase();

                            const cellStyle =
                              sev === 'CRITICAL'
                                ? 'bg-[#FFF1F2] text-[#9F1239] border-[#FECDD3] hover:bg-[#FFE4E6]'
                                : sev === 'WATCH'
                                  ? 'bg-[#FFFBEB] text-[#92400E] border-[#FDE68A] hover:bg-[#FEF3C7]'
                                  : 'bg-[#ECFDF5] text-[#065F46] border-[#A7F3D0] hover:bg-[#D1FAE5]';

                            return (
                              <td key={f}>
                                <button
                                  type="button"
                                  onClick={() => {
                                    const upFuel = f.toUpperCase();
                                    setStationId(stId);
                                    setFuelType(upFuel);
                                    onSelectTarget(stId, upFuel);
                                    runSimulation(stId, upFuel, shipmentQty, arrivalOffset);
                                  }}
                                  className={`w-full cursor-pointer rounded border px-2.5 py-2 text-center transition-colors focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none ${cellStyle} ${
                                    isSelected ? 'ring-2 ring-[#1D4ED8] ring-offset-1' : ''
                                  }`}
                                >
                                  <div className="font-mono-num font-semibold">
                                    {fill.toFixed(0)}%
                                  </div>
                                  <div className="font-mono-num text-[11px] opacity-85">
                                    {typeof eta === 'number' ? `${eta.toFixed(1)}h` : 'Safe'}
                                  </div>
                                </button>
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {/* Depot Reserve Risks */}
          <Panel
            title="Upstream Depot Reserve Risks"
            subtitle="Effective dispatchable fuel accounting for pending dispatches and arrivals"
          >
            {depotIds.length === 0 ? (
              <EmptyNotice title="No depot risk metrics available" />
            ) : (
              <div className="space-y-3">
                {depotIds.map((dpId) => {
                  const fuels = risk?.depot_risks?.[dpId] ?? {};
                  const first = Object.values(fuels)[0];
                  return (
                    <div key={dpId} className="rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3">
                      <div className="mb-2 flex items-center justify-between text-xs">
                        <span className="font-semibold text-[#0F172A]">
                          {first?.depot_name || dpId}
                        </span>
                        <span className="font-mono-num text-[#526071]">{dpId}</span>
                      </div>
                      <div className="grid grid-cols-3 gap-2 text-xs">
                        {Object.entries(fuels).map(([f, dr]) => (
                          <div key={f} className="rounded border border-[#E6EBF0] bg-white p-2">
                            <div className="flex items-center justify-between gap-1">
                              <span className="font-medium text-[#0F172A]">{formatLabel(f)}</span>
                              <StatusPill tone={statusTone(dr.severity)}>
                                {(dr.current_fill_ratio * 100).toFixed(0)}%
                              </StatusPill>
                            </div>
                            <div className="font-mono-num mt-1 text-[11px] text-[#526071]">
                              Avail {formatLiters(dr.effective_available_liters)}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </Panel>
        </div>

        {/* Right Column: Station Deep-Dive + Digital Twin Workbench */}
        <div className="space-y-5 xl:col-span-7">
          <Panel
            title={
              activeRisk
                ? `${activeRisk.station_name} · ${formatLabel(fuelType)} Diagnostics`
                : 'Station Risk & Forecast Inspector'
            }
            subtitle={
              activeRisk
                ? activeRisk.explanation
                : 'Select a station cell to inspect deterministic trajectory and risk drivers'
            }
            actions={
              activeRisk && (
                <div className="flex items-center gap-2">
                  {activeRisk.is_unavoidable_stockout && (
                    <StatusPill tone="critical">Unavoidable shortage</StatusPill>
                  )}
                  <StatusPill tone={statusTone(activeRisk.severity)}>
                    {formatLabel(activeRisk.severity)}
                  </StatusPill>
                </div>
              )
            }
          >
            {activeRisk ? (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3 rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3 sm:grid-cols-4">
                  <div>
                    <div className="text-[11px] text-[#526071]">Burn Rate</div>
                    <div className="font-mono-num text-sm font-semibold text-[#0F172A]">
                      {formatInt(activeRisk.burn_rate_liters_per_hour)} L/h
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-[#526071]">Runout (No Arrivals)</div>
                    <div className="font-mono-num text-sm font-semibold text-[#0F172A]">
                      {typeof activeRisk.runout_hours_no_arrivals === 'number'
                        ? `${activeRisk.runout_hours_no_arrivals.toFixed(1)} h`
                        : '—'}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-[#526071]">In-Transit / Pending</div>
                    <div className="font-mono-num text-sm font-semibold text-[#0F172A]">
                      {formatLiters(
                        activeRisk.drivers.in_transit_liters_total +
                          activeRisk.drivers.pending_liters_total,
                      )}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] text-[#526071]">Routes / Confidence</div>
                    <div className="font-mono-num text-sm font-semibold text-[#0F172A]">
                      {activeRisk.drivers.available_supply_routes_count}/
                      {activeRisk.drivers.total_supply_routes_count} ·{' '}
                      {Math.round(activeRisk.confidence * 100)}%
                    </div>
                  </div>
                </div>

                {activeForecast && (
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-[#E6EBF0] px-3 py-2 text-xs">
                    <span className="text-[#526071]">
                      Forecaster:{' '}
                      <span className="font-mono-num font-medium text-[#0F172A]">
                        {activeForecast.model_used}
                      </span>{' '}
                      · MAE {formatDecimal(activeForecast.metrics.mae, ' L/t')} · MAPE{' '}
                      {formatDecimal(activeForecast.metrics.mape, '%')} · σ{' '}
                      {formatDecimal(activeForecast.metrics.residual_std)}
                    </span>
                    <StatusPill tone={activeForecast.anomaly.is_spike ? 'watch' : 'ok'}>
                      {activeForecast.anomaly.is_spike
                        ? `Spike ${activeForecast.anomaly.spike_ratio.toFixed(2)}×`
                        : 'Demand normal'}
                    </StatusPill>
                  </div>
                )}

                <TrajectoryChart
                  inventoryPoints={activeRisk.trajectory}
                  capacityLiters={activeRisk.capacity_liters}
                  forecastPoints={activeForecast?.trajectory}
                />
              </div>
            ) : (
              <EmptyNotice title="Select a station to load deterministic risk and forecast telemetry" />
            )}
          </Panel>

          {/* Monte Carlo Digital Twin Workbench */}
          <Panel
            title="Monte Carlo Digital Twin Simulator"
            subtitle="Paired stochastic trials comparing baseline vs candidate dispatch arrival"
            actions={
              simResult && (
                <StatusPill
                  tone={
                    simResult.with_shipment.stockout_probability <
                    simResult.baseline.stockout_probability
                      ? 'ok'
                      : 'neutral'
                  }
                >
                  Stockout {(simResult.baseline.stockout_probability * 100).toFixed(1)}% →{' '}
                  {(simResult.with_shipment.stockout_probability * 100).toFixed(1)}%
                </StatusPill>
              )
            }
          >
            <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-6">
              <div className="sm:col-span-2">
                <label htmlFor="twin-station" className="mb-1 block text-xs font-medium text-[#526071]">
                  Station
                </label>
                <select
                  id="twin-station"
                  value={stationId}
                  onChange={(e) => setStationId(e.target.value)}
                  className="w-full rounded border border-[#D6DCE3] bg-white px-2.5 py-1.5 text-xs text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                >
                  {stationIds.map((id) => (
                    <option key={id} value={id}>
                      {risk?.station_risks?.[id]?.[fuelType]?.station_name ?? id} ({id})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label htmlFor="twin-fuel" className="mb-1 block text-xs font-medium text-[#526071]">
                  Fuel
                </label>
                <select
                  id="twin-fuel"
                  value={fuelType}
                  onChange={(e) => setFuelType(e.target.value)}
                  className="w-full rounded border border-[#D6DCE3] bg-white px-2.5 py-1.5 text-xs text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                >
                  <option value="DIESEL">Diesel</option>
                  <option value="PETROL">Petrol</option>
                  <option value="OCTANE">Octane</option>
                </select>
              </div>

              <div>
                <label htmlFor="twin-qty" className="mb-1 block text-xs font-medium text-[#526071]">
                  Volume (L)
                </label>
                <input
                  id="twin-qty"
                  type="number"
                  step={1000}
                  min={0}
                  value={shipmentQty}
                  onChange={(e) => setShipmentQty(Math.max(0, Number(e.target.value)))}
                  className="font-mono-num w-full rounded border border-[#D6DCE3] px-2.5 py-1.5 text-xs text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                />
              </div>

              <div>
                <label htmlFor="twin-offset" className="mb-1 block text-xs font-medium text-[#526071]">
                  Arrival (+ticks)
                </label>
                <input
                  id="twin-offset"
                  type="number"
                  min={1}
                  max={24}
                  value={arrivalOffset}
                  onChange={(e) => setArrivalOffset(Math.max(1, Number(e.target.value)))}
                  className="font-mono-num w-full rounded border border-[#D6DCE3] px-2.5 py-1.5 text-xs text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                />
              </div>

              <div>
                <label htmlFor="twin-trials" className="mb-1 block text-xs font-medium text-[#526071]">
                  Trials / Seed
                </label>
                <div className="flex gap-1">
                  <input
                    id="twin-trials"
                    type="number"
                    min={50}
                    max={2000}
                    step={50}
                    value={trials}
                    aria-label="Monte Carlo trials"
                    onChange={(e) => setTrials(Math.min(2000, Math.max(50, Number(e.target.value))))}
                    className="font-mono-num w-full rounded border border-[#D6DCE3] px-2 py-1.5 text-xs text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                  />
                  <input
                    type="number"
                    min={0}
                    value={seed}
                    aria-label="Random seed"
                    onChange={(e) => setSeed(Math.max(0, Number(e.target.value)))}
                    className="font-mono-num w-14 rounded border border-[#D6DCE3] px-2 py-1.5 text-xs text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                  />
                </div>
              </div>
            </div>

            <div className="mb-4 flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={simLoading || !stationId}
                onClick={() => runSimulation()}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded bg-[#0B6E4F] px-3.5 py-2 text-xs font-medium text-white transition-colors hover:bg-[#08553D] disabled:cursor-not-allowed disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
              >
                <Play className="h-3.5 w-3.5" aria-hidden="true" />
                {simLoading ? 'Running trials…' : `Simulate +${formatLiters(shipmentQty)} @ t${currentTick + arrivalOffset}`}
              </button>
              <button
                type="button"
                disabled={simLoading || !stationId}
                onClick={runBaselineTwin}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded border border-[#D6DCE3] bg-white px-3 py-2 text-xs font-medium text-[#0F172A] transition-colors hover:bg-[#F7F9FB] disabled:cursor-not-allowed disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
              >
                <RefreshCw className="h-3.5 w-3.5 text-[#526071]" aria-hidden="true" />
                Baseline only
              </button>
            </div>

            {simError && (
              <div
                role="alert"
                className="mb-3 rounded border border-[#FECDD3] bg-[#FFF1F2] px-3 py-2 text-xs text-[#9F1239]"
              >
                {simError}
              </div>
            )}

            {simResult && (
              <div className="mb-4 grid grid-cols-2 gap-3 rounded border border-[#E6EBF0] bg-[#F7F9FB] p-3 text-xs sm:grid-cols-4">
                <div>
                  <div className="text-[11px] text-[#526071]">Relative Risk Drop</div>
                  <div className="font-mono-num mt-0.5 text-sm font-semibold text-[#0B6E4F]">
                    {simResult.relative_risk_reduction_percent.toFixed(1)}%
                  </div>
                </div>
                <div>
                  <div className="text-[11px] text-[#526071]">Unmet Prevented</div>
                  <div className="font-mono-num mt-0.5 text-sm font-semibold text-[#0F172A]">
                    {formatLiters(simResult.expected_unmet_reduction_liters)}
                  </div>
                </div>
                <div>
                  <div className="text-[11px] text-[#526071]">Baseline Unmet (95% CI)</div>
                  <div className="font-mono-num mt-0.5 text-xs font-medium text-[#0F172A]">
                    {formatInt(simResult.baseline.expected_unmet_liters)} L [
                    {formatInt(simResult.baseline.unmet_ci.lower)}–
                    {formatInt(simResult.baseline.unmet_ci.upper)}]
                  </div>
                </div>
                <div>
                  <div className="text-[11px] text-[#526071]">Trials / Residual σ</div>
                  <div className="font-mono-num mt-0.5 text-xs font-medium text-[#0F172A]">
                    {simResult.trials_count} · {simResult.residual_std.toFixed(1)} L/t
                  </div>
                </div>
              </div>
            )}

            <TwinChart simulation={simResult} arrivalTickOffset={arrivalOffset} />
          </Panel>
        </div>
      </div>

      {/* Priority Alerts Table */}
      <Panel
        title={`Fleet Risk Alerts (${alerts.length})`}
        subtitle="Urgency-sorted station and commodity risk triggers"
        actions={
          <div className="flex items-center gap-1 rounded border border-[#D6DCE3] bg-[#F7F9FB] p-0.5">
            {(
              [
                ['ALL', 'All'],
                ['CRITICAL', 'Critical'],
                ['WATCH', 'Watch'],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setSevFilter(key)}
                className={`cursor-pointer rounded px-2.5 py-1 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none ${
                  sevFilter === key ? 'bg-white text-[#0F172A] shadow-2xs' : 'text-[#526071] hover:text-[#0F172A]'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        }
      >
        {alerts.length === 0 ? (
          <EmptyNotice title="No matching alerts" hint="All stations match current filter thresholds." />
        ) : (
          <div className="divide-y divide-[#E6EBF0]">
            {alerts.map((a, idx) => {
              const fill = (a.current_fill_ratio ?? 0) * 100;
              const tone = statusTone(a.severity);
              const fuelStr = String(a.fuel_type).toUpperCase();

              return (
                <div
                  key={`${a.station_id}-${fuelStr}-${idx}`}
                  className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusPill tone={tone}>{formatLabel(a.severity)}</StatusPill>
                      <span className="text-sm font-semibold text-[#0F172A]">
                        {a.station_name || a.station_id}
                      </span>
                      <span className="text-xs text-[#526071]">{formatLabel(fuelStr)}</span>
                      <span className="font-mono-num text-xs text-[#526071]">
                        Urgency {a.urgency_score.toFixed(1)}
                      </span>
                    </div>
                    <div className="flex items-center gap-3">
                      <div className="w-28 shrink-0">
                        <MeterBar percent={fill} tone={tone} label={`${a.station_name} fill`} />
                      </div>
                      <span className="font-mono-num text-xs font-medium text-[#0F172A]">
                        {fill.toFixed(0)}%
                      </span>
                      <span className="text-xs text-[#526071]">{a.explanation}</span>
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-3">
                    <span className="font-mono-num text-xs text-[#526071]">
                      {typeof a.hours_to_stockout === 'number'
                        ? `Stockout in ${a.hours_to_stockout.toFixed(1)}h`
                        : 'Beyond horizon'}
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        setStationId(a.station_id);
                        setFuelType(fuelStr);
                        runSimulation(a.station_id, fuelStr, shipmentQty, arrivalOffset);
                      }}
                      className="cursor-pointer rounded border border-[#D6DCE3] bg-[#F7F9FB] px-2.5 py-1.5 text-xs font-medium text-[#0F172A] transition-colors hover:bg-[#EEF1F4] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
                    >
                      Simulate in Twin
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
