"""Fuel Intelligence Platform - Operator Decision Dashboard (Streamlit).

Thin client connecting to the Fuel Intelligence FastAPI backend.
Displays real-time network topology, inventory levels, route health,
active chaos events, demand forecasts, and urgency-ranked risk projections.
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
    from client import ApiResponse, BackendApiClient

# Configure page layout and visual identity
st.set_page_config(
    page_title="Fuel Supply Intelligence & Resilience Platform",
    page_icon="⛽",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for high-contrast operator badges and status cards
st.markdown(
    """
    <style>
    .metric-card {
        background-color: #1e2530;
        border-radius: 8px;
        padding: 12px 16px;
        border: 1px solid #2d3748;
        margin-bottom: 8px;
    }
    .badge-critical {
        background-color: #dc2626;
        color: white;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .badge-watch {
        background-color: #d97706;
        color: white;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .badge-ok {
        background-color: #16a34a;
        color: white;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .alert-card-critical {
        border-left: 5px solid #dc2626;
        background-color: rgba(220, 38, 38, 0.08);
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
    .alert-card-watch {
        border-left: 5px solid #d97706;
        background-color: rgba(217, 119, 6, 0.08);
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
    .alert-card-ok {
        border-left: 5px solid #16a34a;
        background-color: rgba(22, 163, 74, 0.08);
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_default_backend_url() -> str:
    """Resolve default backend URL from environment or fallback."""
    return os.environ.get("BACKEND_API_URL", "http://127.0.0.1:8001")


def render_operational_mode_banner(health_data: dict[str, Any] | None) -> None:
    """Render high-visibility operational mode banner with diagnostic justification."""
    if health_data is None:
        st.error(
            "🔴 **OPERATIONAL MODE: DISCONNECTED** — Backend API unreachable. "
            "Please verify that the Fuel Intelligence FastAPI server is running."
        )
        return

    mode = health_data.get("mode", "UNKNOWN")
    reason = health_data.get("reason", "No diagnostic reason provided.")
    sim = health_data.get("simulator", {})
    store = health_data.get("store", {})

    circuit = sim.get("circuit_state", "UNKNOWN")
    data_age = store.get("data_age_seconds")
    age_str = f"{data_age:.1f}s ago" if data_age is not None else "N/A"

    if mode == "NORMAL":
        st.success(
            f"🟢 **MODE: NORMAL** | Circuit: `{circuit}` | State Freshness: `{age_str}`  \n"
            f"**Diagnostic Status:** {reason}"
        )
    elif mode == "DEGRADED_CACHED":
        st.warning(
            f"🟠 **MODE: DEGRADED_CACHED** | Circuit: `{circuit}` | Cached Age: `{age_str}`  \n"
            f"**Resilience Fallback:** Upstream disruption active. Serving cached state.  \n"
            f"**Reason:** {reason}"
        )
    elif mode == "DEGRADED_FALLBACK":
        st.error(
            f"🔴 **MODE: DEGRADED_FALLBACK** | Circuit: `{circuit}`  \n"
            f"**Safe Fallback:** Forecasting unavailable; heuristic threshold rules active.  \n"
            f"**Reason:** {reason}"
        )
    else:
        st.info(f"⚪ **MODE: {mode}** | {reason}")

    # Render any active data integrity or resilience alerts
    alerts = health_data.get("alerts", [])
    if alerts:
        for alert_msg in alerts:
            st.warning(f"⚠️ **Data Integrity Alert:** {alert_msg}")


def render_status_panel(
    health_data: dict[str, Any] | None,
    forecast_data: dict[str, Any] | None,
    risk_data: dict[str, Any] | None,
) -> None:
    """Render 4-column KPI status panel across simulator, backend, forecast, and risk."""
    col1, col2, col3, col4 = st.columns(4)

    # Column 1: Simulator Upstream & Breaker
    with col1:
        st.markdown("#### ⚡ Upstream Simulator")
        if health_data:
            sim = health_data.get("simulator", {})
            connected = sim.get("connected", False)
            circuit = sim.get("circuit_state", "UNKNOWN")
            failures = sim.get("consecutive_failures", 0)
            cooldown = sim.get("cooldown_remaining", 0.0)

            status_icon = "🟢 Connected" if connected else "🔴 Disrupted"
            st.metric(label="Simulator Gateway", value=status_icon)
            st.caption(
                f"Circuit: **{circuit}** | Failures: **{failures}** | Cooldown: **{cooldown:.1f}s**"
            )
        else:
            st.metric(label="Simulator Gateway", value="Disconnected")
            st.caption("Backend offline")

    # Column 2: Ingest Store & Poller
    with col2:
        st.markdown("#### 📦 State Repository")
        if health_data:
            store = health_data.get("store", {})
            poller = health_data.get("poller", {})
            has_snap = store.get("has_snapshot", False)
            is_stale = store.get("is_stale", False)
            age = store.get("data_age_seconds")
            updates = store.get("total_updates", 0)
            poll_running = poller.get("is_running", False)

            snap_status = "⚠️ Stale" if is_stale else ("🟢 Synchronized" if has_snap else "⚪ Empty")
            st.metric(label="State Store", value=snap_status)
            age_display = f"{age:.1f}s" if age is not None else "--"
            st.caption(
                f"Age: **{age_display}** | Ingested: **{updates}** | "
                f"Poller: **{'Active' if poll_running else 'Stopped'}**"
            )
        else:
            st.metric(label="State Store", value="Unknown")
            st.caption("--")

    # Column 3: Demand Forecaster Telemetry
    with col3:
        st.markdown("#### 📈 Demand Intelligence")
        if forecast_data:
            burn_rate = forecast_data.get("total_fleet_demand_per_hour", 0.0)
            mae = forecast_data.get("average_mae", 0.0)
            mape = forecast_data.get("average_mape", 0.0)
            spikes = forecast_data.get("active_anomalies_count", 0)

            st.metric(label="Fleet Consumption", value=f"{burn_rate:,.0f} L/h")
            st.caption(f"MAE: **{mae:.2f} L/t** | MAPE: **{mape:.1f}%** | Spikes: **{spikes}**")
        else:
            st.metric(label="Fleet Consumption", value="-- L/h")
            st.caption("Awaiting forecast data")

    # Column 4: Fleet Risk & Stockout Projections
    with col4:
        st.markdown("#### 🛡️ Fleet Risk Status")
        if risk_data:
            severity = risk_data.get("overall_severity", "UNKNOWN")
            crit_count = risk_data.get("critical_count", 0)
            watch_count = risk_data.get("watch_count", 0)
            earliest_h = risk_data.get("earliest_stockout_hours")
            earliest_st = risk_data.get("earliest_stockout_station_id")

            earliest_str = (
                f"{earliest_h:.1f}h ({earliest_st})" if earliest_h is not None else "None"
            )
            st.metric(label="Worst Severity", value=severity)
            st.caption(
                f"Crit: **{crit_count}** | Watch: **{watch_count}** | ETA: **{earliest_str}**"
            )
        else:
            st.metric(label="Worst Severity", value="--")
            st.caption("Awaiting risk assessment")


def render_urgency_alerts(risk_data: dict[str, Any] | None, severity_filter: str) -> None:
    """Render priority operational alerts sorted by urgency score."""
    st.subheader("🚨 Priority Operational Alerts (Ranked by Urgency)")

    if not risk_data or "active_alerts" not in risk_data:
        st.info("No active risk assessment available yet.")
        return

    alerts = risk_data.get("active_alerts", [])
    if severity_filter != "ALL":
        alerts = [a for a in alerts if a.get("severity") == severity_filter]

    if not alerts:
        st.success("✅ Fleet is fully supplied. No active alerts matching criteria.")
        return

    for alert in alerts:
        sev = alert.get("severity", "OK")
        card_class = (
            "alert-card-critical"
            if sev == "CRITICAL"
            else ("alert-card-watch" if sev == "WATCH" else "alert-card-ok")
        )
        badge_class = (
            "badge-critical"
            if sev == "CRITICAL"
            else ("badge-watch" if sev == "WATCH" else "badge-ok")
        )

        st_id = alert.get("station_id", "")
        st_name = alert.get("station_name", st_id)
        fuel = alert.get("fuel_type", alert.get("fuel", "")).upper()
        urgency = alert.get("urgency_score", 0.0)
        hours_stockout = alert.get("hours_to_stockout")
        stockout_tick = alert.get("first_stockout_tick", alert.get("stockout_tick"))
        is_unavoidable = alert.get("is_unavoidable_stockout", False)
        unavoidable_h = alert.get("unavoidable_shortage_hours")
        confidence = alert.get("confidence", 1.0)
        conf_tier = alert.get("confidence_level", alert.get("confidence_tier", "HIGH"))
        needs_review = alert.get("needs_human_review", False)

        drivers = alert.get("drivers", {})
        explanation = alert.get(
            "explanation", drivers.get("explanation", "Potential supply shortage detected.")
        )
        initial_inv = drivers.get("initial_inventory_liters", drivers.get("initial_inventory", 0.0))
        capacity = drivers.get("tank_capacity_liters", drivers.get("capacity", 1.0))
        fill_ratio = (
            alert.get("current_fill_ratio", (initial_inv / capacity) if capacity > 0 else 0.0)
            * 100.0
        )
        burn_rate = drivers.get(
            "demand_rate_liters_per_hour", drivers.get("burn_rate_liters_per_hour", 0.0)
        )
        incoming_shipments = drivers.get(
            "in_transit_shipments_count", drivers.get("incoming_shipments_count", 0)
        )

        # Build Card Header HTML
        header_html = (
            f'<div class="{card_class}">'
            f'<div style="display:flex; justify-content:space-between; '
            f'align-items:center; margin-bottom:8px;">'
            f'<div><span class="{badge_class}">{sev}</span>'
            f'<strong style="font-size:1.05rem; margin-left:8px;">{st_name} ({st_id})</strong>'
            f'<span style="background-color:#374151; color:#f3f4f6; padding:2px 6px; '
            f'border-radius:4px; font-size:0.8rem; margin-left:6px;">{fuel}</span></div>'
            f'<div><span style="font-size:0.85rem; color:#9ca3af;">'
            f"Urgency Score: <strong>{urgency:.1f}</strong></span></div>"
            f"</div>"
        )
        st.markdown(header_html, unsafe_allow_html=True)

        col_a, col_b, col_c = st.columns([4, 4, 4])
        with col_a:
            if hours_stockout is not None:
                st.markdown(
                    f"⏱️ **Stockout Proximity:** `{hours_stockout:.1f}h` (Tick {stockout_tick})"
                )
            else:
                st.markdown("⏱️ **Stockout Proximity:** `> Horizon (Safe)`")

            if is_unavoidable:
                st.markdown(
                    f"🚨 **UNAVOIDABLE STOCKOUT:** Fastest arrival cannot prevent shortage! "
                    f"Deficit window: ~`{unavoidable_h:.1f}h`."
                )

        with col_b:
            st.markdown(
                f"⛽ **Inventory:** `{initial_inv:,.0f} / {capacity:,.0f} L` "
                f"(**{fill_ratio:.1f}%**)"
            )
            st.progress(min(1.0, max(0.0, fill_ratio / 100.0)))
            st.caption(f"Burn Rate: **{burn_rate:,.0f} L/h** | Arrivals: **{incoming_shipments}**")

        with col_c:
            st.markdown(f"**Root Cause:** {explanation}")
            review_badge = (
                "⚠️ **HUMAN REVIEW MANDATED**"
                if needs_review
                else f"Confidence: `{conf_tier} ({confidence:.0%})`"
            )
            st.caption(review_badge)

        st.markdown("</div>", unsafe_allow_html=True)


def render_risk_matrix(risk_data: dict[str, Any] | None, severity_filter: str) -> None:
    """Render color-coded table of all (station, fuel) pairs."""
    st.subheader("📊 Fleet Inventory & Risk Matrix")

    if not risk_data or "station_risks" not in risk_data:
        st.info("No station risk matrix available.")
        return

    station_risks = risk_data.get("station_risks", {})
    records = []

    for st_id, fuels in station_risks.items():
        for fuel, r in fuels.items():
            sev = r.get("severity", "OK")
            if severity_filter != "ALL" and sev != severity_filter:
                continue

            inv = r.get("current_inventory_liters", r.get("current_inventory", 0.0))
            cap = r.get("capacity_liters", r.get("capacity", 0.0))
            fill_pct = r.get("current_fill_ratio", r.get("fill_ratio", 0.0)) * 100.0
            stockout_h = r.get("hours_to_stockout", r.get("projected_stockout_hours"))
            stockout_str = f"{stockout_h:.1f}h" if stockout_h is not None else "Safe (>24t)"
            unmet = r.get("total_unmet_liters", r.get("projected_unmet_liters", 0.0))
            conf = r.get("confidence", 1.0)
            needs_review = r.get("needs_human_review", False)
            drivers = r.get("drivers", {})
            arrivals = drivers.get("in_transit_shipments_count", r.get("arrivals_count", 0))

            records.append(
                {
                    "Station": f"{r.get('station_name', st_id)} ({st_id})",
                    "Fuel": fuel.upper(),
                    "Severity": sev,
                    "Fill %": f"{fill_pct:.1f}%",
                    "Inventory (L)": f"{inv:,.0f}",
                    "Capacity (L)": f"{cap:,.0f}",
                    "Stockout ETA": stockout_str,
                    "Unmet (L)": f"{unmet:,.0f}" if unmet > 0 else "0",
                    "In-Flight": arrivals,
                    "Confidence": f"{conf:.0%}" + (" ⚠️" if needs_review else ""),
                }
            )

    if not records:
        st.info("No records matching the selected severity filter.")
        return

    df = pd.DataFrame(records)

    # Style severity with soft background colors
    def style_severity(val: str) -> str:
        if val == "CRITICAL":
            return "background-color: rgba(220, 38, 38, 0.3); font-weight: bold;"
        if val == "WATCH":
            return "background-color: rgba(217, 119, 6, 0.3); font-weight: bold;"
        if val == "OK":
            return "background-color: rgba(22, 163, 74, 0.2);"
        return ""

    styled_df = df.style.map(style_severity, subset=["Severity"])
    st.dataframe(styled_df, use_container_width=True, hide_index=True)


def render_stations_inventory(state_data: dict[str, Any] | None) -> None:
    """Render station tanks, fill percentages, and demand multipliers."""
    st.subheader("🏪 Station Tank Levels & Capacity")

    if not state_data or not state_data.get("snapshot"):
        st.info("No snapshot state available.")
        return

    stations = state_data["snapshot"].get("stations", [])
    if not stations:
        st.info("No stations found in snapshot.")
        return

    for st_info in stations:
        name = st_info.get("name", st_info.get("id"))
        st_id = st_info.get("id")
        multiplier = st_info.get("demand_multiplier", 1.0)
        status_val = st_info.get("status", "OPEN")

        spike_badge = "🔥 **DEMAND SPIKE ACTIVE**" if multiplier > 1.05 else "Normal"
        expander_title = (
            f"📍 {name} ({st_id}) — Status: {status_val} | "
            f"Demand: {multiplier:.2f}x ({spike_badge})"
        )

        with st.expander(expander_title, expanded=True):
            inv_map = st_info.get("inventory", {})
            cap_map = st_info.get("capacity", {})

            cols = st.columns(len(inv_map))
            for idx, (fuel, current_l) in enumerate(inv_map.items()):
                total_cap = cap_map.get(fuel, 1.0)
                fill_ratio = (current_l / total_cap * 100.0) if total_cap > 0 else 0.0

                with cols[idx]:
                    st.metric(
                        label=f"{fuel.upper()}",
                        value=f"{current_l:,.0f} L",
                        delta=f"{fill_ratio:.1f}% fill",
                    )
                    st.progress(min(1.0, max(0.0, fill_ratio / 100.0)))
                    st.caption(f"Capacity: {total_cap:,.0f} L")


def render_depot_status(
    state_data: dict[str, Any] | None, risk_data: dict[str, Any] | None
) -> None:
    """Render depot bulk inventories, dispatch capacities, and reserve risks."""
    st.subheader("🏭 Bulk Supply Depots & Reserves")

    if not state_data or not state_data.get("snapshot"):
        st.info("No snapshot state available.")
        return

    depots = state_data["snapshot"].get("depots", [])
    depot_risks = risk_data.get("depot_risks", {}) if risk_data else {}

    if not depots:
        st.info("No depots found in snapshot.")
        return

    for dp in depots:
        dp_id = dp.get("id")
        name = dp.get("name", dp_id)
        status_val = dp.get("status", "OPEN")
        dispatch_cap = dp.get("dispatch_capacity", 0.0)
        current_disp = dp.get("current_dispatch", 0.0)

        expander_title = (
            f"🏢 {name} ({dp_id}) — Status: `{status_val}` | "
            f"Dispatch Load: {current_disp:,.0f} / {dispatch_cap:,.0f} L/t"
        )
        with st.expander(expander_title, expanded=True):
            inv_map = dp.get("inventory", {})
            cap_map = dp.get("capacity", {})

            cols = st.columns(len(inv_map))
            for idx, (fuel, current_l) in enumerate(inv_map.items()):
                total_cap = cap_map.get(fuel, 1.0)
                fill_ratio = (current_l / total_cap * 100.0) if total_cap > 0 else 0.0

                dp_fuel_risk = depot_risks.get(dp_id, {}).get(fuel, {})
                reserve_crit = dp_fuel_risk.get(
                    "is_reserve_critical", dp_fuel_risk.get("severity") == "CRITICAL"
                )
                disp_inv = dp_fuel_risk.get(
                    "effective_available_liters",
                    dp_fuel_risk.get("dispatchable_inventory", current_l),
                )

                with cols[idx]:
                    label = f"{fuel.upper()}" + (" ⚠️ (Reserve Low)" if reserve_crit else "")
                    st.metric(
                        label=label,
                        value=f"{current_l:,.0f} L",
                        delta=f"{fill_ratio:.1f}% fill",
                    )
                    st.progress(min(1.0, max(0.0, fill_ratio / 100.0)))
                    st.caption(f"Cap: {total_cap:,.0f} L | Dispatchable: **{disp_inv:,.0f} L**")


def render_route_status(state_data: dict[str, Any] | None) -> None:
    """Render route network topology, disruptions, and transit times."""
    st.subheader("🚚 Supply Route Logistics & Health")

    if not state_data or not state_data.get("snapshot"):
        st.info("No snapshot state available.")
        return

    routes = state_data["snapshot"].get("routes", [])
    if not routes:
        st.info("No routes found in snapshot.")
        return

    # Check for any route disruptions to display an operator warning
    disrupted_routes = [r for r in routes if r.get("status") == "DISRUPTED"]
    if disrupted_routes:
        st.error(
            f"⚠️ **ACTIVE ROUTE DISRUPTION:** {len(disrupted_routes)} route(s) are disrupted!  \n"
            f"**Critical Operational Rule:** Any allocations dispatched on disrupted routes fail "
            f"permanently at departure and fuel is burned without refund."
        )

    records = []
    for r in routes:
        r_id = r.get("id")
        orig = r.get("origin_id")
        dest = r.get("destination_id")
        status_val = r.get("status", "AVAILABLE")
        transit_m = r.get("transit_time", 0.0)
        transit_t = r.get("transit_ticks", 0)
        max_ship = r.get("max_shipment", 0.0)

        records.append(
            {
                "Route ID": r_id,
                "Origin Depot": orig,
                "Destination Station": dest,
                "Status": status_val,
                "Transit Duration": f"{transit_m:.0f}m ({transit_t} ticks)",
                "Max Shipment (L)": f"{max_ship:,.0f}",
            }
        )

    df = pd.DataFrame(records)

    def style_route_status(val: str) -> str:
        if val == "DISRUPTED":
            return "background-color: rgba(220, 38, 38, 0.3); font-weight: bold;"
        if val == "SLOW":
            return "background-color: rgba(217, 119, 6, 0.3); font-weight: bold;"
        if val == "AVAILABLE":
            return "background-color: rgba(22, 163, 74, 0.2);"
        return ""

    styled_df = df.style.map(style_route_status, subset=["Status"])
    st.dataframe(styled_df, use_container_width=True, hide_index=True)


def render_active_events(state_data: dict[str, Any] | None) -> None:
    """Render active simulation events (spikes, disruptions, delays, outages)."""
    st.subheader("⚡ Active Simulation & Chaos Events")

    if not state_data or not state_data.get("snapshot"):
        st.info("No snapshot state available.")
        return

    events = state_data["snapshot"].get("events", [])
    if not events:
        st.success("✅ No active simulation events or chaos injections present.")
        return

    records = []
    for evt in events:
        evt_id = evt.get("id", "N/A")
        evt_type = evt.get("event_type", "unknown")
        target_type = evt.get("target_type", "")
        target_id = evt.get("target_id", "")
        multiplier = evt.get("multiplier")
        delay_ticks = evt.get("delay_ticks")
        duration = evt.get("duration_ticks", "--")
        remaining = evt.get("remaining_ticks", "--")

        details_parts = []
        if multiplier is not None:
            details_parts.append(f"multiplier={multiplier}x")
        if delay_ticks is not None:
            details_parts.append(f"delay={delay_ticks} ticks")
        details_str = ", ".join(details_parts) if details_parts else "--"

        records.append(
            {
                "Event ID": evt_id,
                "Event Type": evt_type.upper(),
                "Target": f"{target_type}:{target_id}",
                "Parameters": details_str,
                "Duration": f"{duration} ticks",
                "Remaining": f"{remaining} ticks",
            }
        )

    df = pd.DataFrame(records)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_recommendations_deck(
    client: BackendApiClient,
    rec_data: dict[str, Any] | None,
) -> None:
    """Render actionable recommendations with approve/reject controls and impact stats."""
    st.subheader("🎯 Actionable Dispatch Recommendations & Approval")

    if not rec_data or "recommendations" not in rec_data:
        st.info("No recommendations available.")
        return

    all_recs = rec_data.get("recommendations", [])
    proposed_recs = [r for r in all_recs if r.get("status") == "PROPOSED"]

    if not proposed_recs:
        st.success(
            "✅ **No pending recommendations.** "
            "All station storage inventories are operating within safe parameters."
        )
        return

    st.write(f"**{len(proposed_recs)} Actionable Recommendation(s) Pending Operator Decision:**")

    for rec in proposed_recs:
        rec_id = rec.get("recommendation_id", "")
        station_name = rec.get("station_name", rec.get("station_id", "Unknown"))
        fuel = rec.get("fuel_type", "UNKNOWN")
        quantity = rec.get("quantity_liters", 0.0)
        source_depot = rec.get("source_depot_name", rec.get("source_depot_id", "Unknown"))
        route_id = rec.get("route_id", "Unknown")
        p_before = rec.get("stockout_probability_before", 0.0) * 100.0
        p_after = rec.get("stockout_probability_after", 0.0) * 100.0
        p_drop = max(0.0, p_before - p_after)
        cover_hours = rec.get("hours_of_cover_added", 0.0)
        unmet_red = rec.get("expected_unmet_reduction_liters", 0.0)
        mitigation = rec.get("mitigation_type", "FULL_PREVENTION")
        constraints = rec.get("constraints", {})
        bottleneck = constraints.get("bottleneck_constraint", "UNKNOWN")
        needs_review = rec.get("needs_human_review", False)
        alternatives = rec.get("alternatives", [])

        with st.container(border=True):
            col_hdr1, col_hdr2 = st.columns([3, 1])
            with col_hdr1:
                st.markdown(f"#### 🚚 **Dispatch {quantity:,.0f} L of `{fuel}` to {station_name}**")
                dep_tick = rec.get("departure_tick", 0)
                arr_tick = rec.get("expected_arrival_tick", 0)
                trans_ticks = rec.get("transit_ticks", 0)
                st.caption(
                    f"**Route:** `{route_id}` from **{source_depot}** | "
                    f"Departure: Tick {dep_tick} ➔ Arrival: Tick {arr_tick} "
                    f"({trans_ticks} ticks transit)"
                )
            with col_hdr2:
                if mitigation == "FULL_PREVENTION":
                    st.success("🛡️ FULL PREVENTION")
                elif mitigation == "PARTIAL_MITIGATION":
                    st.warning("⏱️ PARTIAL MITIGATION")
                else:
                    st.info("🔄 RESTOCK")

                if needs_review:
                    st.error("⚠️ HUMAN REVIEW MANDATED")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric(
                "Stockout Risk",
                f"{p_after:.1f}%",
                f"-{p_drop:.1f}% drop",
                delta_color="inverse",
            )
            m2.metric("Cover Added", f"+{cover_hours:.1f} hrs")
            m3.metric("Unmet Liters Prevented", f"{unmet_red:,.0f} L")
            m4.metric("Bottleneck Constraint", bottleneck.replace("_", " "))

            st.markdown(f"*{rec.get('explanation', '')}*")

            with st.expander("🔍 Constraints Breakdown & Runner-up Alternatives"):
                c_col1, c_col2 = st.columns(2)
                with c_col1:
                    st.markdown("**Evaluated Operational Bounds:**")
                    r_max = constraints.get("route_max_shipment", 0)
                    st.markdown(f"- **Route Max Shipment:** {r_max:,.0f} L")
                    st.markdown(
                        f"- **Depot Stock (above reserve):** "
                        f"{constraints.get('depot_available_inventory', 0):,.0f} L"
                    )
                    st.markdown(
                        f"- **Depot Dispatch Remaining:** "
                        f"{constraints.get('depot_remaining_dispatch_capacity', 0):,.0f} L"
                    )
                    st.markdown(
                        f"- **Station Free Ullage:** "
                        f"{constraints.get('station_effective_free_space', 0):,.0f} L"
                    )
                    st.markdown(
                        f"- **Deficit / Amount Needed:** "
                        f"{constraints.get('amount_needed', 0):,.0f} L"
                    )
                    st.markdown(f"- **Binding Constraint:** `{bottleneck}`")
                with c_col2:
                    st.markdown(f"**Feasible Alternatives Evaluated ({len(alternatives)}):**")
                    if alternatives:
                        for idx, alt in enumerate(alternatives, 1):
                            st.markdown(
                                f"**Option #{idx}:** Route `{alt.get('route_id')}` "
                                f"via {alt.get('source_depot_name')}  \n"
                                f"- Volume: {alt.get('quantity_liters', 0):,.0f} L | "
                                f"Transit: {alt.get('transit_ticks', 0)} ticks  \n"
                                f"- *Why not chosen:* {alt.get('reason_not_chosen')}"
                            )
                    else:
                        st.caption("No viable alternative routes available for this station.")

            btn_col1, btn_col2, note_col = st.columns([1, 1, 2])
            with note_col:
                op_notes = st.text_input(
                    "Operator Justification (optional):",
                    key=f"note_{rec_id}",
                    placeholder="Enter reason or operational context...",
                    label_visibility="collapsed",
                )
            with btn_col1:
                if st.button(
                    "✅ Approve & Dispatch",
                    key=f"app_{rec_id}",
                    use_container_width=True,
                    type="primary",
                ):
                    resp = client.approve_recommendation(rec_id, operator_notes=op_notes or None)
                    if resp.success:
                        alloc_id = resp.data.get("recommendation", {}).get(
                            "simulator_allocation_id"
                        )
                        st.toast(
                            f"✅ Approved and dispatched allocation #{alloc_id}!",
                            icon="🚚",
                        )
                        st.rerun()
                    else:
                        st.error(f"❌ Dispatch failed: {resp.error}")
            with btn_col2:
                if st.button(
                    "❌ Reject",
                    key=f"rej_{rec_id}",
                    use_container_width=True,
                ):
                    resp = client.reject_recommendation(rec_id, operator_notes=op_notes or None)
                    if resp.success:
                        st.toast(
                            "🚫 Recommendation rejected and recorded in audit.",
                            icon="📝",
                        )
                        st.rerun()
                    else:
                        st.error(f"❌ Rejection failed: {resp.error}")


def render_decision_audit_ledger(
    client: BackendApiClient,
    audit_data: dict[str, Any] | None,
    stats_data: dict[str, Any] | None,
) -> None:
    """Render SQLite audit ledger KPIs, decision history, and event timeline."""
    st.subheader("📜 Decision Audit Ledger & Operator Traceability")

    stats = stats_data.get("stats", {}) if stats_data else {}
    total_recs = stats.get("total_recommendations", 0)
    submitted_count = stats.get("submitted_count", 0)
    rejected_count = stats.get("rejected_count", 0)
    failed_count = stats.get("failed_count", 0)
    rejection_rate = stats.get("rejection_rate", 0.0) * 100.0

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Formulated", f"{total_recs}")
    k2.metric("Submitted", f"{submitted_count}")
    k3.metric("Rejected", f"{rejected_count}")
    k4.metric("Failed", f"{failed_count}")
    k5.metric("Rejection Rate", f"{rejection_rate:.1f}%")

    st.markdown("---")
    st.markdown("#### 📋 Historical Recommendations & Decision Outcomes")

    all_recs_resp = client.get_recommendations(limit=100)
    if all_recs_resp.success and all_recs_resp.data:
        recs = all_recs_resp.data.get("recommendations", [])
        if recs:
            table_rows = []
            for r in recs:
                table_rows.append(
                    {
                        "Created (UTC)": r.get("created_at", "")[:19].replace("T", " "),
                        "Station": r.get("station_name", r.get("station_id", "")),
                        "Fuel": r.get("fuel_type", ""),
                        "Liters": f"{r.get('quantity_liters', 0):,.0f}",
                        "Depot": r.get("source_depot_name", r.get("source_depot_id", "")),
                        "Risk Before": f"{r.get('stockout_probability_before', 0) * 100:.0f}%",
                        "Risk After": f"{r.get('stockout_probability_after', 0) * 100:.0f}%",
                        "Status": r.get("status", ""),
                        "Alloc ID": r.get("simulator_allocation_id") or "—",
                        "Failure / Notes": (
                            r.get("failure_reason") or r.get("operator_notes") or "—"
                        ),
                    }
                )
            df_recs = pd.DataFrame(table_rows)
            st.dataframe(df_recs, use_container_width=True, hide_index=True)
        else:
            st.caption("No historical recommendations recorded in audit ledger.")
    else:
        st.info("Unable to query recommendation history.")

    st.markdown("---")
    st.markdown("#### ⏱️ State Transition Audit Event Log")
    events = audit_data.get("events", []) if audit_data else []
    if events:
        event_rows = []
        for e in events:
            event_rows.append(
                {
                    "Timestamp (UTC)": e.get("timestamp", "")[:19].replace("T", " "),
                    "Event": e.get("event_type", ""),
                    "Transition": f"{e.get('from_status') or 'NEW'} ➔ {e.get('to_status', '')}",
                    "Recommendation ID": e.get("recommendation_id", "")[:28] + "...",
                    "Operator Notes": e.get("operator_notes") or "—",
                }
            )
        df_events = pd.DataFrame(event_rows)
        st.dataframe(df_events, use_container_width=True, hide_index=True)
    else:
        st.caption("No audit events recorded yet.")


# Main dynamic dashboard fragment that auto-refreshes
@st.fragment(run_every=2)
def render_dashboard_fragment(backend_url: str, severity_filter: str) -> None:
    """Auto-refreshing dashboard fragment using modern Streamlit st.fragment."""
    client = BackendApiClient(base_url=backend_url, timeout_seconds=4.0)

    # Fetch health first to test connectivity
    health_resp: ApiResponse = client.get_health()
    if not health_resp.is_connected:
        render_operational_mode_banner(None)
        st.markdown("---")
        render_status_panel(None, None, None)
        st.info(
            "💡 **Backend API is offline or unreachable.**  \n"
            "Run `uv run uvicorn app.api.main:app --port 8001` or verify your network connection."
        )
        return

    # Fetch state, forecasts, and risks concurrently/sequentially
    state_resp: ApiResponse = client.get_state()
    forecast_resp: ApiResponse = client.get_forecasts()
    risk_resp: ApiResponse = client.get_risks()
    rec_resp: ApiResponse = client.get_recommendations()
    audit_resp: ApiResponse = client.get_audit_history(limit=50)
    stats_resp: ApiResponse = client.get_audit_stats()

    health_data = health_resp.data if health_resp.success else None
    state_data = state_resp.data if state_resp.success else None
    forecast_data = forecast_resp.data if forecast_resp.success else None
    risk_data = risk_resp.data if risk_resp.success else None
    rec_data = rec_resp.data if rec_resp.success else None
    audit_data = audit_resp.data if audit_resp.success else None
    stats_data = stats_resp.data if stats_resp.success else None

    # Top: Operational Mode Banner
    render_operational_mode_banner(health_data)

    st.markdown("---")

    # Status Panel KPIs
    render_status_panel(health_data, forecast_data, risk_data)

    st.markdown("---")

    # Actionable Dispatch Recommendations & Approval
    render_recommendations_deck(client, rec_data)

    st.markdown("---")

    # Priority Urgency Alerts
    render_urgency_alerts(risk_data, severity_filter)

    st.markdown("---")

    # Tabbed Operational Views
    tab0, tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
        [
            "🎯 Recommendations & Approvals",
            "📊 Risk Matrix",
            "🏪 Station Tanks",
            "🏭 Bulk Depots",
            "🚚 Logistics Routes",
            "⚡ Active Events",
            "📜 Decision Audit Ledger",
        ]
    )

    with tab0:
        render_recommendations_deck(client, rec_data)

    with tab1:
        render_risk_matrix(risk_data, severity_filter)

    with tab2:
        render_stations_inventory(state_data)

    with tab3:
        render_depot_status(state_data, risk_data)

    with tab4:
        render_route_status(state_data)

    with tab5:
        render_active_events(state_data)

    with tab6:
        render_decision_audit_ledger(client, audit_data, stats_data)

    # Footer timestamp
    st.caption(
        f"Last auto-refresh: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')} | "
        f"Backend: `{backend_url}`"
    )


def main() -> None:
    """Main Streamlit application entrypoint."""
    st.title("⛽ Fuel Supply Intelligence & Resilience Platform")
    st.markdown(
        "*Real-time decision support, predictive stockout prevention, and operational resilience.*"
    )

    # Sidebar configuration and controls
    with st.sidebar:
        st.header("⚙️ Platform Controls")

        backend_url = st.text_input(
            "Backend API URL",
            value=get_default_backend_url(),
            help="Base URL of the running Fuel Intelligence FastAPI backend.",
        )

        severity_filter = st.selectbox(
            "Filter Alerts by Severity",
            options=["ALL", "CRITICAL", "WATCH", "OK"],
            index=0,
            help="Filter operational alerts and risk tables by severity level.",
        )

        st.markdown("---")

        if st.button("🔄 Force Sync with Simulator", use_container_width=True):
            client = BackendApiClient(base_url=backend_url, timeout_seconds=6.0)
            sync_resp = client.trigger_sync()
            if sync_resp.success:
                st.toast(
                    f"✅ Synchronized at tick {sync_resp.data.get('tick')}",
                    icon="🔄",
                )
            else:
                st.toast(f"❌ Sync failed: {sync_resp.error}", icon="⚠️")

        if st.button("🎯 Formulate Dispatch Recommendations", use_container_width=True):
            client = BackendApiClient(base_url=backend_url, timeout_seconds=8.0)
            rec_resp = client.generate_recommendations()
            if rec_resp.success:
                total_recs = rec_resp.data.get("total_count", 0)
                st.toast(
                    f"✅ Formulated {total_recs} recommendation(s)!",
                    icon="🎯",
                )
            else:
                st.toast(f"❌ Planning failed: {rec_resp.error}", icon="⚠️")

        st.markdown("---")
        st.markdown("### 📋 Quick Reference")
        st.markdown(
            """
            - **NORMAL**: Fully healthy, simulator connected.
            - **DEGRADED_CACHED**: Breaker tripped; serving cached state.
            - **DEGRADED_FALLBACK**: Safe heuristic fallback active.
            - **CRITICAL**: Stockout < 3h or tank < 20%.
            - **WATCH**: Stockout < 8h or demand surge.
            """
        )

    # Render auto-refreshing dashboard fragment
    render_dashboard_fragment(backend_url, severity_filter)


if __name__ == "__main__":
    main()
