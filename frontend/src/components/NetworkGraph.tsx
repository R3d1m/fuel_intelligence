import type {
  AllocationSnapshot,
  DepotSnapshot,
  RiskResponse,
  RouteSnapshot,
  StationSnapshot,
} from '../types';
import { formatInt } from './ui';

interface NetworkGraphProps {
  depots: DepotSnapshot[];
  stations: StationSnapshot[];
  routes: RouteSnapshot[];
  allocations?: AllocationSnapshot[];
  risk: RiskResponse | null;
  selectedStationId?: string;
  onSelectStation?: (stationId: string) => void;
}

function avgFill(inv: Record<string, number> = {}, cap: Record<string, number> = {}): number {
  const totalInv = Object.values(inv).reduce((a, b) => a + b, 0);
  const totalCap = Object.values(cap).reduce((a, b) => a + b, 0);
  return totalCap > 0 ? (totalInv / totalCap) * 100 : 0;
}

function worstStationSeverity(stationId: string, risk: RiskResponse | null): string {
  const fuels = risk?.station_risks?.[stationId];
  if (!fuels) return 'OK';
  const sevs = Object.values(fuels).map((f) => f.severity);
  if (sevs.includes('CRITICAL')) return 'CRITICAL';
  if (sevs.includes('WATCH')) return 'WATCH';
  return 'OK';
}

