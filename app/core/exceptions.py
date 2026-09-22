"""Domain exceptions for the PII security proxy."""

from __future__ import annotations


class PIIProxyError(Exception):
    """Base class for all domain errors."""

    status_code: int = 500
    error_type: str = "PROCESSING_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConsumerNotAllowedError(PIIProxyError):
    """Raised when a consumer is disabled or unknown."""

    status_code = 403
    error_type = "CONSUMER_NOT_ALLOWED"


class InvalidPayloadError(PIIProxyError):
    """Raised when the request payload is invalid."""

    status_code = 422
    error_type = "INVALID_PAYLOAD"


class RestorationStateNotFoundError(PIIProxyError):
    """Raised when no restoration state exists for a payload_id."""

    status_code = 404
    error_type = "RESTORATION_STATE_NOT_FOUND"


class ProcessingError(PIIProxyError):
    """Raised when an unexpected processing failure occurs."""

    status_code = 500
    error_type = "PROCESSING_ERROR"


class ServiceOverloadedError(PIIProxyError):
    """Raised when the service cannot accept more load."""

    status_code = 429
    error_type = "SERVICE_OVERLOADED"

    def __init__(self, message: str, retry_after: int = 1) -> None:
        super().__init__(message)
        self.retry_after = retry_after