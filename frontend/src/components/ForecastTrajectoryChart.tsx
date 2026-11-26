import { useState } from 'react';
import type { ForecastPoint, InventoryTrajectoryPoint } from '../types';
import { formatLiters } from './ui';

interface TrajectoryChartProps {
  inventoryPoints: InventoryTrajectoryPoint[];
  capacityLiters: number;
  forecastPoints?: ForecastPoint[];
}

export function TrajectoryChart({
  inventoryPoints,
  capacityLiters,
  forecastPoints = [],
}: TrajectoryChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  if (inventoryPoints.length < 2) {
    return (
      <div className="flex h-44 items-center justify-center rounded border border-dashed border-[#D6DCE3] bg-[#F7F9FB] text-xs text-[#526071]">
        No deterministic projection trajectory available.
      </div>
    );
  }

  const n = inventoryPoints.length;
  const maxY = Math.max(
    capacityLiters,
    ...inventoryPoints.map((p) => p.projected_inventory),
    1000,
  );

  const w = 620;
  const h = 190;
  const padL = 54;
  const padR = 16;
  const padT = 16;
  const padB = 28;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const toX = (i: number) => padL + (i / (n - 1)) * plotW;
  const toY = (v: number) => padT + plotH - (Math.max(0, v) / maxY) * plotH;

  const invPath = inventoryPoints
    .map(
      (p, i) =>
        `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(p.projected_inventory).toFixed(1)}`,
    )
    .join(' ');

  const areaPath = `${invPath} L${toX(n - 1).toFixed(1)},${(padT + plotH).toFixed(1)} L${toX(0).toFixed(1)},${(padT + plotH).toFixed(1)} Z`;

  const capY = toY(capacityLiters);
  const active = hoverIdx !== null && hoverIdx < n ? inventoryPoints[hoverIdx] : null;
  const activeFc = hoverIdx !== null && hoverIdx < forecastPoints.length ? forecastPoints[hoverIdx] : null;

  return (
    <div className="space-y-2">
      <div className="w-full overflow-x-auto">
        <svg
          viewBox={`0 0 ${w} ${h}`}
          className="h-auto w-full min-w-[440px]"
          role="img"
          aria-label="Deterministic projected tank inventory trajectory"
          onMouseLeave={() => setHoverIdx(null)}
        >
          {[0, 0.5, 1].map((frac) => {
            const y = padT + plotH * (1 - frac);
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
                  {((maxY * frac) / 1000).toFixed(1)}k
                </text>
              </g>
            );
          })}

          <line
            x1={padL}
            y1={capY}
            x2={w - padR}
            y2={capY}
            stroke="#94A3B8"
            strokeWidth={1}
            strokeDasharray="4 3"
          />

          <path d={areaPath} fill="#1D4ED8" fillOpacity={0.08} />
          <path d={invPath} fill="none" stroke="#1D4ED8" strokeWidth={2} />

          {inventoryPoints.map((p, idx) => {
            if (p.incoming_liters > 0) {
              return (
                <circle
                  key={`arr-${idx}`}
                  cx={toX(idx)}
                  cy={toY(p.projected_inventory)}
                  r={4}
                  fill="#0B6E4F"
                  stroke="#FFFFFF"
                  strokeWidth={1.2}
                />
              );
            }
            if (p.is_stockout) {
              return (
                <circle
                  key={`so-${idx}`}
                  cx={toX(idx)}
                  cy={toY(p.projected_inventory)}
                  r={3.5}
                  fill="#BE123C"
                />
              );
            }
            return null;
          })}

          {hoverIdx !== null && active && (
            <line
              x1={toX(hoverIdx)}
              y1={padT}
              x2={toX(hoverIdx)}
              y2={padT + plotH}
              stroke="#334155"
              strokeWidth={1}
            />
          )}

          {inventoryPoints.map((_, idx) => {
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

          <text x={padL} y={h - 6} fontSize={10} fontFamily="JetBrains Mono, monospace" fill="#526071">
            Tick {inventoryPoints[0].tick}
          </text>
          <text
            x={w - padR}
            y={h - 6}
            textAnchor="end"
            fontSize={10}
            fontFamily="JetBrains Mono, monospace"
            fill="#526071"
          >
            Tick {inventoryPoints[n - 1].tick}
          </text>
        </svg>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[#526071]">
        <div className="flex flex-wrap items-center gap-4">
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-3.5 bg-[#1D4ED8]" />
            Projected tank level
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-[#0B6E4F]" />
            Scheduled arrival
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-[#BE123C]" />
            Stockout tick
          </span>
        </div>

        {active && (
          <span className="font-mono-num text-[#0F172A]">
            Tick {active.tick}: {formatLiters(active.projected_inventory)} (
            {(active.fill_ratio * 100).toFixed(0)}%) · Burn {formatLiters(active.forecast_demand_liters)}
            {active.incoming_liters > 0 ? ` · +${formatLiters(active.incoming_liters)} in` : ''}
            {activeFc ? ` · ${Math.round(activeFc.forecast_liters_per_hour)} L/h` : ''}
          </span>
        )}
      </div>
    </div>
  );
}
