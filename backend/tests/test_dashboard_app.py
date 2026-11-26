"""Unit tests for the Streamlit dashboard rendering components and view functions."""

from unittest.mock import MagicMock, patch

from dashboard.app import (
    _backend_url,
    _render_alert_card,
    _render_events,
    _render_risk_table,
    _render_routes,
    _render_stations,
    dashboard_fragment,
    render_alerts_and_risks,
    render_audit,
    render_kpis,
    render_mode_banner,
    render_network,
    render_recommendations,
)
from dashboard.client import ApiResponse


def test_backend_url_default(monkeypatch) -> None:
    monkeypatch.delenv("BACKEND_API_URL", raising=False)
    assert _backend_url() == "http://127.0.0.1:8001"

    monkeypatch.setenv("BACKEND_API_URL", "http://custom-host:9000")
    assert _backend_url() == "http://custom-host:9000"


def test_render_mode_banner_disconnected() -> None:
    with patch("streamlit.error") as mock_err:
        render_mode_banner(None)
        mock_err.assert_called_once()
        assert "DISCONNECTED" in mock_err.call_args[0][0]


def test_render_mode_banner_normal() -> None:
    with patch("streamlit.success") as mock_succ:
        render_mode_banner(
            {
                "mode": "NORMAL",
                "reason": "Optimal health",
                "simulator": {"circuit_state": "CLOSED"},
                "store": {"data_age_seconds": 1.2},
            }
        )
        mock_succ.assert_called_once()
        assert "NORMAL" in mock_succ.call_args[0][0]


def test_render_mode_banner_degraded_cached() -> None:
    with patch("streamlit.warning") as mock_warn:
        render_mode_banner(
            {
                "mode": "DEGRADED_CACHED",
                "reason": "Simulator 503 fault",
                "simulator": {"circuit_state": "OPEN"},
                "store": {"data_age_seconds": 14.5},
            }
        )
        mock_warn.assert_called_once()
        assert "DEGRADED" in mock_warn.call_args[0][0]


def test_render_mode_banner_degraded_fallback() -> None:
    with patch("streamlit.error") as mock_err:
        render_mode_banner(
            {
                "mode": "DEGRADED_FALLBACK",
                "reason": "Forecaster failed",
                "simulator": {"circuit_state": "CLOSED"},
                "store": {},
            }
        )
        mock_err.assert_called_once()
        assert "DEGRADED" in mock_err.call_args[0][0]


def test_render_kpis() -> None:
    health = {
        "simulator": {
            "connected": True,
            "circuit_state": "CLOSED",
            "consecutive_failures": 0,
            "cooldown_remaining": 0.0,
        },
        "store": {
            "has_snapshot": True,
            "is_stale": False,
            "data_age_seconds": 1.5,
            "total_updates": 20,
        },
        "poller": {"is_running": True},
    }
    forecast = {
        "total_fleet_demand_per_hour": 12500.0,
        "average_mae": 3.2,
        "average_mape": 6.5,
        "active_anomalies_count": 1,
    }
    risk = {
        "overall_severity": "WATCH",
        "critical_count": 0,
        "watch_count": 2,
        "earliest_stockout_hours": 6.5,
        "earliest_stockout_station_id": "st-north",
    }

    with (
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.metric") as mock_metric,
        patch("streamlit.caption"),
    ):
        mock_cols.return_value = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        render_kpis(health, forecast, risk, None)
        assert mock_metric.call_count == 4


def test_render_alerts_and_risks_empty() -> None:
    with patch("streamlit.success") as mock_succ, patch("streamlit.info"):
        render_alerts_and_risks(
            {"active_alerts": [], "station_risks": {}},
            severity_filter="ALL",
        )
        mock_succ.assert_called_once()


def test_render_alerts_and_risks_with_alerts() -> None:
    risk_data = {
        "active_alerts": [
            {
                "station_id": "st-1",
                "station_name": "Station North",
                "fuel": "gasoline",
                "severity": "CRITICAL",
                "urgency_score": 95.0,
                "hours_to_stockout": 2.1,
                "is_unavoidable_stockout": True,
                "needs_human_review": True,
                "drivers": {
                    "initial_inventory": 2000.0,
                    "capacity": 20000.0,
                    "burn_rate_liters_per_hour": 1000.0,
                    "explanation": "Critical inventory level",
                },
            },
        ],
        "station_risks": {},
    }

    with (
        patch("streamlit.markdown"),
        patch("streamlit.container"),
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.metric"),
        patch("streamlit.progress"),
        patch("streamlit.caption"),
        patch("streamlit.divider"),
        patch("streamlit.info"),
    ):
        mock_cols.return_value = [MagicMock(), MagicMock()]
        render_alerts_and_risks(risk_data, severity_filter="ALL")


