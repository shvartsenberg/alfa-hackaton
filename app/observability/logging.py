"""Safe logging configuration.

Never log payloads, original text, mapping values, or PIIEntity.value.
Only log hashed identifiers, operation names, PII types, counts, and timings.

Logs are emitted as structured JSON by default (``LOG_FORMAT=json``). Set
``LOG_FORMAT=text`` for a human-readable single-line format.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime

from app.config.settings import Settings

_LOGGER_NAME = "pii_security_proxy"
_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STRUCTURED_FIELDS = (
    "event",
    "request_id",
    "method",
    "path",
    "status_code",
    "operation",
    "consumer_id",
    "payload_id_hash",
    "detected_types",
    "entity_count",
    "duration_ms",
    "error_type",
    "exception_type",
)


class JsonFormatter(logging.Formatter):
    """Emit each record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        request_id = getattr(record, "request_id", None) or _REQUEST_ID.get()
        if request_id is not None:
            payload["request_id"] = request_id
        for field in _STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            # Never include the exception text/traceback: it may contain PII.
            exc_type = record.exc_info[0]
            if exc_type is not None:
                payload["exception_type"] = exc_type.__name__
        return json.dumps(payload, ensure_ascii=False)


def _build_formatter(log_format: str) -> logging.Formatter:
    if log_format.lower() == "json":
        return JsonFormatter()
    return logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def configure_logging(settings: Settings) -> None:
    """Configure the root application logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_build_formatter(settings.log_format))
    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(settings.log_level.upper())
    root.handlers = [handler]
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the application namespace."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def normalize_request_id(candidate: str | None) -> str:
    """Return a safe external request id or generate a new UUID."""
    if candidate is not None and _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def set_request_id(request_id: str) -> Token[str | None]:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _REQUEST_ID.reset(token)


__all__ = [
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "normalize_request_id",
    "reset_request_id",
    "set_request_id",
]
