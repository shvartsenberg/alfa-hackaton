"""Safe logging configuration.

Never log payloads, original text, mapping values, or PIIEntity.value.
Only log hashed identifiers, operation names, PII types, counts, and timings.
"""

from __future__ import annotations

import logging
import sys

from app.config.settings import Settings

_LOGGER_NAME = "pii_security_proxy"


def configure_logging(settings: Settings) -> None:
    """Configure the root application logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger(_LOGGER_NAME)
    root.setLevel(settings.log_level.upper())
    root.handlers = [handler]
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the application namespace."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")