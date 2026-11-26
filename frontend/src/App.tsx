import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  Fuel,
  LayoutDashboard,
  Network,
  Pause,
  Play,
  RefreshCw,
  Send,
  Settings,
  ShieldAlert,
} from 'lucide-react';
import { FuelApiClient, getDefaultBackendUrl } from './api';
import { ErrorBoundary } from './components/ErrorBoundary';
import { formatLabel, formatTime, StatusPill, statusTone } from './components/ui';
import type {
  AuditEvent,
  AuditStats,
  ForecastResponse,
  HealthResponse,
  ParsedMetrics,
  PlanResponse,
  RecommendationItem,
  RiskResponse,
  StateResponse,
} from './types';
import { DispatchesPlanView } from './views/DispatchesPlanView';
import { IntelligenceTwinView } from './views/IntelligenceTwinView';
import { NetworkLedgerView } from './views/NetworkLedgerView';
import { OverviewView } from './views/OverviewView';
import { SystemHealthView } from './views/SystemHealthView';

type NavTab = 'overview' | 'intelligence' | 'dispatches' | 'network' | 'health';

const VALID_TABS: NavTab[] = ['overview', 'intelligence', 'dispatches', 'network', 'health'];

function readInitialUrlState(): {
  tab: NavTab;
  station?: string;
  fuel?: string;
} {
  const params = new URLSearchParams(window.location.search);
  const rawTab = params.get('tab') as NavTab | null;
  const tab = rawTab && VALID_TABS.includes(rawTab) ? rawTab : 'overview';
  const station = params.get('station') || undefined;
  const fuel = params.get('fuel') || undefined;
  return { tab, station, fuel };
}

