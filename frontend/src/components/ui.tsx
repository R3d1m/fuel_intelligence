import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';

export type Tone = 'neutral' | 'ok' | 'watch' | 'critical' | 'info';

const intFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const decFormatter = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});
const timeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
});
const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

export function formatInt(val?: number | null): string {
  if (typeof val !== 'number' || !Number.isFinite(val)) return '—';
  return intFormatter.format(Math.round(val));
}

export function formatLiters(val?: number | null): string {
  if (typeof val !== 'number' || !Number.isFinite(val)) return '—';
  return `${intFormatter.format(Math.round(val))}\u00A0L`;
}

export function formatDecimal(val?: number | null, suffix = ''): string {
  if (typeof val !== 'number' || !Number.isFinite(val)) return '—';
  return `${decFormatter.format(val)}${suffix}`;
}

export function formatTime(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : timeFormatter.format(d);
}

export function formatDateTime(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : dateTimeFormatter.format(d);
}

export function formatLabel(raw?: string | null): string {
  if (!raw) return 'Unknown';
  return raw
    .toLowerCase()
    .replace(/_/g, ' ')
    .replace(/^\w/, (c) => c.toUpperCase());
}

export function statusTone(status?: string | null): Tone {
  const s = (status ?? '').toUpperCase();
  if (
    [
      'CRITICAL',
      'DISRUPTED',
      'FAILED',
      'CANCELLED',
      'DEGRADED_FALLBACK',
      'DOWN',
      'OPEN',
      'CLOSED',
      'OUTAGE',
      'LOW',
      'REJECTED',
    ].includes(s)
  ) {
    // Note: Depot/Station OPEN means operational, CircuitBreaker OPEN means tripped.
    // Callers with Depot/Station status should pass explicit tone when 'OPEN' is healthy.
    if (s === 'OPEN') return 'critical';
    return 'critical';
  }
  if (
    [
      'WATCH',
      'CONGESTED',
      'CONSTRAINED',
      'DELAYED',
      'DEGRADED_CACHED',
      'STALE',
      'HALF_OPEN',
      'MEDIUM',
      'PENDING',
    ].includes(s)
  ) {
    return 'watch';
  }
  if (
    [
      'OK',
      'AVAILABLE',
      'NORMAL',
      'SUBMITTED',
      'ARRIVED',
      'CONNECTED',
      'FRESH',
      'HIGH',
      'APPROVED',
    ].includes(s)
  ) {
    return 'ok';
  }
  if (['IN_TRANSIT', 'SCHEDULED', 'PROPOSED'].includes(s)) {
    return 'info';
  }
  return 'neutral';
}

export function facilityTone(status?: string | null): Tone {
  const s = (status ?? '').toUpperCase();
  if (s === 'OPEN') return 'ok';
  if (s === 'CONSTRAINED') return 'watch';
  if (s === 'CLOSED' || s === 'OUTAGE') return 'critical';
  return 'neutral';
}

export function breakerTone(state?: string | null): Tone {
  const s = (state ?? '').toUpperCase();
  if (s === 'CLOSED') return 'ok';
  if (s === 'HALF_OPEN') return 'watch';
  if (s === 'OPEN') return 'critical';
  return 'neutral';
}

export function StatusPill({
  children,
  tone = 'neutral',
  className = '',
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  const styles: Record<Tone, string> = {
    neutral: 'bg-[#F1F5F9] text-[#334155] border-[#CBD5E1]',
    ok: 'bg-[#ECFDF5] text-[#065F46] border-[#A7F3D0]',
    watch: 'bg-[#FFFBEB] text-[#92400E] border-[#FDE68A]',
    critical: 'bg-[#FFF1F2] text-[#9F1239] border-[#FECDD3]',
    info: 'bg-[#EFF6FF] text-[#1E40AF] border-[#BFDBFE]',
  };
  const dot: Record<Tone, string> = {
    neutral: 'bg-[#64748B]',
    ok: 'bg-[#0B6E4F]',
    watch: 'bg-[#D97706]',
    critical: 'bg-[#BE123C]',
    info: 'bg-[#1D4ED8]',
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${styles[tone]} ${className}`}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot[tone]}`} aria-hidden="true" />
      {children}
    </span>
  );
}

export function Panel({
  children,
  className = '',
  title,
  subtitle,
  actions,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <section
      className={`rounded-md border border-[#D6DCE3] bg-white p-4 sm:p-5 ${className}`}
    >
      {(title || actions) && (
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3 border-b border-[#E6EBF0] pb-3">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-[#0F172A]">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-[#526071]">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function MeterBar({
  percent,
  tone = 'neutral',
  label,
}: {
  percent: number;
  tone?: Tone;
  label?: string;
}) {
  const clamped = Math.min(100, Math.max(0, Number.isFinite(percent) ? percent : 0));
  const fill: Record<Tone, string> = {
    neutral: 'bg-[#475569]',
    ok: 'bg-[#0B6E4F]',
    watch: 'bg-[#D97706]',
    critical: 'bg-[#BE123C]',
    info: 'bg-[#1D4ED8]',
  };
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuenow={Math.round(clamped)}
      aria-valuemin={0}
      aria-valuemax={100}
      className="h-2 w-full overflow-hidden rounded-sm bg-[#E6EBF0]"
    >
      <div
        className={`h-full rounded-sm transition-[width] duration-300 ${fill[tone]}`}
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}

export function EmptyNotice({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-md border border-dashed border-[#CBD5E1] bg-[#F7F9FB] px-6 py-8 text-center">
      <div className="text-sm font-medium text-[#0F172A]">{title}</div>
      {hint && <p className="mx-auto mt-1 max-w-md text-xs text-[#526071]">{hint}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ModalDrawer({
  open,
  title,
  subtitle,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      openerRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-[#0F172A]/45 backdrop-blur-[1px]"
      onClick={onClose}
      role="presentation"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className="flex h-full w-full max-w-2xl flex-col border-l border-[#D6DCE3] bg-white shadow-2xl focus:outline-none"
        style={{ overscrollBehavior: 'contain' }}
      >
        <div className="flex items-start justify-between gap-3 border-b border-[#E6EBF0] px-5 py-4">
          <div className="min-w-0">
            <h2 id="drawer-title" className="text-base font-semibold text-[#0F172A]">
              {title}
            </h2>
            {subtitle && <p className="mt-0.5 text-xs text-[#526071]">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close detail panel"
            className="cursor-pointer rounded border border-[#D6DCE3] p-1.5 text-[#526071] transition-colors hover:bg-[#F7F9FB] hover:text-[#0F172A] focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
