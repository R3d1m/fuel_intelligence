"""Unit tests for simulator adapter error parsing and exception types."""

from app.adapters.errors import (
    SimulatorDomainError,
    SimulatorError,
    SimulatorUnavailableError,
    SimulatorValidationError,
    parse_simulator_error,
)


def test_parse_chaos_injected_fault_shape():
    """Verify Shape 1: {"error": {"code": ..., "message": ...}}."""
    payload = {
        "error": {
            "code": "FAULT_INJECTED",
            "message": "Simulator API temporarily unavailable.",
        }
    }
    err = parse_simulator_error(status_code=503, payload=payload)
    assert isinstance(err, SimulatorUnavailableError)
    assert err.code == "FAULT_INJECTED"
    assert "temporarily unavailable" in err.message
    assert err.status_code == 503
    assert err.raw_payload == payload


def test_parse_domain_error_shape():
    """Verify Shape 2: {"detail": {"code": ..., "message": ...}}."""
    payload = {
        "detail": {
            "code": "DESTINATION_CAPACITY_EXCEEDED",
            "message": "Allocation volume exceeds destination station free capacity",
        }
    }
    err = parse_simulator_error(status_code=409, payload=payload)
    assert isinstance(err, SimulatorDomainError)
    assert err.code == "DESTINATION_CAPACITY_EXCEEDED"
    assert "exceeds destination station" in err.message
    assert err.status_code == 409


def test_parse_fastapi_validation_error_shape():
    """Verify Shape 3: {"detail": [{"loc": [...], "msg": ...}]}."""
    payload = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "quantity"],
                "msg": "Field required",
                "input": {},
            },
            {
                "type": "greater_than",
                "loc": ["body", "quantity"],
                "msg": "Input should be greater than 0",
                "input": -5,
            },
        ]
    }
    err = parse_simulator_error(status_code=422, payload=payload)
    assert isinstance(err, SimulatorValidationError)
    assert err.code == "VALIDATION_ERROR"
    assert err.status_code == 422
    assert "body -> quantity: Field required" in err.message
    assert len(err.validation_errors) == 2


def test_parse_string_detail_format():
    """Verify fallback when detail is a plain string."""
    payload = {"detail": "Internal resource locking failure"}
    err = parse_simulator_error(status_code=500, payload=payload)
    assert isinstance(err, SimulatorDomainError)
    assert err.message == "Internal resource locking failure"
    assert err.status_code == 500


def test_parse_unstructured_fallback():
    """Verify fallback for non-JSON or HTML 503 responses."""
    err = parse_simulator_error(
        status_code=503,
        payload=None,
        raw_text="503 Service Temporarily Unavailable",
    )
    assert isinstance(err, SimulatorUnavailableError)
    assert err.status_code == 503
    assert "503 Service Temporarily Unavailable" in err.message


def test_simulator_error_string_representation():
    """Verify __str__ format for logging readability."""
    err = SimulatorError(
        message="Route congested",
        status_code=409,
        code="ROUTE_CONGESTED",
    )
    assert str(err) == "ROUTE_CONGESTED [HTTP 409]: Route congested"
