"""Centralized error handling for the API.

Domain exceptions are mapped to HTTP responses. Stack traces are never
returned to the user.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import (
    BadRequestError,
    ConsumerNotAllowedError,
    InvalidPayloadError,
    PIIProxyError,
    ProcessingError,
    RestorationStateNotFoundError,
    ServiceOverloadedError,
    ServiceUnavailableError,
)
from app.observability.logging import get_logger

logger = get_logger("api.errors")


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers on the FastAPI app."""

    @app.exception_handler(PIIProxyError)
    async def handle_domain_error(request: Request, exc: PIIProxyError) -> JSONResponse:
        logger.warning(
            "request_error error_type=%s status=%s",
            exc.error_type,
            exc.status_code,
        )
        headers: dict[str, str] = {}
        if isinstance(exc, ServiceOverloadedError):
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.error_type, "message": exc.message},
            headers=headers,
        )

    @app.exception_handler(ProcessingError)
    async def handle_processing_error(
        request: Request, exc: ProcessingError
    ) -> JSONResponse:
        logger.error("processing_error error_type=%s", exc.error_type)
        return JSONResponse(
            status_code=500,
            content={"error": "PROCESSING_ERROR", "message": "internal error"},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected_error")
        return JSONResponse(
            status_code=500,
            content={"error": "INTERNAL_ERROR", "message": "internal error"},
        )


__all__ = [
    "BadRequestError",
    "ConsumerNotAllowedError",
    "InvalidPayloadError",
    "RestorationStateNotFoundError",
    "ProcessingError",
    "ServiceOverloadedError",
    "ServiceUnavailableError",
    "register_error_handlers",
]