def test_render_risk_table() -> None:
    risk_data = {
        "station_risks": {
            "st-1": {
                "gasoline": {
                    "station_name": "North",
                    "severity": "CRITICAL",
                    "fill_ratio": 0.15,
                    "current_inventory": 3000.0,
                    "capacity": 20000.0,
                    "projected_stockout_hours": 2.5,
                    "projected_unmet_liters": 5000.0,
                    "confidence": 0.9,
                    "needs_human_review": True,
                    "arrivals_count": 0,
                }
            }
        }
    }

    with patch("streamlit.dataframe") as mock_df:
        _render_risk_table(risk_data, severity_filter="ALL")
        mock_df.assert_called_once()


def test_render_stations() -> None:
    snap = {
        "stations": [
            {
                "id": "st-1",
                "name": "Station One",
                "status": "OPEN",
                "demand_multiplier": 2.0,
                "inventory": {"gasoline": 5000.0, "diesel": 8000.0},
                "capacity": {"gasoline": 20000.0, "diesel": 20000.0},
            }
        ]
    }

    with (
        patch("streamlit.expander") as mock_exp,
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.metric") as mock_metric,
        patch("streamlit.progress"),
    ):
        mock_exp.return_value.__enter__.return_value = MagicMock()
        mock_cols.return_value = [MagicMock(), MagicMock()]
        _render_stations(snap)
        assert mock_metric.call_count == 2


def test_render_routes_disrupted_warning() -> None:
    snap = {
        "routes": [
            {
                "id": "rt-1",
                "origin_id": "dp-1",
                "destination_id": "st-1",
                "status": "DISRUPTED",
                "transit_time": 45.0,
                "transit_ticks": 3,
                "max_shipment": 10000.0,
            }
        ]
    }

    with (
        patch("streamlit.error") as mock_err,
        patch("streamlit.dataframe") as mock_df,
    ):
        _render_routes(snap)
        mock_err.assert_called_once()
        assert "disrupted" in mock_err.call_args[0][0]
        mock_df.assert_called_once()


def test_render_events() -> None:
    snap = {
        "events": [
            {
                "id": "evt-1",
                "event_type": "demand_spike",
                "target_type": "station",
                "target_id": "st-1",
                "multiplier": 2.5,
                "duration_ticks": 10,
                "remaining_ticks": 4,
            }
        ]
    }

    with patch("streamlit.dataframe") as mock_df:
        _render_events(snap)
        mock_df.assert_called_once()


def test_dashboard_fragment_offline() -> None:
    mock_client = MagicMock()
    mock_client.get_health.return_value = ApiResponse(
        success=False, is_connected=False, error="Connection refused"
    )

    with (
        patch("dashboard.app.BackendApiClient", return_value=mock_client),
        patch("streamlit.error") as mock_err,
        patch("streamlit.info") as mock_info,
        patch("streamlit.warning"),
    ):
        dashboard_fragment.__wrapped__("http://127.0.0.1:8001", "ALL")
        mock_err.assert_called_once()
        assert "DISCONNECTED" in mock_err.call_args[0][0]
        mock_info.assert_called_once()


