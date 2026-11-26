import { useState } from 'react';
import { Table } from 'lucide-react';
import type { TwinSimulationResult } from '../types';
import { formatInt, formatLiters } from './ui';

interface TwinChartProps {
  simulation: TwinSimulationResult | null;
  arrivalTickOffset?: number;
}

export function TwinChart({ simulation, arrivalTickOffset }: TwinChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);

  const baseP50 = simulation?.baseline?.median_trajectory ?? [];
  const baseP10 = simulation?.baseline?.p10_trajectory ?? [];
  const baseP90 = simulation?.baseline?.p90_trajectory ?? [];
  const shipP50 = simulation?.with_shipment?.median_trajectory ?? [];
  const shipP10 = simulation?.with_shipment?.p10_trajectory ?? [];
  const shipP90 = simulation?.with_shipment?.p90_trajectory ?? [];

  const n = Math.max(baseP50.length, shipP50.length);
  if (!simulation || n < 2) {
    return (
      <div className="flex h-52 items-center justify-center rounded border border-dashed border-[#D6DCE3] bg-[#F7F9FB] text-xs text-[#526071]">
        Run a Monte Carlo simulation to compare baseline vs shipment trajectories.
      </div>
    );
  }

  const allVals = [
    ...baseP10,
    ...baseP50,
    ...baseP90,
    ...shipP10,
    ...shipP50,
    ...shipP90,
  ].filter((v) => typeof v === 'number' && Number.isFinite(v));

  const maxVal = Math.max(...allVals, 1000);
  const startTick = simulation.current_tick ?? 0;

  const w = 640;
  const h = 232;
  const padL = 56;
  const padR = 18;
  const padT = 18;
  const padB = 30;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const toX = (i: number) => padL + (i / (n - 1)) * plotW;
  const toY = (v: number) => padT + plotH - (Math.max(0, v) / maxVal) * plotH;

  const linePath = (arr: number[]) =>
    arr.map((v, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(' ');

  const bandPath = (low: number[], high: number[]) => {
    if (low.length !== high.length || low.length < 2) return '';
    const top = high
      .map((v, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`)
      .join(' ');
    const bot = [...low]
      .reverse()
      .map((v, idx) => {
        const i = low.length - 1 - idx;
        return `L${toX(i).toFixed(1)},${toY(v).toFixed(1)}`;
      })
      .join(' ');
    return `${top} ${bot} Z`;
  };

  const activeIdx = hoverIdx !== null && hoverIdx < n ? hoverIdx : null;
  const arrivalStep =
    typeof arrivalTickOffset === 'number' && arrivalTickOffset >= 1 && arrivalTickOffset <= n
      ? arrivalTickOffset - 1
      : null;

  return (
    <div className="space-y-3">
      <div className="w-full overflow-x-auto">
        <svg
          viewBox={`0 0 ${w} ${h}`}
          className="h-auto w-full min-w-[460px]"
          role="img"
          aria-label={`Monte Carlo inventory projection for ${simulation.station_name} ${simulation.fuel_type}`}
          onMouseLeave={() => setHoverIdx(null)}
        >
          {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
            const y = padT + plotH * (1 - frac);
            const val = maxVal * frac;
            return (
              <g key={frac}>
                <line x1={padL} y1={y} x2={w - padR} y2={y} stroke="#E6EBF0" strokeWidth={1} />
                <text
                  x={padL - 8}
                  y={y + 3.5}
                  textAnchor="end"
                  fontSize={10}
                  fontFamily="JetBrains Mono, monospace"
                  fill="#526071"
                >
                  {(val / 1000).toFixed(1)}k
                </text>
              </g>
            );
          })}

          {arrivalStep !== null && (
            <g>
              <line
                x1={toX(arrivalStep)}
                y1={padT}
                x2={toX(arrivalStep)}
                y2={padT + plotH}
                stroke="#1D4ED8"
                strokeWidth={1.2}
                strokeDasharray="3 3"
              />
              <text
                x={Math.min(w - padR - 4, toX(arrivalStep) + 4)}
                y={padT + 11}
                fontSize={9.5}
                fontFamily="JetBrains Mono, monospace"
                fill="#1D4ED8"
                fontWeight={600}
              >
                Arrival t+{arrivalStep + 1}
              </text>
            </g>
          )}

          {baseP10.length > 1 && baseP90.length > 1 && (
            <path d={bandPath(baseP10, baseP90)} fill="#FDA4AF" fillOpacity={0.22} />
          )}
          {baseP50.length > 1 && (
            <path
              d={linePath(baseP50)}
              fill="none"
              stroke="#BE123C"
              strokeWidth={1.8}
              strokeDasharray="5 4"
            />
          )}

          {shipP10.length > 1 && shipP90.length > 1 && (
            <path d={bandPath(shipP10, shipP90)} fill="#6EE7B7" fillOpacity={0.28} />
          )}
          {shipP50.length > 1 && (
            <path d={linePath(shipP50)} fill="none" stroke="#0B6E4F" strokeWidth={2.2} />
          )}

          {activeIdx !== null && (
            <g>
              <line
                x1={toX(activeIdx)}
                y1={padT}
                x2={toX(activeIdx)}
                y2={padT + plotH}
                stroke="#334155"
                strokeWidth={1}
              />
              {typeof baseP50[activeIdx] === 'number' && (
                <circle cx={toX(activeIdx)} cy={toY(baseP50[activeIdx])} r={3.5} fill="#BE123C" />
              )}
              {typeof shipP50[activeIdx] === 'number' && (
                <circle cx={toX(activeIdx)} cy={toY(shipP50[activeIdx])} r={3.5} fill="#0B6E4F" />
              )}
            </g>
          )}

          {/* Invisible hit columns for smooth tick scrubbing */}
          {Array.from({ length: n }).map((_, idx) => {
            const colW = plotW / n;
            return (
              <rect
                key={idx}
                x={toX(idx) - colW / 2}
                y={padT}
                width={colW}
                height={plotH}
                fill="transparent"
                onMouseEnter={() => setHoverIdx(idx)}
              />
            );
          })}

          <text x={padL} y={h - 8} fontSize={10} fontFamily="JetBrains Mono, monospace" fill="#526071">
            Tick {startTick + 1}
          </text>
          <text
            x={w - padR}
            y={h - 8}
            textAnchor="end"
            fontSize={10}
            fontFamily="JetBrains Mono, monospace"
            fill="#526071"
          >
            Tick {startTick + n} (+{n} ticks)
          </text>
        </svg>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[#E6EBF0] pt-2.5 text-xs text-[#526071]">
        <div className="flex flex-wrap items-center gap-4">
          <span className="inline-flex items-center gap-2">
            <span className="inline-block h-0.5 w-4 border-t-2 border-dashed border-[#BE123C]" />
            Baseline P50 (P10–P90 band)
          </span>
          <span className="inline-flex items-center gap-2">
            <span className="inline-block h-0.5 w-4 bg-[#0B6E4F]" />
            With Shipment P50 (P10–P90 band)
          </span>
        </div>

        <div className="flex items-center gap-3">
          {activeIdx !== null && (
            <span className="font-mono-num text-[#0F172A]">
              Tick {startTick + activeIdx + 1}: Base {formatLiters(baseP50[activeIdx])} · Ship{' '}
              {formatLiters(shipP50[activeIdx])}
            </span>
          )}
          <button
            type="button"
            onClick={() => setShowTable((s) => !s)}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded border border-[#D6DCE3] bg-[#F7F9FB] px-2.5 py-1 text-xs font-medium text-[#0F172A] transition-colors hover:bg-[#EEF1F4] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
          >
            <Table className="h-3.5 w-3.5" aria-hidden="true" />
            {showTable ? 'Hide data table' : 'Percentile table'}
          </button>
        </div>
      </div>

      {showTable && (
        <div className="max-h-60 overflow-auto rounded border border-[#D6DCE3]">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 border-b border-[#D6DCE3] bg-[#F7F9FB] text-[#526071]">
              <tr>
                <th className="px-3 py-2 font-medium">Tick</th>
                <th className="px-3 py-2 text-right font-medium">Base P10</th>
                <th className="px-3 py-2 text-right font-medium">Base P50</th>
                <th className="px-3 py-2 text-right font-medium">Base P90</th>
                <th className="px-3 py-2 text-right font-medium">Ship P10</th>
                <th className="px-3 py-2 text-right font-medium">Ship P50</th>
                <th className="px-3 py-2 text-right font-medium">Ship P90</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#E6EBF0]">
              {Array.from({ length: n }).map((_, i) => (
                <tr key={i}>
                  <td className="font-mono-num px-3 py-1.5 text-[#0F172A]">{startTick + i + 1}</td>
                  <td className="font-mono-num px-3 py-1.5 text-right text-[#526071]">
                    {formatInt(baseP10[i])}
                  </td>
                  <td className="font-mono-num px-3 py-1.5 text-right font-medium text-[#BE123C]">
                    {formatInt(baseP50[i])}
                  </td>
                  <td className="font-mono-num px-3 py-1.5 text-right text-[#526071]">
                    {formatInt(baseP90[i])}
                  </td>
                  <td className="font-mono-num px-3 py-1.5 text-right text-[#526071]">
                    {formatInt(shipP10[i])}
                  </td>
                  <td className="font-mono-num px-3 py-1.5 text-right font-medium text-[#0B6E4F]">
                    {formatInt(shipP50[i])}
                  </td>
                  <td className="font-mono-num px-3 py-1.5 text-right text-[#526071]">
                    {formatInt(shipP90[i])}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
