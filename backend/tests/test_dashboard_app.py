"""Unit tests for the Streamlit dashboard rendering components and view functions."""

from unittest.mock import MagicMock, patch

from dashboard.app import (
    get_default_backend_url,
    render_active_events,
    render_dashboard_fragment,
    render_depot_status,
    render_operational_mode_banner,
    render_risk_matrix,
    render_route_status,
    render_stations_inventory,
    render_status_panel,
    render_urgency_alerts,
)
from dashboard.client import ApiResponse


def test_get_default_backend_url(monkeypatch) -> None:
    monkeypatch.delenv("BACKEND_API_URL", raising=False)
    assert get_default_backend_url() == "http://127.0.0.1:8001"

    monkeypatch.setenv("BACKEND_API_URL", "http://custom-host:9000")
    assert get_default_backend_url() == "http://custom-host:9000"


def test_render_operational_mode_banner() -> None:
    # 1. Disconnected
    with patch("streamlit.error") as mock_err:
        render_operational_mode_banner(None)
        mock_err.assert_called_once()
        assert "DISCONNECTED" in mock_err.call_args[0][0]

    # 2. NORMAL
    with patch("streamlit.success") as mock_succ:
        render_operational_mode_banner({
            "mode": "NORMAL",
            "reason": "Optimal health",
            "simulator": {"circuit_state": "CLOSED"},
            "store": {"data_age_seconds": 1.2},
        })
        mock_succ.assert_called_once()
        assert "MODE: NORMAL" in mock_succ.call_args[0][0]

    # 3. DEGRADED_CACHED
    with patch("streamlit.warning") as mock_warn:
        render_operational_mode_banner({
            "mode": "DEGRADED_CACHED",
            "reason": "Simulator 503 fault",
            "simulator": {"circuit_state": "OPEN"},
            "store": {"data_age_seconds": 14.5},
        })
        mock_warn.assert_called_once()
        assert "MODE: DEGRADED_CACHED" in mock_warn.call_args[0][0]

    # 4. DEGRADED_FALLBACK
    with patch("streamlit.error") as mock_err:
        render_operational_mode_banner({
            "mode": "DEGRADED_FALLBACK",
            "reason": "Forecaster failed",
            "simulator": {"circuit_state": "CLOSED"},
            "store": {},
        })
        mock_err.assert_called_once()
        assert "MODE: DEGRADED_FALLBACK" in mock_err.call_args[0][0]


def test_render_status_panel() -> None:
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

    with patch("streamlit.columns") as mock_cols, patch("streamlit.metric") as mock_metric:
        mock_cols.return_value = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        render_status_panel(health, forecast, risk)
        assert mock_metric.call_count == 4


def test_render_urgency_alerts() -> None:
    risk_data = {
        "active_alerts": [
            {
                "station_id": "st-1",
                "station_name": "Station North",
                "fuel": "gasoline",
                "severity": "CRITICAL",
                "urgency_score": 95.0,
                "hours_to_stockout": 2.1,
                "stockout_tick": 8,
                "is_unavoidable_stockout": True,
                "unavoidable_shortage_hours": 1.2,
                "confidence": 0.85,
                "confidence_tier": "HIGH",
                "needs_human_review": True,
                "drivers": {
                    "initial_inventory": 2000.0,
                    "capacity": 20000.0,
                    "burn_rate_liters_per_hour": 1000.0,
                    "incoming_shipments_count": 0,
                    "explanation": "Critical inventory level",
                },
            },
            {
                "station_id": "st-2",
                "station_name": "Station South",
                "fuel": "diesel",
                "severity": "WATCH",
                "urgency_score": 50.0,
                "hours_to_stockout": 6.0,
                "stockout_tick": 24,
                "is_unavoidable_stockout": False,
                "confidence": 0.90,
                "confidence_tier": "HIGH",
                "needs_human_review": False,
                "drivers": {
                    "initial_inventory": 6000.0,
                    "capacity": 20000.0,
                    "burn_rate_liters_per_hour": 800.0,
                    "incoming_shipments_count": 1,
                    "explanation": "Approaching threshold",
                },
            },
        ]
    }

    # Test ALL filter
    with (
        patch("streamlit.markdown") as mock_md,
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.progress"),
    ):
        mock_cols.return_value = [MagicMock(), MagicMock(), MagicMock()]
        render_urgency_alerts(risk_data, severity_filter="ALL")
        assert mock_md.called

    # Test CRITICAL filter
    with (
        patch("streamlit.markdown") as mock_md,
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.progress"),
    ):
        mock_cols.return_value = [MagicMock(), MagicMock(), MagicMock()]
        render_urgency_alerts(risk_data, severity_filter="CRITICAL")
        assert mock_md.called

    # Test Empty / OK
    with patch("streamlit.success") as mock_succ:
        render_urgency_alerts({"active_alerts": []}, severity_filter="ALL")
        mock_succ.assert_called_once()


