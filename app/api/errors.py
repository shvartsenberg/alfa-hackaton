"""Centralized error handling for the API.

Domain exceptions are mapped to HTTP responses. Stack traces are never
returned to the user.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.dependencies import get_metrics_recorder
from app.core.exceptions import (
    ConsumerNotAllowedError,
    InvalidPayloadError,
    PIIProxyError,
    ProcessingError,
    RestorationStateNotFoundError,
    ServiceOverloadedError,
)
from app.observability.logging import get_logger

logger = get_logger("api.errors")


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers on the FastAPI app."""

    def request_headers(request: Request) -> dict[str, str]:
        request_id = getattr(request.state, "request_id", None)
        return {"X-Request-ID": request_id} if request_id is not None else {}

    @app.exception_handler(PIIProxyError)
    async def handle_domain_error(request: Request, exc: PIIProxyError) -> JSONResponse:
        get_metrics_recorder().record_error(exc.error_type)
        logger.warning(
            "request_error",
            extra={
                "event": "request_error",
                "request_id": getattr(request.state, "request_id", None),
                "error_type": exc.error_type,
                "status_code": exc.status_code,
            },
        )
        headers = request_headers(request)
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
        get_metrics_recorder().record_error(exc.error_type)
        logger.error(
            "processing_error",
            extra={
                "event": "processing_error",
                "request_id": getattr(request.state, "request_id", None),
                "error_type": exc.error_type,
                "status_code": 500,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"error": "PROCESSING_ERROR", "message": "internal error"},
            headers=request_headers(request),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never log the exception message or traceback: it may contain PII.
        # Only the exception type is safe to record.
        get_metrics_recorder().record_error("INTERNAL_ERROR")
        logger.error(
            "unexpected_error",
            extra={
                "event": "unexpected_error",
                "request_id": getattr(request.state, "request_id", None),
                "error_type": "INTERNAL_ERROR",
                "exception_type": type(exc).__name__,
                "status_code": 500,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"error": "INTERNAL_ERROR", "message": "internal error"},
            headers=request_headers(request),
        )


__all__ = [
    "ConsumerNotAllowedError",
    "InvalidPayloadError",
    "RestorationStateNotFoundError",
    "ProcessingError",
    "ServiceOverloadedError",
    "register_error_handlers",
]
