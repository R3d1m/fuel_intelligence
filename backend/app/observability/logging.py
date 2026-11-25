"""Structured JSON logging configuration and domain event emitter.

Formats application logs as single-line JSON records with explicit `event`
names (e.g. `decision.approved`, `simulator.breaker_open`, `mode.changed`)
and structured context fields for log aggregation and incident debugging.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JsonLogFormatter(logging.Formatter):
    """Logging formatter that outputs structured single-line JSON records."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        event = getattr(record, "event", None)
        if event:
            payload["event"] = str(event)

        context = getattr(record, "context", None)
        if isinstance(context, dict) and context:
            payload["context"] = context

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure root logging with structured JSON or plain text formatting."""
    root_logger = logging.getLogger()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # Avoid adding duplicate stream handlers on repeated app creation
    for handler in list(root_logger.handlers):
        if getattr(handler, "_fuel_intelligence_managed", False):
            root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)
    handler._fuel_intelligence_managed = True

    if json_format:
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )

    root_logger.addHandler(handler)


def log_event(
    logger_obj: logging.Logger,
    event: str,
    message: str,
    level: int = logging.INFO,
    **context: Any,
) -> None:
    """Emit a structured log record carrying an explicit domain event name."""
    logger_obj.log(
        level,
        message,
        extra={
            "event": event,
            "context": context,
        },
    )