export function NetworkGraph({
  depots,
  stations,
  routes,
  allocations = [],
  risk,
  selectedStationId,
  onSelectStation,
}: NetworkGraphProps) {
  if (depots.length === 0 && stations.length === 0) {
    return (
      <div className="flex h-60 items-center justify-center rounded border border-dashed border-[#D6DCE3] bg-[#F7F9FB] text-xs text-[#526071]">
        Waiting for initial simulator snapshot…
      </div>
    );
  }

  const width = 700;
  const rows = Math.max(depots.length, stations.length, 1);
  const height = Math.max(260, rows * 88 + 32);

  const depotPos = new Map<string, { x: number; y: number }>();
  depots.forEach((d, i) => {
    const spacing = height / (depots.length + 1);
    depotPos.set(d.id, { x: 118, y: spacing * (i + 1) });
  });

  const stationPos = new Map<string, { x: number; y: number }>();
  stations.forEach((s, i) => {
    const spacing = height / (stations.length + 1);
    stationPos.set(s.id, { x: 582, y: spacing * (i + 1) });
  });

  // Group active IN_TRANSIT / PENDING allocations by route_id
  const activeByRoute = new Map<string, { count: number; liters: number }>();
  for (const a of allocations) {
    if (a.status === 'IN_TRANSIT' || a.status === 'PENDING') {
      const prev = activeByRoute.get(a.route_id) ?? { count: 0, liters: 0 };
      activeByRoute.set(a.route_id, {
        count: prev.count + 1,
        liters: prev.liters + (a.quantity ?? 0),
      });
    }
  }

  return (
    <div className="w-full overflow-x-auto">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-auto w-full min-w-[540px]"
        role="img"
        aria-label="Fuel supply network topology connecting depots to stations"
      >
        <text x={20} y={18} fontSize={10.5} fontWeight={600} fill="#526071">
          Supply Depots ({depots.length})
        </text>
        <text x={width - 20} y={18} textAnchor="end" fontSize={10.5} fontWeight={600} fill="#526071">
          Retail Stations ({stations.length})
        </text>

        {/* Routes */}
        {routes.map((r) => {
          const from = depotPos.get(r.source_depot_id);
          const to = stationPos.get(r.destination_station_id);
          if (!from || !to) return null;

          const x1 = from.x + 96;
          const y1 = from.y;
          const x2 = to.x - 96;
          const y2 = to.y;
          const midX = (x1 + x2) / 2;
          const midY = (y1 + y2) / 2;

          const active = activeByRoute.get(r.id);
          const status = r.status.toUpperCase();
          const isDisrupted = status === 'DISRUPTED';
          const isCongested = status === 'CONGESTED' || status === 'SLOW';
          const isSelectedRoute = selectedStationId === r.destination_station_id;

          const stroke = isDisrupted
            ? '#BE123C'
            : isCongested
              ? '#B45309'
              : active
                ? '#1D4ED8'
                : isSelectedRoute
                  ? '#0B6E4F'
                  : '#CBD5E1';

          const labelText = isDisrupted
            ? 'Cut'
            : active
              ? `${r.transit_ticks}t · ${formatInt(active.liters / 1000)}k`
              : isCongested
                ? `Slow ${r.transit_ticks}t`
                : `${r.transit_ticks}t`;

          const pillW = Math.max(46, labelText.length * 6.2 + 12);

          return (
            <g key={r.id}>
              <line
                x1={x1}
                y1={y1}
                x2={x2}
                y2={y2}
                stroke={stroke}
                strokeWidth={isDisrupted || active || isSelectedRoute ? 2 : 1.3}
                strokeDasharray={isDisrupted ? '5 4' : isCongested ? '3 3' : undefined}
              />
              <rect
                x={midX - pillW / 2}
                y={midY - 9.5}
                width={pillW}
                height={19}
                rx={3}
                fill={active ? '#EFF6FF' : isDisrupted ? '#FFF1F2' : '#FFFFFF'}
                stroke={stroke}
                strokeWidth={1}
              />
              <text
                x={midX}
                y={midY + 3.5}
                textAnchor="middle"
                fontSize={9.5}
                fontFamily="JetBrains Mono, monospace"
                fill={
                  isDisrupted
                    ? '#9F1239'
                    : active
                      ? '#1E40AF'
                      : isCongested
                        ? '#92400E'
                        : '#526071'
                }
                fontWeight={500}
              >
                {labelText}
              </text>
            </g>
          );
        })}

        {/* Depots */}
        {depots.map((d) => {
          const pos = depotPos.get(d.id);
          if (!pos) return null;
          const fill = avgFill(d.inventory, d.capacity);
          const isClosed = d.status === 'CLOSED';
          const isConstrained = d.status === 'CONSTRAINED';

          return (
            <g key={d.id} transform={`translate(${pos.x - 96}, ${pos.y - 30})`}>
              <rect
                width={192}
                height={60}
                rx={5}
                fill={isClosed ? '#FFF1F2' : '#F7F9FB'}
                stroke={isClosed ? '#BE123C' : isConstrained ? '#B45309' : '#CBD5E1'}
                strokeWidth={isClosed || isConstrained ? 1.5 : 1}
              />
              <text x={11} y={19} fontSize={12} fontWeight={600} fill="#0F172A">
                {d.name || d.id}
              </text>
              <text
                x={181}
                y={19}
                textAnchor="end"
                fontSize={9.5}
                fontFamily="JetBrains Mono, monospace"
                fill="#526071"
              >
                {d.id}
              </text>
              <text x={11} y={36} fontSize={10.5} fill="#526071">
                {fill.toFixed(0)}% stock · {formatInt(d.dispatch_capacity_per_tick)} L/t cap
              </text>
              <rect x={11} y={45} width={170} height={4} rx={2} fill="#E2E8F0" />
              <rect
                x={11}
                y={45}
                width={Math.max(0, Math.min(170, (fill / 100) * 170))}
                height={4}
                rx={2}
                fill={isClosed ? '#BE123C' : isConstrained ? '#D97706' : '#0B6E4F'}
              />
            </g>
          );
        })}

        {/* Stations */}
        {stations.map((s) => {
          const pos = stationPos.get(s.id);
          if (!pos) return null;
          const fill = avgFill(s.inventory, s.capacity);
          const sev = worstStationSeverity(s.id, risk);
          const isSelected = selectedStationId === s.id;
          const border = isSelected
            ? '#1D4ED8'
            : sev === 'CRITICAL'
              ? '#BE123C'
              : sev === 'WATCH'
                ? '#B45309'
                : '#CBD5E1';
          const bg =
            sev === 'CRITICAL' ? '#FFF5F6' : sev === 'WATCH' ? '#FFFBEB' : '#FFFFFF';
          const bar =
            sev === 'CRITICAL' ? '#BE123C' : sev === 'WATCH' ? '#D97706' : '#0B6E4F';

          return (
            <g
              key={s.id}
              transform={`translate(${pos.x - 96}, ${pos.y - 30})`}
              onClick={() => onSelectStation?.(s.id)}
              onKeyDown={(e) => {
                if ((e.key === 'Enter' || e.key === ' ') && onSelectStation) {
                  e.preventDefault();
                  onSelectStation(s.id);
                }
              }}
              role={onSelectStation ? 'button' : undefined}
              tabIndex={onSelectStation ? 0 : undefined}
              aria-label={`Inspect ${s.name || s.id}, fill ${fill.toFixed(0)} percent, status ${sev}`}
              className={
                onSelectStation
                  ? 'cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-[#1D4ED8]'
                  : undefined
              }
            >
              <rect
                width={192}
                height={60}
                rx={5}
                fill={bg}
                stroke={border}
                strokeWidth={isSelected || sev !== 'OK' ? 1.8 : 1}
              />
              <text x={11} y={19} fontSize={12} fontWeight={600} fill="#0F172A">
                {s.name || s.id}
              </text>
              <text
                x={181}
                y={19}
                textAnchor="end"
                fontSize={9.5}
                fontFamily="JetBrains Mono, monospace"
                fill={sev === 'CRITICAL' ? '#9F1239' : '#526071'}
              >
                {s.demand_multiplier > 1.05 ? `${s.demand_multiplier.toFixed(1)}×` : s.id}
              </text>
              <text x={11} y={36} fontSize={10.5} fill="#526071">
                {fill.toFixed(0)}% fill · {s.demand_profile}
              </text>
              <rect x={11} y={45} width={170} height={4} rx={2} fill="#E2E8F0" />
              <rect
                x={11}
                y={45}
                width={Math.max(0, Math.min(170, (fill / 100) * 170))}
                height={4}
                rx={2}
                fill={bar}
              />
            </g>
          );
        })}
      </svg>
    </div>
  );
}
