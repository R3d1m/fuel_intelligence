"""Fuel Intelligence Platform — Operator Dashboard.

Streamlit dashboard that connects to the FastAPI backend and presents
fuel-network state, risks, forecasts, and dispatch recommendations in
a clean operator-friendly layout.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import streamlit as st

try:
    from dashboard.client import ApiResponse, BackendApiClient
except ModuleNotFoundError:
    from client import ApiResponse, BackendApiClient  # noqa: F401

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Fuel Intelligence",
    page_icon="⛽",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Minimal CSS — only what Streamlit can't do natively
st.markdown(
    """
    <style>
    /* Tighten up the top padding */
    .block-container { padding-top: 1.5rem; }
    /* Severity pills inside dataframes */
    .sev-crit {
      color: #fff; background: #dc2626;
      padding: 2px 8px; border-radius: 10px;
      font-weight: 600; font-size: .82rem;
    }
    .sev-watch {
      color: #fff; background: #d97706;
      padding: 2px 8px; border-radius: 10px;
      font-weight: 600; font-size: .82rem;
    }
    .sev-ok {
      color: #fff; background: #16a34a;
      padding: 2px 8px; border-radius: 10px;
      font-weight: 600; font-size: .82rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _backend_url() -> str:
    return os.environ.get("BACKEND_API_URL", "http://127.0.0.1:8001")


# ═══════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════
def render_sidebar() -> tuple[str, str]:
    """Sidebar: connection settings, actions, and legend. Returns (url, severity_filter)."""
    with st.sidebar:
        st.header("Fuel Intelligence")

        backend_url = st.text_input(
            "Backend URL",
            value=_backend_url(),
            help="Base URL of the FastAPI backend.",
        )

        st.divider()

        # --- Actions ---
        st.subheader("Actions")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Sync now", use_container_width=True):
                _do_sync(backend_url)
        with c2:
            if st.button("Plan dispatches", use_container_width=True):
                _do_plan(backend_url)

        st.divider()

        # --- Filters ---
        severity_filter: str = st.selectbox(
            "Severity filter",
            ["ALL", "CRITICAL", "WATCH", "OK"],
            index=0,
        )

        st.divider()

        # --- Legend ---
        with st.expander("Mode & severity legend"):
            st.markdown(
                """
| Label | Meaning |
|---|---|
| **NORMAL** | Simulator connected, data fresh |
| **DEGRADED_CACHED** | Breaker tripped — cached data |
| **DEGRADED_FALLBACK** | Heuristic fallback active |
| **CRITICAL** | Stockout < 3 h or tank < 20 % |
| **WATCH** | Stockout < 8 h or demand surge |
"""
            )

    return backend_url, severity_filter


def _do_sync(url: str) -> None:
    client = BackendApiClient(base_url=url, timeout_seconds=6.0)
    resp = client.trigger_sync()
    if resp.success:
        st.toast(f"Synced — tick {resp.data.get('tick')}", icon="✅")
    else:
        st.toast(f"Sync failed: {resp.error}", icon="❌")


def _do_plan(url: str) -> None:
    client = BackendApiClient(base_url=url, timeout_seconds=8.0)
    resp = client.generate_recommendations()
    if resp.success:
        n = resp.data.get("total_count", 0)
        st.toast(f"{n} recommendation(s) generated", icon="✅")
    else:
        st.toast(f"Planning failed: {resp.error}", icon="❌")


# ═══════════════════════════════════════════════════════════════════════════
# HEADER STRIP — mode banner + KPIs
# ═══════════════════════════════════════════════════════════════════════════
def render_mode_banner(health: dict[str, Any] | None) -> None:
    """Single-line operational mode indicator."""
    if health is None:
        st.error("**DISCONNECTED** — backend unreachable")
        return

    mode = health.get("mode", "UNKNOWN")
    reason = health.get("reason", "")
    circuit = health.get("simulator", {}).get("circuit_state", "?")
    age = health.get("store", {}).get("data_age_seconds")
    age_s = f"{age:.0f}s" if age is not None else "—"

    if mode == "NORMAL":
        st.success(f"**NORMAL** · circuit {circuit} · data age {age_s}")
    elif mode == "DEGRADED_CACHED":
        st.warning(
            f"**DEGRADED — CACHED** · circuit {circuit} · cached {age_s} ago  \n"
            f"{reason}"
        )
    elif mode == "DEGRADED_FALLBACK":
        st.error(f"**DEGRADED — FALLBACK** · {reason}")
    else:
        st.info(f"**{mode}** · {reason}")

    for alert_msg in health.get("alerts", []):
        st.warning(f"Data alert: {alert_msg}")


def render_kpis(
    health: dict | None,
    forecast: dict | None,
    risk: dict | None,
    metrics: dict | None,
) -> None:
    """Top-line KPI row — four numbers that matter most."""
    c1, c2, c3, c4 = st.columns(4)

    # 1) Simulator status
    with c1:
        if health:
            sim = health.get("simulator", {})
            conn = sim.get("connected", False)
            fails = sim.get("consecutive_failures", 0)
            st.metric("Simulator", "Connected" if conn else "Down")
            if fails:
                st.caption(f"{fails} consecutive failure(s)")
        else:
            st.metric("Simulator", "—")

    # 2) Fleet consumption
    with c2:
        if forecast:
            burn = forecast.get("total_fleet_demand_per_hour", 0)
            spikes = forecast.get("active_anomalies_count", 0)
            st.metric("Fleet demand", f"{burn:,.0f} L/h")
            if spikes:
                st.caption(f"{spikes} active demand spike(s)")
        else:
            st.metric("Fleet demand", "—")

    # 3) Risk summary
    with c3:
        if risk:
            sev = risk.get("overall_severity", "—")
            crits = risk.get("critical_count", 0)
            watches = risk.get("watch_count", 0)
            st.metric("Fleet risk", sev)
            st.caption(f"{crits} critical · {watches} watch")
        else:
            st.metric("Fleet risk", "—")

    # 4) Earliest stockout
    with c4:
        if risk:
            eta_h = risk.get("earliest_stockout_hours")
            eta_st = risk.get("earliest_stockout_station_id", "")
            if eta_h is not None:
                st.metric("Nearest stockout", f"{eta_h:.1f} h")
                st.caption(eta_st)
            else:
                st.metric("Nearest stockout", "None")
        else:
            st.metric("Nearest stockout", "—")


# ═══════════════════════════════════════════════════════════════════════════
# TAB 1 — DISPATCH RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════════════
def render_recommendations(client: BackendApiClient, rec_data: dict | None) -> None:
    if not rec_data:
        st.info("No recommendation data available.")
        return

    proposed = [r for r in rec_data.get("recommendations", []) if r.get("status") == "PROPOSED"]

    if not proposed:
        st.success("All stations within safe inventory levels — no dispatches needed.")
        return

    st.caption(f"{len(proposed)} pending dispatch(es)")

    for rec in proposed:
        rec_id = rec.get("recommendation_id", "")
        station = rec.get("station_name", rec.get("station_id", "?"))
        fuel = rec.get("fuel_type", "?").upper()
        qty = rec.get("quantity_liters", 0)
        depot = rec.get("source_depot_name", rec.get("source_depot_id", "?"))
        route = rec.get("route_id", "?")

        p_before = rec.get("stockout_probability_before", 0) * 100
        p_after = rec.get("stockout_probability_after", 0) * 100
        cover = rec.get("hours_of_cover_added", 0)
        unmet_saved = rec.get("expected_unmet_reduction_liters", 0)
        mitigation = rec.get("mitigation_type", "")
        needs_review = rec.get("needs_human_review", False)

        # Card title
        title = f"**{qty:,.0f} L {fuel}** → {station}"
        if needs_review:
            title += "  ·  ⚠ review required"

        with st.container(border=True):
            # Header row
            head_l, head_r = st.columns([5, 1])
            with head_l:
                st.markdown(title)
                st.caption(f"From {depot} via route `{route}`")
            with head_r:
                _mitigation_badge(mitigation)

            # Metrics
            m1, m2, m3 = st.columns(3)
            m1.metric(
                "Stockout risk",
                f"{p_after:.0f}%",
                f"-{p_before - p_after:.0f}%",
                delta_color="inverse",
            )
            m2.metric("Cover added", f"+{cover:.1f} h")
            m3.metric("Shortage prevented", f"{unmet_saved:,.0f} L")

            # Explanation
            expl = rec.get("explanation", "")
            if expl:
                st.caption(expl)

            # Constraints detail (collapsed)
            constraints = rec.get("constraints", {})
            alternatives = rec.get("alternatives", [])
            if constraints or alternatives:
                with st.expander("Constraints & alternatives"):
                    if constraints:
                        bottleneck = constraints.get("bottleneck_constraint", "—")
                        st.markdown(f"**Binding constraint:** `{bottleneck}`")
                        cc1, cc2 = st.columns(2)
                        route_max = constraints.get(
                            'route_max_shipment', 0
                        )
                        depot_inv = constraints.get(
                            'depot_available_inventory', 0
                        )
                        depot_disp = constraints.get(
                            'depot_remaining_dispatch_capacity', 0
                        )
                        ullage = constraints.get(
                            'station_effective_free_space', 0
                        )
                        needed = constraints.get('amount_needed', 0)
                        cc1.markdown(
                            f"- Route max: {route_max:,.0f} L\n"
                            f"- Depot stock: {depot_inv:,.0f} L\n"
                            f"- Depot dispatch cap: "
                            f"{depot_disp:,.0f} L"
                        )
                        cc2.markdown(
                            f"- Station ullage: "
                            f"{ullage:,.0f} L\n"
                            f"- Amount needed: {needed:,.0f} L"
                        )
                    if alternatives:
                        st.markdown(f"**{len(alternatives)} alternative(s) evaluated:**")
                        for i, alt in enumerate(alternatives, 1):
                            route_id = alt.get('route_id')
                            depot_name = alt.get(
                                'source_depot_name'
                            )
                            qty_l = alt.get('quantity_liters', 0)
                            ticks = alt.get('transit_ticks', 0)
                            reason = alt.get('reason_not_chosen')
                            st.markdown(
                                f"{i}. Route `{route_id}` "
                                f"via {depot_name} "
                                f"— {qty_l:,.0f} L, "
                                f"{ticks} ticks  \n"
                                f"   *{reason}*"
                            )

            # Action buttons
            btn_l, btn_r, note_col = st.columns([1, 1, 3])
            with note_col:
                notes = st.text_input(
                    "Operator notes",
                    key=f"n_{rec_id}",
                    placeholder="Optional justification…",
                    label_visibility="collapsed",
                )
            with btn_l:
                approved = st.button(
                    "Approve & dispatch",
                    key=f"a_{rec_id}",
                    type="primary",
                    use_container_width=True,
                )
                if approved:
                    resp = client.approve_recommendation(
                        rec_id, operator_notes=notes or None,
                    )
                    if resp.success:
                        alloc_id = (
                            resp.data
                            .get("recommendation", {})
                            .get("simulator_allocation_id")
                        )
                        st.toast(f"Dispatched — allocation #{alloc_id}", icon="✅")
                        st.rerun()
                    else:
                        st.error(f"Failed: {resp.error}")
            with btn_r:
                if st.button("Reject", key=f"r_{rec_id}", use_container_width=True):
                    resp = client.reject_recommendation(rec_id, operator_notes=notes or None)
                    if resp.success:
                        st.toast("Rejected and logged", icon="✅")
                        st.rerun()
                    else:
                        st.error(f"Failed: {resp.error}")


def _mitigation_badge(mitigation: str) -> None:
    if mitigation == "FULL_PREVENTION":
        st.success("Full prevention")
    elif mitigation == "PARTIAL_MITIGATION":
        st.warning("Partial mitigation")
    else:
        st.info("Restock")


# ═══════════════════════════════════════════════════════════════════════════
# TAB 2 — ALERTS & RISK MATRIX
# ═══════════════════════════════════════════════════════════════════════════
def render_alerts_and_risks(risk_data: dict | None, severity_filter: str) -> None:
    if not risk_data:
        st.info("No risk data available.")
        return

    # --- Priority alerts ---
    alerts = risk_data.get("active_alerts", [])
    if severity_filter != "ALL":
        alerts = [a for a in alerts if a.get("severity") == severity_filter]

    if alerts:
        st.markdown("#### Priority alerts")
        for alert in alerts:
            _render_alert_card(alert)
    else:
        st.success("No active alerts matching the filter.")

    st.divider()

    # --- Risk matrix table ---
    st.markdown("#### Inventory & risk matrix")
    _render_risk_table(risk_data, severity_filter)


def _render_alert_card(alert: dict) -> None:
    """Render one alert as a compact bordered container."""
    sev = alert.get("severity", "OK")
    st_name = alert.get("station_name", alert.get("station_id", "?"))
    fuel = alert.get("fuel_type", alert.get("fuel", "?")).upper()
    hours = alert.get("hours_to_stockout")
    urgency = alert.get("urgency_score", 0)
    explanation = alert.get("explanation", alert.get("drivers", {}).get("explanation", ""))
    is_unavoidable = alert.get("is_unavoidable_stockout", False)
    needs_review = alert.get("needs_human_review", False)

    drivers = alert.get("drivers", {})
    inv = drivers.get("initial_inventory_liters", drivers.get("initial_inventory", 0))
    cap = drivers.get("tank_capacity_liters", drivers.get("capacity", 1))
    fill = (alert.get("current_fill_ratio", (inv / cap) if cap > 0 else 0)) * 100
    burn = drivers.get("demand_rate_liters_per_hour", drivers.get("burn_rate_liters_per_hour", 0))

    with st.container(border=True):
        # Header
        h1, h2 = st.columns([4, 1])
        with h1:
            sev_icon = "🔴" if sev == "CRITICAL" else ("🟡" if sev == "WATCH" else "🟢")
            eta = f"{hours:.1f} h" if hours is not None else "> horizon"
            st.markdown(f"{sev_icon} **{st_name}** · {fuel} · stockout in **{eta}**")
            if is_unavoidable:
                st.caption("Unavoidable — fastest arrival cannot prevent shortage")
            if needs_review:
                st.caption("⚠ Manual review required")
        with h2:
            st.metric("Urgency", f"{urgency:.0f}")

        # Detail row
        d1, d2 = st.columns(2)
        with d1:
            st.progress(min(1.0, max(0.0, fill / 100)))
            st.caption(f"{inv:,.0f} / {cap:,.0f} L ({fill:.0f}%) · {burn:,.0f} L/h burn")
        with d2:
            if explanation:
                st.caption(explanation)


def _render_risk_table(risk_data: dict, severity_filter: str) -> None:
    station_risks = risk_data.get("station_risks", {})
    rows: list[dict] = []

    for st_id, fuels in station_risks.items():
        for fuel, r in fuels.items():
            sev = r.get("severity", "OK")
            if severity_filter != "ALL" and sev != severity_filter:
                continue

            inv = r.get("current_inventory_liters", r.get("current_inventory", 0))
            cap = r.get("capacity_liters", r.get("capacity", 0))
            fill = r.get("current_fill_ratio", r.get("fill_ratio", 0)) * 100
            stockout_h = r.get("hours_to_stockout", r.get("projected_stockout_hours"))
            eta = f"{stockout_h:.1f} h" if stockout_h is not None else "Safe"
            unmet = r.get("total_unmet_liters", r.get("projected_unmet_liters", 0))
            conf = r.get("confidence", 1.0)

            rows.append({
                "Station": r.get("station_name", st_id),
                "Fuel": fuel.upper(),
                "Severity": sev,
                "Fill": f"{fill:.0f}%",
                "Inventory": f"{inv:,.0f} L",
                "Capacity": f"{cap:,.0f} L",
                "Stockout ETA": eta,
                "Unmet": f"{unmet:,.0f} L" if unmet > 0 else "—",
                "Confidence": f"{conf:.0%}",
            })

    if not rows:
        st.info("No entries match the filter.")
        return

    df = pd.DataFrame(rows)

    def _color_sev(val: str) -> str:
        if val == "CRITICAL":
            return "background-color: rgba(220,38,38,.25); font-weight: 600"
        if val == "WATCH":
            return "background-color: rgba(217,119,6,.25); font-weight: 600"
        if val == "OK":
            return "background-color: rgba(22,163,74,.15)"
        return ""

    st.dataframe(
        df.style.map(_color_sev, subset=["Severity"]),
        use_container_width=True,
        hide_index=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
# TAB 3 — NETWORK STATUS  (stations, depots, routes, events)
# ═══════════════════════════════════════════════════════════════════════════
def render_network(state: dict | None, risk: dict | None) -> None:
    if not state or not state.get("snapshot"):
        st.info("No snapshot available.")
        return

    snap = state["snapshot"]

    # Sub-tabs inside the network tab keep things tidy
    t_stations, t_depots, t_routes, t_events = st.tabs(
        ["Stations", "Depots", "Routes", "Events"]
    )

    with t_stations:
        _render_stations(snap)

    with t_depots:
        _render_depots(snap, risk)

    with t_routes:
        _render_routes(snap)

    with t_events:
        _render_events(snap)


def _render_stations(snap: dict) -> None:
    stations = snap.get("stations", [])
    if not stations:
        st.info("No stations in snapshot.")
        return

    for s in stations:
        name = s.get("name", s.get("id"))
        sid = s.get("id")
        mult = s.get("demand_multiplier", 1.0)
        spike = mult > 1.05

        label = f"{name} ({sid})"
        if spike:
            label += f" · demand {mult:.2f}×"

        with st.expander(label, expanded=False):
            inv_map = s.get("inventory", {})
            cap_map = s.get("capacity", {})
            if not inv_map:
                st.caption("No tank data.")
                continue

            cols = st.columns(len(inv_map))
            for idx, (fuel, litres) in enumerate(inv_map.items()):
                cap = cap_map.get(fuel, 1)
                fill = (litres / cap * 100) if cap > 0 else 0
                with cols[idx]:
                    st.metric(fuel.upper(), f"{litres:,.0f} L", f"{fill:.0f}%")
                    st.progress(min(1.0, max(0.0, fill / 100)))


def _render_depots(snap: dict, risk: dict | None) -> None:
    depots = snap.get("depots", [])
    depot_risks = risk.get("depot_risks", {}) if risk else {}

    if not depots:
        st.info("No depots in snapshot.")
        return

    for d in depots:
        did = d.get("id")
        name = d.get("name", did)
        dispatch_cap = d.get("dispatch_capacity", 0)
        current_disp = d.get("current_dispatch", 0)

        label = f"{name} ({did}) · dispatch {current_disp:,.0f}/{dispatch_cap:,.0f} L/t"

        with st.expander(label, expanded=False):
            inv_map = d.get("inventory", {})
            cap_map = d.get("capacity", {})
            if not inv_map:
                st.caption("No inventory data.")
                continue

            cols = st.columns(len(inv_map))
            for idx, (fuel, litres) in enumerate(inv_map.items()):
                cap = cap_map.get(fuel, 1)
                fill = (litres / cap * 100) if cap > 0 else 0
                dr = depot_risks.get(did, {}).get(fuel, {})
                reserve_low = dr.get("is_reserve_critical", dr.get("severity") == "CRITICAL")

                with cols[idx]:
                    lbl = fuel.upper()
                    if reserve_low:
                        lbl += " ⚠"
                    st.metric(lbl, f"{litres:,.0f} L", f"{fill:.0f}%")
                    st.progress(min(1.0, max(0.0, fill / 100)))


def _render_routes(snap: dict) -> None:
    routes = snap.get("routes", [])
    if not routes:
        st.info("No routes in snapshot.")
        return

    disrupted = [r for r in routes if r.get("status") == "DISRUPTED"]
    if disrupted:
        st.error(
            f"{len(disrupted)} route(s) disrupted — dispatches on these routes will fail."
        )

    rows = []
    for r in routes:
        rows.append({
            "Route": r.get("id"),
            "From": r.get("origin_id"),
            "To": r.get("destination_id"),
            "Status": r.get("status", "AVAILABLE"),
            "Transit": f"{r.get('transit_ticks', 0)} ticks",
            "Max shipment": f"{r.get('max_shipment', 0):,.0f} L",
        })

    df = pd.DataFrame(rows)

    def _color(val: str) -> str:
        if val == "DISRUPTED":
            return "background-color: rgba(220,38,38,.25); font-weight: 600"
        if val == "SLOW":
            return "background-color: rgba(217,119,6,.25); font-weight: 600"
        return ""

    st.dataframe(df.style.map(_color, subset=["Status"]), use_container_width=True, hide_index=True)


def _render_events(snap: dict) -> None:
    events = snap.get("events", [])
    if not events:
        st.success("No active simulation events.")
        return

    rows = []
    for e in events:
        params = []
        if e.get("multiplier") is not None:
            params.append(f"{e['multiplier']}×")
        if e.get("delay_ticks") is not None:
            params.append(f"+{e['delay_ticks']} tick delay")
        rows.append({
            "Event": e.get("event_type", "?").upper(),
            "Target": f"{e.get('target_type', '')}:{e.get('target_id', '')}",
            "Parameters": ", ".join(params) or "—",
            "Remaining": f"{e.get('remaining_ticks', '?')} / {e.get('duration_ticks', '?')} ticks",
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════
# TAB 4 — AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════
def render_audit(
    client: BackendApiClient,
    audit_data: dict | None,
    stats_data: dict | None,
) -> None:
    # KPIs
    stats = stats_data.get("stats", {}) if stats_data else {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Formulated", stats.get("total_recommendations", 0))
    c2.metric("Submitted", stats.get("submitted_count", 0))
    c3.metric("Rejected", stats.get("rejected_count", 0))
    c4.metric("Rejection rate", f"{stats.get('rejection_rate', 0) * 100:.0f}%")

    st.divider()

    # Recommendation history
    st.markdown("#### Decision history")
    all_recs = client.get_recommendations(limit=100)
    if all_recs.success and all_recs.data:
        recs = all_recs.data.get("recommendations", [])
        if recs:
            rows = []
            for r in recs:
                p_bef = r.get(
                    'stockout_probability_before', 0,
                ) * 100
                p_aft = r.get(
                    'stockout_probability_after', 0,
                ) * 100
                rows.append({
                    "Time": r.get("created_at", "")[:19].replace("T", " "),
                    "Station": r.get("station_name", r.get("station_id", "")),
                    "Fuel": r.get("fuel_type", ""),
                    "Litres": f"{r.get('quantity_liters', 0):,.0f}",
                    "Depot": r.get("source_depot_name", r.get("source_depot_id", "")),
                    "Risk →": f"{p_bef:.0f}% → {p_aft:.0f}%",
                    "Status": r.get("status", ""),
                    "Notes": r.get("failure_reason") or r.get("operator_notes") or "—",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No historical recommendations.")
    else:
        st.info("Could not load recommendation history.")

    st.divider()

    # Audit events
    st.markdown("#### Audit event log")
    events = audit_data.get("events", []) if audit_data else []
    if events:
        rows = []
        for e in events:
            rows.append({
                "Time": e.get("timestamp", "")[:19].replace("T", " "),
                "Event": e.get("event_type", ""),
                "Transition": f"{e.get('from_status') or 'NEW'} → {e.get('to_status', '')}",
                "Rec ID": (
                    e.get("recommendation_id", "")[:12] + "…"
                ) if e.get("recommendation_id") else "—",
                "Notes": e.get("operator_notes") or "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No audit events yet.")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN FRAGMENT — auto-refreshes every 2 s
# ═══════════════════════════════════════════════════════════════════════════
@st.fragment(run_every=2)
def dashboard_fragment(backend_url: str, severity_filter: str) -> None:
    client = BackendApiClient(base_url=backend_url, timeout_seconds=4.0)

    # Connectivity check
    health_resp = client.get_health()
    if not health_resp.is_connected:
        render_mode_banner(None)
        st.info(
            "Start the backend with "
            "`uv run uvicorn app.api.main:app --port 8001` and reload."
        )
        return

    # Fetch everything
    state_resp = client.get_state()
    forecast_resp = client.get_forecasts()
    risk_resp = client.get_risks()
    rec_resp = client.get_recommendations()
    audit_resp = client.get_audit_history(limit=50)
    stats_resp = client.get_audit_stats()
    metrics_resp = client.get_metrics()

    health = health_resp.data if health_resp.success else None
    state = state_resp.data if state_resp.success else None
    forecast = forecast_resp.data if forecast_resp.success else None
    risk = risk_resp.data if risk_resp.success else None
    rec = rec_resp.data if rec_resp.success else None
    audit = audit_resp.data if audit_resp.success else None
    stats = stats_resp.data if stats_resp.success else None
    metrics = metrics_resp.data if metrics_resp.success else None

    # ── Mode banner ──
    render_mode_banner(health)

    # ── KPI strip ──
    render_kpis(health, forecast, risk, metrics)

    st.divider()

    # ── Three clean tabs ──
    tab_dispatch, tab_risk, tab_network, tab_audit = st.tabs(
        ["Dispatches", "Alerts & risks", "Network", "Audit log"]
    )

    with tab_dispatch:
        render_recommendations(client, rec)

    with tab_risk:
        render_alerts_and_risks(risk, severity_filter)

    with tab_network:
        render_network(state, risk)

    with tab_audit:
        render_audit(client, audit, stats)

    # Footer
    st.caption(
        f"Updated {datetime.now(UTC).strftime('%H:%M:%S UTC')} · "
        f"backend `{backend_url}`"
    )


# ═══════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════
def main() -> None:
    st.title("Fuel Intelligence")
    st.caption(
        "Real-time dispatch decisions · predictive "
        "stockout prevention · operational resilience"
    )

    backend_url, severity_filter = render_sidebar()
    dashboard_fragment(backend_url, severity_filter)


if __name__ == "__main__":
    main()
