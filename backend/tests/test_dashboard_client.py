"""Unit tests for the dashboard API client."""

import httpx
import pytest
import respx

from dashboard.client import BackendApiClient


@pytest.fixture
def client() -> BackendApiClient:
    return BackendApiClient(base_url="http://mock-backend:8001", timeout_seconds=1.0)


@respx.mock
def test_get_health_success(client: BackendApiClient) -> None:
    respx.get("http://mock-backend:8001/health").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "healthy",
                "mode": "NORMAL",
                "reason": "Healthy",
                "simulator": {"connected": True, "circuit_state": "CLOSED"},
                "store": {"has_snapshot": True, "is_stale": False},
                "poller": {"is_running": True, "poll_interval_seconds": 1.0},
                "timestamp": "2026-11-23T14:00:00Z",
            },
        )
    )

    resp = client.get_health()
    assert resp.success is True
    assert resp.is_connected is True
    assert resp.data is not None
    assert resp.data["status"] == "healthy"
    assert resp.data["mode"] == "NORMAL"


@respx.mock
def test_get_state_success(client: BackendApiClient) -> None:
    respx.get("http://mock-backend:8001/state").mock(
        return_value=httpx.Response(
            200,
            json={
                "mode": "NORMAL",
                "reason": "Healthy",
                "is_stale": False,
                "circuit_state": "CLOSED",
                "snapshot": {"tick": 10, "stations": [], "depots": [], "routes": []},
            },
        )
    )

    resp = client.get_state()
    assert resp.success is True
    assert resp.data is not None
    assert resp.data["snapshot"]["tick"] == 10


@respx.mock
def test_get_state_503_degraded_handling(client: BackendApiClient) -> None:
    respx.get("http://mock-backend:8001/state").mock(
        return_value=httpx.Response(
            503,
            json={
                "detail": {
                    "message": "No simulator snapshot available yet.",
                    "mode": "DEGRADED_CACHED",
                }
            },
        )
    )

    resp = client.get_state()
    assert resp.success is False
    assert resp.status_code == 503
    assert "No simulator snapshot available yet" in (resp.error or "")


@respx.mock
def test_get_forecasts_and_risks_success(client: BackendApiClient) -> None:
    respx.get("http://mock-backend:8001/forecasts").mock(
        return_value=httpx.Response(
            200,
            json={
                "mode": "NORMAL",
                "total_fleet_demand_per_hour": 1000.0,
                "average_mae": 2.5,
                "average_mape": 4.1,
                "active_anomalies_count": 0,
                "active_anomalies": [],
                "forecasts": {},
            },
        )
    )
    respx.get("http://mock-backend:8001/risks").mock(
        return_value=httpx.Response(
            200,
            json={
                "mode": "NORMAL",
                "overall_severity": "OK",
                "critical_count": 0,
                "watch_count": 0,
                "ok_count": 5,
                "active_alerts": [],
                "station_risks": {},
                "depot_risks": {},
            },
        )
    )

    fc_resp = client.get_forecasts()
    assert fc_resp.success is True
    assert fc_resp.data["average_mae"] == 2.5

    rk_resp = client.get_risks()
    assert rk_resp.success is True
    assert rk_resp.data["overall_severity"] == "OK"


@respx.mock
def test_trigger_sync_post(client: BackendApiClient) -> None:
    respx.post("http://mock-backend:8001/sync").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "tick": 15, "message": "Synced."},
        )
    )

    resp = client.trigger_sync()
    assert resp.success is True
    assert resp.data["tick"] == 15


@respx.mock
def test_client_connect_error(client: BackendApiClient) -> None:
    respx.get("http://mock-backend:8001/health").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    resp = client.get_health()
    assert resp.success is False
    assert resp.is_connected is False
    assert "Cannot connect to backend" in (resp.error or "")


@respx.mock
def test_client_timeout_error(client: BackendApiClient) -> None:
    respx.get("http://mock-backend:8001/state").mock(
        side_effect=httpx.TimeoutException("Read timed out")
    )

    resp = client.get_state()
    assert resp.success is False
    assert resp.is_connected is False
    assert "timed out" in (resp.error or "")


@respx.mock
def test_recommendations_and_audit_methods(client: BackendApiClient) -> None:
    base = "http://mock-backend:8001"

    # get_recommendations
    respx.get(f"{base}/recommendations").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "recommendations": []})
    )
    rec_resp = client.get_recommendations(status="PROPOSED")
    assert rec_resp.success is True
    assert rec_resp.data["total_count"] == 1

    # generate_recommendations
    respx.post(f"{base}/recommendations/generate").mock(
        return_value=httpx.Response(200, json={"total_count": 2, "recommendations": []})
    )
    gen_resp = client.generate_recommendations()
    assert gen_resp.success is True
    assert gen_resp.data["total_count"] == 2

    # approve_recommendation
    respx.post(f"{base}/recommendations/rec_1/approve").mock(
        return_value=httpx.Response(
            200,
            json={"recommendation": {"recommendation_id": "rec_1", "status": "SUBMITTED"}},
        )
    )
    app_resp = client.approve_recommendation("rec_1", operator_notes="Approve test")
    assert app_resp.success is True
    assert app_resp.data["recommendation"]["status"] == "SUBMITTED"

    # reject_recommendation
    respx.post(f"{base}/recommendations/rec_1/reject").mock(
        return_value=httpx.Response(
            200,
            json={"recommendation": {"recommendation_id": "rec_1", "status": "REJECTED"}},
        )
    )
    rej_resp = client.reject_recommendation("rec_1", operator_notes="Reject test")
    assert rej_resp.success is True
    assert rej_resp.data["recommendation"]["status"] == "REJECTED"

    # get_audit_history
    respx.get(f"{base}/decisions/audit").mock(
        return_value=httpx.Response(200, json={"total_events": 5, "events": []})
    )
    audit_resp = client.get_audit_history(limit=10)
    assert audit_resp.success is True
    assert audit_resp.data["total_events"] == 5

    # get_audit_stats
    respx.get(f"{base}/decisions/stats").mock(
        return_value=httpx.Response(200, json={"stats": {"total_recommendations": 3}})
    )
    stats_resp = client.get_audit_stats()
    assert stats_resp.success is True
    assert stats_resp.data["stats"]["total_recommendations"] == 3
