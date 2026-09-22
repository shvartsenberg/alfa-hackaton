"""Application entry point."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.dependencies import get_rate_limiter
from app.api.errors import register_error_handlers
from app.api.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.api.routes.health import router as health_router
from app.api.routes.process import router as process_router
from app.config.settings import get_settings
from app.observability.logging import configure_logging

settings = get_settings()
configure_logging(settings)

app = FastAPI(
    title="PII Security Proxy",
    version="0.1.0",
    description="Personal data masking module between consumer and LLM",
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(RateLimitMiddleware, limiter=get_rate_limiter())

register_error_handlers(app)
app.include_router(health_router)
app.include_router(process_router)