export default function App() {
  const initial = useMemo(() => readInitialUrlState(), []);

  const [backendUrl, setBackendUrl] = useState<string>(getDefaultBackendUrl);
  const [showSettings, setShowSettings] = useState(false);
  const [urlDraft, setUrlDraft] = useState(backendUrl);
  const [activeTab, setActiveTab] = useState<NavTab>(initial.tab);
  const [selectedStation, setSelectedStation] = useState<string | undefined>(initial.station);
  const [selectedFuel, setSelectedFuel] = useState<string | undefined>(initial.fuel);
  const [livePolling, setLivePolling] = useState(true);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [state, setState] = useState<StateResponse | null>(null);
  const [forecast, setForecast] = useState<ForecastResponse | null>(null);
  const [risk, setRisk] = useState<RiskResponse | null>(null);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [recommendations, setRecommendations] = useState<RecommendationItem[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [auditStats, setAuditStats] = useState<AuditStats | null>(null);
  const [metrics, setMetrics] = useState<ParsedMetrics | null>(null);

  const [connected, setConnected] = useState<boolean>(true);
  const [syncing, setSyncing] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [lastGenerateMsg, setLastGenerateMsg] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [toast, setToast] = useState<{ msg: string; tone: 'ok' | 'err' } | null>(null);

  const client = useMemo(() => new FuelApiClient(backendUrl), [backendUrl]);

  // Sync URL query params so tab & station deep links survive refresh
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    params.set('tab', activeTab);
    if (selectedStation) params.set('station', selectedStation);
    else params.delete('station');
    if (selectedFuel) params.set('fuel', selectedFuel);
    else params.delete('fuel');
    const nextUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState(null, '', nextUrl);
  }, [activeTab, selectedStation, selectedFuel]);

  const notify = (msg: string, tone: 'ok' | 'err' = 'ok') => {
    setToast({ msg, tone });
    window.setTimeout(() => setToast(null), 4000);
  };

  const fetchAll = useCallback(async () => {
    try {
      const h = await client.getHealth();
      setHealth(h);
      setConnected(true);
    } catch {
      setHealth(null);
      setConnected(false);
      return;
    }

    const [st, fc, rk, pl, rc, au, as, mt] = await Promise.allSettled([
      client.getState(),
      client.getForecasts(),
      client.getRisks(),
      client.getPlan(),
      client.getRecommendations({ limit: 100 }),
      client.getAuditHistory({ limit: 50 }),
      client.getAuditStats(),
      client.getMetrics(),
    ]);

    if (st.status === 'fulfilled') setState(st.value);
    if (fc.status === 'fulfilled') setForecast(fc.value);
    if (rk.status === 'fulfilled') setRisk(rk.value);
    if (pl.status === 'fulfilled') setPlan(pl.value);
    if (rc.status === 'fulfilled') setRecommendations(rc.value.recommendations ?? []);
    if (au.status === 'fulfilled') setAuditEvents(au.value.events ?? []);
    if (as.status === 'fulfilled') setAuditStats(as.value.stats ?? null);
    if (mt.status === 'fulfilled') setMetrics(mt.value);
  }, [client]);

  useEffect(() => {
    fetchAll();
    if (!livePolling) return;
    const id = window.setInterval(() => {
      if (!document.hidden) fetchAll();
    }, 2500);
    return () => window.clearInterval(id);
  }, [fetchAll, livePolling]);

  const handleSync = async () => {
    setSyncing(true);
    try {
      const res = await client.triggerSync();
      notify(res.message, res.success ? 'ok' : 'err');
      await fetchAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Manual sync failed', 'err');
    } finally {
      setSyncing(false);
    }
  };

  const handleGenerate = async () => {
    setGenerating(true);
    try {
      const res = await client.generateRecommendations();
      const count = res.total_count ?? 0;
      const msg =
        count > 0
          ? `Generated ${count} dispatch recommendation(s)`
          : 'Planner ran: 0 dispatches needed (all stations within safe thresholds)';
      setLastGenerateMsg(msg);
      notify(msg);
      await fetchAll();
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : 'Recommendation generation failed';
      setLastGenerateMsg(errMsg);
      notify(errMsg, 'err');
    } finally {
      setGenerating(false);
    }
  };

  const handleApprove = async (id: string, notes?: string) => {
    setBusyId(id);
    try {
      const res = await client.approveRecommendation(id, notes);
      const allocId = res.recommendation?.simulator_allocation_id;
      notify(`Dispatched allocation #${allocId ?? 'confirmed'}`);
      await fetchAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Dispatch approval failed', 'err');
    } finally {
      setBusyId(null);
    }
  };

  const handleReject = async (id: string, notes?: string) => {
    setBusyId(id);
    try {
      await client.rejectRecommendation(id, notes);
      notify('Recommendation declined and logged');
      await fetchAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Decline failed', 'err');
    } finally {
      setBusyId(null);
    }
  };

  const pendingCount = recommendations.filter((r) => r.status === 'PROPOSED').length;
  const criticalCount = risk?.critical_count ?? 0;
  const currentTick = state?.snapshot?.tick ?? 0;

  const navItems: Array<{
    id: NavTab;
    label: string;
    icon: typeof LayoutDashboard;
    badge?: number;
    badgeTone?: 'critical' | 'info';
  }> = [
    { id: 'overview', label: 'Command Deck', icon: LayoutDashboard },
    {
      id: 'intelligence',
      label: 'Risks & Digital Twin',
      icon: ShieldAlert,
      badge: criticalCount || undefined,
      badgeTone: 'critical',
    },
    {
      id: 'dispatches',
      label: 'Planner & Dispatches',
      icon: Send,
      badge: pendingCount || undefined,
      badgeTone: 'info',
    },
    { id: 'network', label: 'Network & Ledger', icon: Network },
    { id: 'health', label: 'System & Metrics', icon: Activity },
  ];

  return (
    <div className="min-h-screen bg-[#EEF1F4] text-[#0F172A]">
      <a href="#main-content" className="skip-link">
        Skip to main content
      </a>

      {/* Deep Slate Command Rail */}
      <header className="sticky top-0 z-30 border-b border-[#1E293B] bg-[#0F172A] text-white">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center justify-between gap-3 px-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-[#0B6E4F] text-white">
              <Fuel className="h-4 w-4" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold tracking-tight text-white">
                Fuel Intelligence Console
              </div>
              <div className="font-mono-num hidden text-[11px] text-[#94A3B8] sm:block">
                {state?.snapshot
                  ? `Tick ${state.snapshot.tick} · ${formatTime(state.snapshot.sim_time)}`
                  : 'Waiting for snapshot…'}
              </div>
            </div>
            <StatusPill tone={connected ? statusTone(health?.mode) : 'critical'}>
              {connected ? formatLabel(health?.mode ?? 'Connecting') : 'Disconnected'}
            </StatusPill>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              disabled={syncing}
              onClick={handleSync}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded border border-[#334155] bg-[#1E293B] px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-[#334155] disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-[#38BDF8] focus-visible:outline-none"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 text-[#94A3B8] ${syncing ? 'animate-spin' : ''}`}
                aria-hidden="true"
              />
              <span className="hidden sm:inline">{syncing ? 'Syncing…' : 'Sync now'}</span>
            </button>

            <button
              type="button"
              onClick={() => setLivePolling((p) => !p)}
              aria-pressed={livePolling}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded border border-[#334155] bg-[#1E293B] px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-[#334155] focus-visible:ring-2 focus-visible:ring-[#38BDF8] focus-visible:outline-none"
            >
              {livePolling ? (
                <Pause className="h-3.5 w-3.5 text-[#34D399]" aria-hidden="true" />
              ) : (
                <Play className="h-3.5 w-3.5 text-[#94A3B8]" aria-hidden="true" />
              )}
              <span className="hidden sm:inline">{livePolling ? 'Auto-sync' : 'Paused'}</span>
            </button>

            <button
              type="button"
              onClick={() => {
                setUrlDraft(backendUrl);
                setShowSettings((s) => !s);
              }}
              aria-expanded={showSettings}
              aria-label="Configure backend API endpoint"
              className="cursor-pointer rounded border border-[#334155] bg-[#1E293B] p-1.5 text-[#94A3B8] transition-colors hover:bg-[#334155] hover:text-white focus-visible:ring-2 focus-visible:ring-[#38BDF8] focus-visible:outline-none"
            >
              <Settings className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </div>

        {showSettings && (
          <div className="border-t border-[#1E293B] bg-[#1E293B] px-4 py-3 sm:px-6">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const clean = urlDraft.trim() || 'http://127.0.0.1:8001';
                localStorage.setItem('fuel_backend_url', clean);
                setBackendUrl(clean);
                setShowSettings(false);
                notify('Backend URL updated');
              }}
              className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-2.5"
            >
              <label htmlFor="backend-url" className="text-xs font-medium text-[#CBD5E1]">
                FastAPI Base URL
              </label>
              <input
                id="backend-url"
                name="backend_url"
                type="url"
                value={urlDraft}
                onChange={(e) => setUrlDraft(e.target.value)}
                placeholder="http://127.0.0.1:8001…"
                className="font-mono-num w-72 rounded border border-[#475569] bg-[#0F172A] px-3 py-1.5 text-xs text-white placeholder:text-[#64748B] focus-visible:ring-2 focus-visible:ring-[#38BDF8] focus-visible:outline-none"
              />
              <button
                type="submit"
                className="cursor-pointer rounded bg-[#0B6E4F] px-3 py-1.5 text-xs font-medium text-white hover:bg-[#08553D] focus-visible:ring-2 focus-visible:ring-[#38BDF8] focus-visible:outline-none"
              >
                Save endpoint
              </button>
              <button
                type="button"
                onClick={() => setShowSettings(false)}
                className="cursor-pointer rounded px-2.5 py-1.5 text-xs text-[#CBD5E1] hover:text-white focus-visible:ring-2 focus-visible:ring-[#38BDF8] focus-visible:outline-none"
              >
                Cancel
              </button>
            </form>
          </div>
        )}
      </header>

      {/* Primary Navigation & Workspace */}
      <div className="mx-auto flex max-w-[1440px] flex-col gap-5 px-4 py-5 sm:px-6 lg:flex-row">
        <nav
          aria-label="Primary console sections"
          className="flex shrink-0 gap-1 overflow-x-auto border-b border-[#D6DCE3] pb-2 lg:w-56 lg:flex-col lg:border-r lg:border-b-0 lg:pr-4 lg:pb-0"
        >
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = activeTab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                aria-current={active ? 'page' : undefined}
                onClick={() => setActiveTab(item.id)}
                className={`flex shrink-0 cursor-pointer items-center justify-between gap-2 rounded border-l-2 px-3 py-2 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-[#1D4ED8] focus-visible:outline-none ${
                  active
                    ? 'border-[#0B6E4F] bg-white text-[#0F172A] shadow-2xs'
                    : 'border-transparent text-[#526071] hover:bg-white/70 hover:text-[#0F172A]'
                }`}
              >
                <span className="flex items-center gap-2.5">
                  <Icon
                    className={`h-4 w-4 ${active ? 'text-[#0B6E4F]' : 'text-[#526071]'}`}
                    aria-hidden="true"
                  />
                  {item.label}
                </span>
                {item.badge !== undefined && (
                  <span
                    className={`font-mono-num rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                      item.badgeTone === 'critical'
                        ? 'bg-[#FFF1F2] text-[#9F1239]'
                        : 'bg-[#EFF6FF] text-[#1E40AF]'
                    }`}
                  >
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <main id="main-content" className="min-w-0 flex-1">
          {!connected && (
            <div
              role="alert"
              className="mb-5 rounded border border-[#FECDD3] bg-[#FFF1F2] px-4 py-3 text-xs text-[#9F1239]"
            >
              Cannot reach Fuel Intelligence backend at{' '}
              <span className="font-mono-num font-semibold" translate="no">
                {backendUrl}
              </span>
              . Start the FastAPI server with{' '}
              <span className="font-mono-num" translate="no">
                uv run uvicorn app.api.main:app --port 8001
              </span>
              .
            </div>
          )}

          <ErrorBoundary resetKey={activeTab}>
            {activeTab === 'overview' && (
              <OverviewView
                health={health}
                state={state}
                forecast={forecast}
                risk={risk}
                pendingRecsCount={pendingCount}
                onInspectStation={(stationId, fuelType) => {
                  setSelectedStation(stationId);
                  if (fuelType) setSelectedFuel(fuelType.toUpperCase());
                  setActiveTab('intelligence');
                }}
                onOpenDispatches={() => setActiveTab('dispatches')}
              />
            )}

            {activeTab === 'intelligence' && (
              <IntelligenceTwinView
                client={client}
                risk={risk}
                currentTick={currentTick}
                selectedStationId={selectedStation}
                selectedFuelType={selectedFuel}
                onSelectTarget={(stId, fuel) => {
                  setSelectedStation(stId);
                  setSelectedFuel(fuel);
                }}
              />
            )}

            {activeTab === 'dispatches' && (
              <DispatchesPlanView
                client={client}
                recommendations={recommendations}
                plan={plan}
                simulatorStatus={state?.snapshot?.instance?.status}
                currentTick={currentTick}
                lastGenerateMsg={lastGenerateMsg}
                onApprove={handleApprove}
                onReject={handleReject}
                onGenerate={handleGenerate}
                busyId={busyId}
                generating={generating}
              />
            )}

            {activeTab === 'network' && (
              <NetworkLedgerView
                state={state}
                risk={risk}
                allRecommendations={recommendations}
                auditEvents={auditEvents}
                auditStats={auditStats}
                onInspectStation={(stationId) => {
                  setSelectedStation(stationId);
                  setActiveTab('intelligence');
                }}
              />
            )}

            {activeTab === 'health' && <SystemHealthView health={health} metrics={metrics} />}
          </ErrorBoundary>
        </main>
      </div>

      {/* Live Toast Feedback */}
      <div aria-live="polite" className="fixed right-5 bottom-5 z-50">
        {toast && (
          <div
            className={`rounded px-4 py-2.5 text-xs font-medium text-white shadow-lg ${
              toast.tone === 'err' ? 'bg-[#BE123C]' : 'bg-[#0F172A]'
            }`}
          >
            {toast.msg}
          </div>
        )}
      </div>
    </div>
  );
}