def test_dashboard_fragment_connected() -> None:
    mock_client = MagicMock()
    mock_client.get_health.return_value = ApiResponse(
        success=True,
        data={
            "mode": "NORMAL",
            "reason": "All systems operational",
            "simulator": {"connected": True, "circuit_state": "CLOSED"},
            "store": {"has_snapshot": True, "is_stale": False, "data_age_seconds": 0.8},
            "poller": {"is_running": True},
        },
    )
    mock_client.get_state.return_value = ApiResponse(
        success=True,
        data={
            "snapshot": {
                "stations": [],
                "depots": [],
                "routes": [],
                "events": [],
            }
        },
    )
    mock_client.get_forecasts.return_value = ApiResponse(
        success=True,
        data={
            "total_fleet_demand_per_hour": 10000.0,
            "average_mae": 2.0,
            "average_mape": 5.0,
            "active_anomalies_count": 0,
        },
    )
    mock_client.get_risks.return_value = ApiResponse(
        success=True,
        data={
            "overall_severity": "OK",
            "critical_count": 0,
            "watch_count": 0,
            "active_alerts": [],
            "station_risks": {},
            "depot_risks": {},
        },
    )
    mock_client.get_recommendations.return_value = ApiResponse(
        success=True,
        data={"total_count": 0, "recommendations": []},
    )
    mock_client.get_audit_history.return_value = ApiResponse(
        success=True,
        data={"total_events": 0, "events": []},
    )
    mock_client.get_audit_stats.return_value = ApiResponse(
        success=True,
        data={"stats": {"total_recommendations": 0, "rejection_rate": 0.0}},
    )
    mock_client.get_metrics.return_value = ApiResponse(
        success=True,
        data={},
    )

    with (
        patch("dashboard.app.BackendApiClient", return_value=mock_client),
        patch("streamlit.success") as mock_succ,
        patch("streamlit.markdown"),
        patch("streamlit.tabs") as mock_tabs,
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.caption"),
        patch("streamlit.metric"),
        patch("streamlit.divider"),
        patch("streamlit.info"),
    ):
        mock_tabs.return_value = [MagicMock() for _ in range(4)]
        mock_cols.side_effect = lambda spec: [
            MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
        ]
        dashboard_fragment.__wrapped__("http://127.0.0.1:8001", "ALL")
        assert mock_succ.called


def test_render_recommendations_empty() -> None:
    mock_client = MagicMock()
    with patch("streamlit.success") as mock_succ:
        render_recommendations(mock_client, {"recommendations": []})
        mock_succ.assert_called_once()
        assert "no dispatches needed" in mock_succ.call_args[0][0]


def test_render_recommendations_with_proposed() -> None:
    mock_client = MagicMock()
    rec = {
        "recommendation_id": "rec_test_1",
        "station_name": "Dhaka Central",
        "fuel_type": "DIESEL",
        "quantity_liters": 10000.0,
        "source_depot_name": "Depot North",
        "route_id": "route_1",
        "departure_tick": 10,
        "expected_arrival_tick": 12,
        "transit_ticks": 2,
        "mitigation_type": "FULL_PREVENTION",
        "urgency_score": 88.0,
        "status": "PROPOSED",
        "stockout_probability_before": 0.85,
        "stockout_probability_after": 0.15,
        "hours_of_cover_added": 4.5,
        "expected_unmet_reduction_liters": 10000.0,
        "explanation": "Urgent stockout mitigation",
        "constraints": {
            "route_max_shipment": 15000.0,
            "depot_available_inventory": 20000.0,
            "depot_remaining_dispatch_capacity": 20000.0,
            "station_effective_free_space": 12000.0,
            "amount_needed": 10000.0,
            "bottleneck_constraint": "AMOUNT_NEEDED",
        },
        "alternatives": [],
    }
    with (
        patch("streamlit.container"),
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.metric"),
        patch("streamlit.markdown"),
        patch("streamlit.caption"),
        patch("streamlit.expander"),
        patch("streamlit.text_input"),
        patch("streamlit.button", return_value=False),
        patch("streamlit.success"),
        patch("streamlit.warning"),
        patch("streamlit.info"),
    ):
        mock_cols.side_effect = lambda spec: [
            MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
        ]
        render_recommendations(mock_client, {"recommendations": [rec]})


def test_render_audit() -> None:
    mock_client = MagicMock()
    mock_client.get_recommendations.return_value = ApiResponse(
        success=True,
        data={
            "recommendations": [
                {
                    "created_at": "2026-11-23T10:00:00Z",
                    "station_name": "Dhaka Central",
                    "fuel_type": "DIESEL",
                    "quantity_liters": 10000.0,
                    "source_depot_name": "Depot North",
                    "stockout_probability_before": 0.8,
                    "stockout_probability_after": 0.2,
                    "status": "SUBMITTED",
                    "simulator_allocation_id": 123,
                    "operator_notes": "Approved",
                }
            ]
        },
    )
    with (
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.metric"),
        patch("streamlit.dataframe") as mock_df,
        patch("streamlit.markdown"),
        patch("streamlit.divider"),
        patch("streamlit.caption"),
    ):
        mock_cols.return_value = [MagicMock() for _ in range(4)]
        render_audit(
            mock_client,
            audit_data={"events": []},
            stats_data={"stats": {"total_recommendations": 1, "submitted_count": 1}},
        )
        assert mock_df.called
