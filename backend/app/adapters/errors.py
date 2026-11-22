"""Custom exceptions and error-shape parsers for the Simulator Adapter boundary.

The BUP Fuel Supply Simulator and upstream proxies return errors in three distinct shapes:
1. Chaos / Fault errors: `{"error": {"code": "...", "message": "..."}}`
2. Domain logic errors: `{"detail": {"code": "...", "message": "..."}}`
3. FastAPI/Pydantic validation errors: `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`

This module standardizes all three formats into typed exceptions.
"""

from typing import Any


class SimulatorError(Exception):
    """Base exception for all simulator adapter communication and protocol failures."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str = "SIMULATOR_ERROR",
        raw_payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.raw_payload = raw_payload

    def __str__(self) -> str:
        status_part = f" [HTTP {self.status_code}]" if self.status_code else ""
        return f"{self.code}{status_part}: {self.message}"


class SimulatorConnectionError(SimulatorError):
    """Network connection failure, socket error, or host unreachable."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str = "CONNECTION_ERROR",
        raw_payload: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, raw_payload=raw_payload)


class SimulatorTimeoutError(SimulatorConnectionError):
    """Request exceeded configured timeout threshold."""

    def __init__(
        self,
        message: str = "Simulator request timed out",
        status_code: int | None = 408,
        code: str = "TIMEOUT",
        raw_payload: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, raw_payload=raw_payload)


class SimulatorUnavailableError(SimulatorError):
    """Simulator service unavailable (503), e.g. server down or injected fault."""

    def __init__(
        self,
        message: str = "Simulator service unavailable",
        status_code: int | None = 503,
        code: str = "SERVICE_UNAVAILABLE",
        raw_payload: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, raw_payload=raw_payload)


class SimulatorDomainError(SimulatorError):
    """Domain rule violation (e.g. route congested, depot not found, capacity exceeded)."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str = "DOMAIN_ERROR",
        raw_payload: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, raw_payload=raw_payload)


class SimulatorValidationError(SimulatorError):
    """FastAPI / Pydantic schema validation failure (HTTP 422)."""

    def __init__(
        self,
        message: str,
        status_code: int = 422,
        code: str = "VALIDATION_ERROR",
        validation_errors: list[dict[str, Any]] | None = None,
        raw_payload: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, raw_payload=raw_payload)
        self.validation_errors = validation_errors or []


class SimulatorCircuitOpenError(SimulatorError):
    """Operation rejected immediately because circuit breaker is OPEN."""

    def __init__(
        self,
        message: str = "Simulator circuit breaker is OPEN; request fast-failed",
        status_code: int = 503,
        code: str = "CIRCUIT_OPEN",
    ) -> None:
        super().__init__(message, status_code=status_code, code=code)


def parse_simulator_error(
    status_code: int,
    payload: Any,
    raw_text: str = "",
) -> SimulatorError:
    """Parse simulator error response into a typed SimulatorError subclass.

    Handles all 3 error formats emitted by the simulator:
    1. {"error": {"code": ..., "message": ...}} (chaos engine / injected faults)
    2. {"detail": {"code": ..., "message": ...}} (domain rule rejections)
    3. {"detail": [{"loc": [...], "msg": ...}]} (FastAPI 422 validation errors)
    """
    # 1. Chaos / Injected Fault format: {"error": {"code": ..., "message": ...}}
    if isinstance(payload, dict) and "error" in payload and isinstance(payload["error"], dict):
        err = payload["error"]
        code = str(err.get("code", "FAULT_INJECTED"))
        msg = str(err.get("message", "Simulator error"))
        if status_code == 503 or code == "FAULT_INJECTED":
            return SimulatorUnavailableError(
                message=msg,
                status_code=status_code,
                code=code,
                raw_payload=payload,
            )
        return SimulatorError(
            message=msg,
            status_code=status_code,
            code=code,
            raw_payload=payload,
        )

    # 2. Domain error format: {"detail": {"code": ..., "message": ...}}
    if isinstance(payload, dict) and "detail" in payload and isinstance(payload["detail"], dict):
        det = payload["detail"]
        code = str(det.get("code", "DOMAIN_ERROR"))
        msg = str(det.get("message", "Domain operation rejected"))
        return SimulatorDomainError(
            message=msg,
            status_code=status_code,
            code=code,
            raw_payload=payload,
        )

    # 3. FastAPI 422 Validation format: {"detail": [{"loc": [...], "msg": ...}]}
    if isinstance(payload, dict) and "detail" in payload and isinstance(payload["detail"], list):
        val_list = payload["detail"]
        formatted_msgs = []
        for item in val_list:
            if isinstance(item, dict):
                loc = " -> ".join(str(elem) for elem in item.get("loc", []))
                field_msg = item.get("msg", "Invalid value")
                formatted_msgs.append(f"{loc}: {field_msg}" if loc else field_msg)
            else:
                formatted_msgs.append(str(item))

        summary_msg = f"Validation failed ({'; '.join(formatted_msgs)})"
        return SimulatorValidationError(
            message=summary_msg,
            status_code=status_code,
            code="VALIDATION_ERROR",
            validation_errors=val_list,
            raw_payload=payload,
        )

    # 4. Standard string detail format: {"detail": "..."}
    if isinstance(payload, dict) and "detail" in payload and isinstance(payload["detail"], str):
        msg = payload["detail"]
        if status_code == 503:
            return SimulatorUnavailableError(
                message=msg,
                status_code=status_code,
                code="SERVICE_UNAVAILABLE",
                raw_payload=payload,
            )
        return SimulatorDomainError(
            message=msg,
            status_code=status_code,
            code="HTTP_ERROR",
            raw_payload=payload,
        )

    # 5. Non-JSON or unstructured fallback
    msg = raw_text.strip() if raw_text else f"Simulator returned HTTP {status_code}"
    if status_code == 503:
        return SimulatorUnavailableError(
            message=msg,
            status_code=status_code,
            code="SERVICE_UNAVAILABLE",
            raw_payload=payload,
        )
    return SimulatorError(
        message=msg,
        status_code=status_code,
        code=f"HTTP_{status_code}",
        raw_payload=payload,
    )
