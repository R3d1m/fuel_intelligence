"""Locust load testing script for the Fuel Intelligence Backend API.

Simulates mixed operator and dashboard traffic across:
- GET /state (authoritative snapshot & operational mode)
- GET /risks (fleet-wide inventory risk projections)
- GET /recommendations (dispatch recommendations list)
- GET /health (liveness & dependency health)

Usage (headless):
    uv run locust -f tools/loadtest/locustfile.py --headless \
        --host http://localhost:8001 -u 50 -r 10 -t 15s --csv tools/loadtest/results
"""

from __future__ import annotations

from locust import HttpUser, between, task


class FuelIntelligenceUser(HttpUser):
    """Simulated operator/dashboard client issuing mixed read traffic."""

    wait_time = between(0.1, 0.5)

    @task(4)
    def get_state(self) -> None:
        """Poll the authoritative platform state snapshot."""
        self.client.get("/state", name="/state")

    @task(3)
    def get_risks(self) -> None:
        """Fetch fleet-wide stockout risk assessments."""
        self.client.get("/risks", name="/risks")

    @task(2)
    def get_recommendations(self) -> None:
        """Fetch active dispatch recommendations."""
        self.client.get("/recommendations", name="/recommendations")

    @task(1)
    def get_health(self) -> None:
        """Check platform liveness and operational mode."""
        self.client.get("/health", name="/health")