def test_render_risk_matrix() -> None:
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
        render_risk_matrix(risk_data, severity_filter="ALL")
        mock_df.assert_called_once()


def test_render_stations_inventory() -> None:
    state_data = {
        "snapshot": {
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
    }

    with (
        patch("streamlit.expander") as mock_exp,
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.metric") as mock_metric,
        patch("streamlit.progress"),
    ):
        mock_exp.return_value.__enter__.return_value = MagicMock()
        mock_cols.return_value = [MagicMock(), MagicMock()]
        render_stations_inventory(state_data)
        assert mock_metric.call_count == 2


def test_render_depot_status() -> None:
    state_data = {
        "snapshot": {
            "depots": [
                {
                    "id": "dp-1",
                    "name": "Central Depot",
                    "status": "OPEN",
                    "dispatch_capacity": 5000.0,
                    "current_dispatch": 1000.0,
                    "inventory": {"gasoline": 45000.0},
                    "capacity": {"gasoline": 50000.0},
                }
            ]
        }
    }
    risk_data = {
        "depot_risks": {
            "dp-1": {
                "gasoline": {
                    "is_reserve_critical": False,
                    "dispatchable_inventory": 40000.0,
                }
            }
        }
    }

    with (
        patch("streamlit.expander") as mock_exp,
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.metric") as mock_metric,
        patch("streamlit.progress"),
    ):
        mock_exp.return_value.__enter__.return_value = MagicMock()
        mock_cols.return_value = [MagicMock()]
        render_depot_status(state_data, risk_data)
        assert mock_metric.call_count == 1


def test_render_route_status_disrupted_warning() -> None:
    state_data = {
        "snapshot": {
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
    }

    with patch("streamlit.error") as mock_err, patch("streamlit.dataframe") as mock_df:
        render_route_status(state_data)
        mock_err.assert_called_once()
        assert "ACTIVE ROUTE DISRUPTION" in mock_err.call_args[0][0]
        mock_df.assert_called_once()


def test_render_active_events() -> None:
    state_data = {
        "snapshot": {
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
    }

    with patch("streamlit.dataframe") as mock_df:
        render_active_events(state_data)
        mock_df.assert_called_once()


def test_render_dashboard_fragment_offline() -> None:
    mock_client = MagicMock()
    mock_client.get_health.return_value = ApiResponse(
        success=False, is_connected=False, error="Connection refused"
    )

    with (
        patch("dashboard.app.BackendApiClient", return_value=mock_client),
        patch("streamlit.error") as mock_err,
        patch("streamlit.info") as mock_info,
        patch("streamlit.markdown"),
    ):
        render_dashboard_fragment.__wrapped__("http://127.0.0.1:8001", "ALL")
        mock_err.assert_called_once()
        assert "DISCONNECTED" in mock_err.call_args[0][0]
        mock_info.assert_called_once()
        assert "Backend API is offline" in mock_info.call_args[0][0]


def test_render_dashboard_fragment_connected() -> None:
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

    with (
        patch("dashboard.app.BackendApiClient", return_value=mock_client),
        patch("streamlit.success") as mock_succ,
        patch("streamlit.markdown"),
        patch("streamlit.tabs") as mock_tabs,
        patch("streamlit.columns") as mock_cols,
        patch("streamlit.caption"),
    ):
        mock_tabs.return_value = [
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]
        mock_cols.return_value = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        render_dashboard_fragment.__wrapped__("http://127.0.0.1:8001", "ALL")
        assert mock_succ.called
