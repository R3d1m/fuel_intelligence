"""Unit tests for application configuration."""

import pytest
from app.config import Settings, get_settings
from pydantic import ValidationError


def test_default_settings():
    """Verify default settings match expected baseline configuration."""
    settings = Settings()
    assert settings.simulator_base_url == "http://localhost:8000"
    assert settings.simulator_timeout_seconds == 5.0
    assert settings.simulator_max_retries == 3
    assert settings.simulator_backoff_factor == 0.5
    assert settings.poll_interval_seconds == 1.0
    assert settings.stale_threshold_seconds == 10.0
    assert settings.forecast_horizon_ticks == 24
    assert settings.forecast_ewma_alpha == 0.3
    assert settings.forecast_history_limit == 300
    assert settings.forecast_spike_threshold_ratio == 1.35
    assert settings.risk_horizon_ticks == 24
    assert settings.risk_critical_hours == 3.0
    assert settings.risk_critical_fill_ratio == 0.20
    assert settings.risk_watch_hours == 8.0
    assert settings.risk_watch_fill_ratio == 0.35
    assert settings.risk_min_confidence == 0.60
    assert settings.circuit_breaker_failure_threshold == 5
    assert settings.circuit_breaker_recovery_timeout == 10.0
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert settings.port == 8001


def test_settings_env_override(monkeypatch):
    """Verify environment variables override defaults correctly."""
    monkeypatch.setenv("SIMULATOR_BASE_URL", "http://simulator:9000")
    monkeypatch.setenv("SIMULATOR_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("APP_ENV", "production")

    settings = Settings()
    assert settings.simulator_base_url == "http://simulator:9000"
    assert settings.simulator_timeout_seconds == 2.5
    assert settings.poll_interval_seconds == 0.5
    assert settings.app_env == "production"


def test_settings_validation_constraints():
    """Verify out-of-bound settings trigger validation errors."""
    with pytest.raises(ValidationError):
        Settings(simulator_timeout_seconds=-1.0)

    with pytest.raises(ValidationError):
        Settings(poll_interval_seconds=0.0)

    with pytest.raises(ValidationError):
        Settings(circuit_breaker_failure_threshold=0)


def test_get_settings_cached():
    """Verify get_settings returns a cached instance."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
