"""Application entry point."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.api.dependencies import get_metrics
from app.api.errors import register_error_handlers
from app.api.routes.health import router as health_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.process import router as process_router
from app.config.settings import get_settings
from app.observability.logging import (
    configure_logging,
    get_logger,
    normalize_request_id,
    reset_request_id,
    set_request_id,
)

settings = get_settings()
configure_logging(settings)
logger = get_logger("http")

app = FastAPI(
    title="PII Security Proxy",
    version="0.1.0",
    description="Personal data masking module between consumer and LLM",
)


@app.middleware("http")
async def observe_http_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Attach a request id and record every HTTP outcome, including errors."""
    request_id = normalize_request_id(request.headers.get("X-Request-ID"))
    request.state.request_id = request_id
    token = set_request_id(request_id)
    metrics = get_metrics()
    started = time.perf_counter()
    status_code = 500
    response: Response | None = None
    try:
        with metrics.track_inflight():
            response = await call_next(request)
            status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration_seconds = time.perf_counter() - started
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        metrics.observe_http_request(
            request.method,
            path,
            status_code,
            duration_seconds,
        )
        logger.info(
            "http_request_completed",
            extra={
                "event": "http_request_completed",
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "status_code": status_code,
                "duration_ms": round(duration_seconds * 1000.0, 3),
            },
        )
        reset_request_id(token)

register_error_handlers(app)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(process_router)
